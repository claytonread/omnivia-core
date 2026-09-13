"""Service-owned workspace bootstrap (R004-10, owner resolution 004 Packet B).

The one shared implementation of "make this directory into a workspace a service
can own". `omnivia init` invokes it as a subprocess and the MCP adapter may point
a user at it; neither imports this module. It lives here rather than in the CLI
for the reason R004-10 gives -- exclusive database and workspace initialisation
are legal in `omnivia-core-service` and nowhere else.

**Why it exists at all.** No shipped command created a workspace. `runner.py`
refuses an unbootstrapped one with "workspace has no ownership substrate; migrate
it before serving", and both routes that could produce one were private Python
API, so `omnivia start` on a fresh machine had nothing to start.

**The sequence, established against the code rather than taken on trust.** The
database comes first and the filesystem last, which is the opposite of the
obvious order and is deliberate -- see "Non-destructive" below.

    1. Take the workspace's lifetime storage lock, creating only `locks/` to put
       it in.
    2. Create the database *file*. This step is easy to miss and it is why the
       obvious sequence does not run: `open_database` only creates a file in
       `OpenMode.EPHEMERAL` -- `may_create` is that mode and no other -- so
       opening a fresh workspace `SERVICE_OWNED` raises `StorageError: no
       workspace database at ...` instead of bootstrapping one.
    3. `open_database(..., OpenMode.EXCLUSIVE_MAINTENANCE)`, twice. First with
       `enable_wal=False`, to decide both refusals without moving a byte of a
       database that turns out not to be ours -- see "Non-destructive" below --
       and then, once it is ours, for real. Exclusive, which is what
       `bootstrap_generation_one` requires, and *not* `SERVICE_OWNED`: this
       process is not a service, does not advertise readiness and does not hold
       the workspace for a lifetime. It is the same mode `migrate_legacy_database`
       uses for the same reason.
    4. `bootstrap_generation_one(..., expect_phase0_baseline=False)` -- the
       pristine branch, which materialises the frozen Phase 0 scaffolding so the
       migrations from 0002 onward have the tables they add triggers to.
    5. `apply_pending_migrations(...)`.
    6. `create_workspace(root, manifest)` -- the portable five-path layout and an
       atomically written `workspace.json`. It creates **no database**, which is
       what lets it run this late.
    7. `InstallationLayout(installation_root).create(workspace_id)` -- the
       installation-local backups, attempts and runtime directories.

`migrate_legacy_database` is the *other* route and is not this one: it requires an
existing Phase 0-fingerprinted SQLite file to adopt.

**Idempotence is read out of the database, never off a path.** `bootstrap_generation_one`
returns the existing state rather than re-creating it and `apply_pending_migrations`
applies only what is pending, so a second run genuinely re-runs the whole sequence
against the real substrate. Deciding "already done" from `workspace.json` existing
would report success for a workspace whose database was never bootstrapped -- which
is exactly the state `runner.py` refuses to serve.

**Non-destructive, and refusing rather than guessing.** The manifest is written on
one path only: when there is none. An existing manifest is read and kept. Nothing
here deletes, truncates or overwrites anything, and the three cases R004-10 names
are refused before any of it starts.

**And a refusal leaves the data tree as it found it.** That is a stronger claim
than "nothing is overwritten" and it used not to hold. `init` wrote the manifest
and the installation-state tree first and consulted the database last, so a
workspace whose manifest was lost while its database survived had a *fresh*
`workspace_id` minted and written -- `layout.exists()` asks about the manifest path
-- before the exclusive open discovered it disagreeing. The refusal was correct
and the manifest it had just written stayed, so every later run refused too and no
shipped command could initialise that installation again. The order above is the
fix, and it is an order rather than a rollback: no manifest, no layout directory
and no installation state is written until the database work has succeeded, so
there is nothing to take back and no window in which a crash could leave a
half-taken-back tree. On Windows, identity and foreign-database vetting deliberately
tighten a pre-existing workspace root to the owning OS user after acquiring its
lock and before opening SQLite; transient sidecar names would otherwise remain
replaceable by another local principal. `WORKSPACE_BUSY` still changes no ACL.

**The claim is bounded, and `_bootstrap`'s docstring states the bound rather than
rounding it up.** It covers the three refusals that decide whether this workspace is
ours to touch, and not `WRITE_FAILURE`, which the storage layer or the filesystem
can raise after a manifest is already on disk. What those three leave behind is
`workspace/locks/` and the lock file they were decided under, plus the Windows
workspace-root security repair just described when vetting reaches an existing
database.

**What "changed" counts.** Four things move, not one, and all four are reported.
The manifest and the substrate row were always counted. `apply_pending_migrations`
is the third and moves on its own, so a run that applied every pending migration to
an interrupted bootstrap used to answer "already initialised; nothing was changed".
The fourth is the filesystem: both `create_directories()` and
`InstallationLayout.create()` repair on the repeat path, and a run that recreated
`blobs/`, `indexes/` and the whole installation-state tree gave that same answer
until `_absent_directories` started counting them.

**The filesystem is qualified first, on both entry points, and that is a repair.**
`runner.py` refuses to serve a workspace on a remote or unrecognised filesystem --
ADR-037 requires it, because direct writable operation needs lock semantics an
NFS/SMB/SSHFS/WebDAV mount does not provide -- but it refuses at *start*, and both
entry points here could create a manifest, a layout, a database, an installation
tree and a lock on such a mount long before anything reached that refusal. So the
same `ownership.locks.qualify_filesystem` gate runs before any of it, with the same
refused and qualified lists rather than a second copy of them, and the answer is
`UNQUALIFIED_FILESYSTEM` rather than a guess at one of the existing six names.

The qualification is taken against the nearest existing ancestor of the workspace
root: the root itself usually does not exist yet, and a gate that created it -- or
that left a lock probe inside a tree it was about to refuse -- would be the defect
it exists to close.

**This starts no service.** `init` establishes state; `start` or MCP managed start
establishes the process.
"""

from __future__ import annotations

import ctypes
import json
import os
import stat
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Final

from omnivia_core.workspace.compatibility import evaluate_compatibility
from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest
from omnivia_core_runtime.ownership.discovery import restrict_to_owner
from omnivia_core_runtime.ownership.locks import (
    LockRole,
    create_lock,
    qualify_filesystem,
)
from omnivia_core_runtime.storage.backup import (
    ATTEMPTS_DIR,
    BACKUPS_DIR,
    CATALOGUE_DIR,
    INSTALLATION_DATABASE,
    INSTALLATION_LOCK,
    RUNTIME_DIR,
    InstallationLayout,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    fingerprint_schema,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations,
    bootstrap_generation_one,
    read_workspace_state,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import (
    ManifestStoreError,
    create_workspace,
    read_manifest,
)

#: Version of the machine-readable result document below. Bumped when a consumer
#: would have to change to keep reading it; additive fields do not bump it.
#:
#: **The symbolic codes in this document are compatibility-controlled from 1.0
#: onward.** That means the *serialised strings* of `WorkspaceInitStatus` and
#: `WorkspaceInitRefusal`, not their Python member names: an out-of-repo adapter
#: branches on `"unrelated_directory"`, and it never sees the identifier
#: `UNRELATED_DIRECTORY`. So renaming a member is a refactor; changing the string
#: beside it, or moving a case from one string to another, is a wire break and
#: requires a version increment and a published mapping. Owner resolution 006
#: R006-07 rules this after a round of this packet moved the foreign-database case
#: from `write_failure` to `unrelated_directory` while leaving `1.0` in place --
#: "the absence of an in-repo positional consumer does not prove the absence of an
#: external consumer".
#:
#: `test_every_published_code_serialises_to_its_pinned_wire_value` is the control.
#: Before it existed, renaming `unrelated_directory` to anything at all left the
#: whole suite green: every other assertion in the tree compares against
#: `WorkspaceInitRefusal.X.value`, which moves with the mutation.
#:
#: **What 1.0 is, on the accepted record.** Nothing. This module has never been in
#: an accepted baseline -- absent from `origin/main`, absent from baseline pointer
#: 009's commit `27a958fd`, no tag and no release anywhere in the repository, and
#: no consumer of `workspace_init_version` in any sibling repository. So the wire
#: value that "moved" moved only inside this unmerged branch, there is no prior
#: accepted value to restore, and this packet is the *first* publication of 1.0
#: rather than a change to it. The control above is what makes that first
#: publication binding.
#:
#: **1.1 widens that vocabulary by exactly one code, and widening it is why the
#: version moved.** `UNQUALIFIED_FILESYSTEM` is new; every 1.0 status and refusal
#: keeps its exact string, and no case moved from one string to another. So a 1.0
#: reader that branches on the six it knows still reads every document 1.0 could
#: produce, and meets an unknown `refusal` only in the state 1.0 had no name for --
#: the additive direction rather than a wire break, which is why this is 1.1 and not
#: 2.0. `test_the_published_vocabulary_widened_additively_from_1_0` is the control:
#: it pins 1.0's six codes as a subset, by value, independently of the enum.
#:
#: **1.2 widens it by one more.** `WORKSPACE_REGISTRATION_CONFLICT` is new, for the
#: same reason `UNQUALIFIED_FILESYSTEM` was: `--init` now registers the canonical
#: workspace it bootstraps into the installation catalogue
#: (`installation_bootstrap.py`), and a workspace id already registered there under a
#: *different* path is a fact 1.1 had no name for. Every 1.1 status and refusal keeps
#: its exact string. `test_the_published_vocabulary_widened_additively_from_1_1` is
#: the control.
WORKSPACE_INIT_VERSION: Final = "1.2"

#: The workspace format a new workspace is created in. Mirrors
#: `bootstrap_generation_one`'s own default, and
#: `test_a_new_workspace_is_created_in_a_supported_format` holds it inside
#: `SUPPORTED_WORKSPACE_FORMATS` so the two cannot drift into a workspace this
#: build creates and then refuses to open.
WORKSPACE_FORMAT_VERSION: Final = "1"

#: The manifest's optional human label. A constant rather than anything derived
#: from the path: the manifest is portable, and putting a user's home directory
#: into it would carry a machine's filesystem layout wherever the workspace goes.
DEFAULT_WORKSPACE_NAME: Final = "OmniVia workspace"

#: Kept as a module seam so the Windows policy can be exercised on non-Windows
#: hosts without mutating ``os.name`` (which would make ``pathlib.Path`` select
#: an unusable concrete path class midway through a test).
_WINDOWS_OWNER_CONTROL: Final = os.name == "nt"

#: A Windows symbolic link, junction or mount point is a directory to ordinary
#: stat predicates. The no-follow attribute is what keeps directory creation from
#: accepting one as a pre-existing component in the workspace chain.
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400

# Windows handles opened with these flags name the entry itself and deliberately
# omit FILE_SHARE_DELETE, pinning it against rename/replacement until close.
_FILE_SHARE_READ: Final = 0x00000001
_FILE_SHARE_WRITE: Final = 0x00000002
_OPEN_EXISTING: Final = 3
_FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value

#: The only top-level entries an installation-state root may hold. Anything else
#: means this directory is not one of ours, and `InstallationLayout` is the single
#: source of all four names.
INSTALLATION_ENTRIES: Final = frozenset(
    {BACKUPS_DIR, ATTEMPTS_DIR, CATALOGUE_DIR, RUNTIME_DIR}
)
INSTALLATION_CATALOGUE_ENTRIES: Final = frozenset(
    {
        INSTALLATION_DATABASE,
        f"{INSTALLATION_DATABASE}-shm",
        f"{INSTALLATION_DATABASE}-wal",
        INSTALLATION_LOCK,
    }
)

#: Entries an operating system or file manager writes on its own, which say nothing
#: about whose directory this is.
#:
#: Both refusals below used to count these as somebody else's content, and the
#: consequence was not theoretical: opening `~/.omnivia` in Finder writes a
#: `.DS_Store`, and one of those was enough to make `omnivia init` refuse that
#: installation permanently -- `UNRELATED_DIRECTORY` for the workspace,
#: `UNRECOGNISED_INSTALLATION_STATE` for the installation state -- with no shipped
#: command able to clear it.
#:
#: A closed list of names rather than "ignore every dotfile", because the refusal is
#: worth keeping for everything a *person* put here: a `.git` directory in a
#: workspace root is somebody's repository and mixing a workspace into it is exactly
#: what R004-10's second refusal exists to prevent.
#:
#: The second group below is what a *filesystem* puts here rather than a file
#: manager, and it was missing. Every one of them is created without anyone asking:
#: `lost+found` by `mke2fs` at the root of every ext2/3/4 volume, `$RECYCLE.BIN` by
#: Windows, and the rest by macOS the first time a volume is written to or backed
#: up. A user whose home is the root of its own disk had `~/.omnivia` refused
#: permanently by a directory they never made and cannot delete.
OS_GENERATED_ENTRIES: Final = frozenset(
    {
        ".DS_Store",  # macOS Finder
        ".localized",  # macOS
        ".Spotlight-V100",  # macOS
        ".fseventsd",  # macOS
        ".Trashes",  # macOS
        ".apdisk",  # macOS
        "Thumbs.db",  # Windows Explorer
        "desktop.ini",  # Windows Explorer
        ".directory",  # KDE Dolphin
        "lost+found",  # ext2/3/4, made by mke2fs and refilled by fsck
        "$RECYCLE.BIN",  # Windows, per volume
        "Temporary Items",  # macOS, on exFAT and network volumes
        ".TemporaryItems",  # macOS
        ".DocumentRevisions-V100",  # macOS versions store
        "Network Trash Folder",  # macOS, on AFP and SMB volumes
        ".AppleDouble",  # netatalk
        ".com.apple.timemachine.donotpresent",  # macOS Time Machine
    }
)

#: The AppleDouble sidecar prefix, matched on *files* only. A copy onto a
#: non-native filesystem leaves one beside each file, so the names cannot be
#: enumerated -- but the rule was a bare prefix test with no such restriction, and
#: it admitted `._my_private_repo/` with a `.git` inside it. Wider than its warrant:
#: a directory is nobody's AppleDouble sidecar.
APPLEDOUBLE_PREFIX: Final = "._"

#: freedesktop.org's per-uid trash, matched on directories by prefix for the reason
#: a closed list cannot serve: it is `.Trash-1000` for the first login account on
#: the machine and `.Trash-1001` for the next, so naming one brick-proofs one user
#: and leaves every other user on that machine refused.
TRASH_PREFIX: Final = ".Trash-"

#: What the lock this holds is recorded as. It is not a service instance -- no
#: service exists yet -- and the value is diagnostic only.
LOCK_HOLDER: Final = "omnivia-core-service --init"


class WorkspaceInitStatus(str, Enum):
    """Whether a workspace was made, was already there, or neither."""

    INITIALISED = "initialised"
    ALREADY_INITIALISED = "already_initialised"
    REFUSED = "refused"


class WorkspaceInitRefusal(str, Enum):
    """Why nothing was written. Closed on purpose, like managed start's set.

    The first three are the cases R004-10 names by hand. The last three cover the
    ways the sequence can stop once it has started, and are separate names rather
    than one bucket because an adapter's advice differs: a busy workspace means a
    service already owns it, an identity mismatch means the manifest and the
    database disagree about which workspace this is, and a write failure means the
    filesystem said no.

    `UNQUALIFIED_FILESYSTEM` is the seventh and is none of those. It means the
    location itself cannot host a directly writable workspace -- a remote mount with
    no reliable cross-host locking, or a filesystem this build has not qualified --
    which is a fact about the *volume* rather than about anything on it, and which
    `runner.py` refuses on at start. It is deliberately not `UNRELATED_DIRECTORY`
    (nobody else's data is there; an empty NFS export is refused too), not
    `WRITE_FAILURE` (nothing was attempted, let alone refused, and the remedy is a
    different location or one networked Core Service rather than free space), and
    not `UNRECOGNISED_INSTALLATION_STATE` (it is the workspace root that is
    unqualified). It is the code `WORKSPACE_INIT_VERSION` 1.1 adds; see there.

    `WORKSPACE_IDENTITY_MISMATCH` is the newest and was carved out of
    `WRITE_FAILURE`, which is where it used to surface. Two reasons, and the second
    is the one that mattered: nothing failed to write, so a name meaning "the
    filesystem said no" was untrue; and the state it names has its own remedy --
    restore the manifest the database's workspace had, or point `--workspace`
    somewhere else -- which is not the remedy for a full disk.

    `WORKSPACE_REGISTRATION_CONFLICT` is `WORKSPACE_INIT_VERSION` 1.2's addition,
    raised by `installation_bootstrap.py` rather than by this module: this
    workspace's id is already durably registered in the installation catalogue at a
    *different* path, so admitting it here would silently re-point an existing
    authorisation. `WRITE_FAILURE` would be the same misnaming
    `WORKSPACE_IDENTITY_MISMATCH` was carved out of -- nothing failed to write, and
    the remedy (pick a different `--installation-state`, or resolve which path the
    catalogue should authorise) is not "retry".

    **`UNRELATED_DIRECTORY` has two arrival points, and the second was misnamed for
    the same reason.** A workspace root holding somebody's files is found by listing
    it, before the lock. A `workspace.sqlite` holding somebody's *tables* is not:
    that filename is a permitted layout entry, so `unexpected_entries()` cannot see
    it, and the fact is only readable by opening the database. That case reached
    callers as `WRITE_FAILURE` -- the `SchemaCreationRefused` from
    `bootstrap_generation_one`'s pristine branch, caught and reported as a write
    that failed. Nothing failed to write. It is one refusal with one remedy, so it
    is one name, and the sentence says which of the two it is.

    `WRITE_FAILURE` now means only what it says. It is also the one refusal the
    ordering in `_bootstrap` does not bound -- see that docstring.

    **Every value below is a literal, and that is a contract rather than a style.**
    R006-07 requires that a refusal code never depend on declaration position, so
    `auto()`, an implicit `_generate_next_value_`, and any integer counter are all
    excluded: with those, inserting a member above another silently renumbers the
    wire. The strings are what `WORKSPACE_INIT_VERSION` declares
    compatibility-controlled from 1.0 onward, and
    `test_a_refusal_code_never_depends_on_its_declaration_position` enforces the
    literal-value rule over this source. Reorder these seven freely; rewrite one of
    the strings and the contract has broken.
    """

    INCOMPATIBLE_MANIFEST = "incompatible_manifest"
    UNRELATED_DIRECTORY = "unrelated_directory"
    UNRECOGNISED_INSTALLATION_STATE = "unrecognised_installation_state"
    UNQUALIFIED_FILESYSTEM = "unqualified_filesystem"
    WORKSPACE_IDENTITY_MISMATCH = "workspace_identity_mismatch"
    WORKSPACE_BUSY = "workspace_busy"
    WRITE_FAILURE = "write_failure"
    WORKSPACE_REGISTRATION_CONFLICT = "workspace_registration_conflict"


@dataclass(frozen=True)
class WorkspaceInitResult:
    """What the invoking adapter receives, and the whole of it.

    R004-10 requires a clear result identifying the initialised workspace and no
    secrets in it. What is carried is the workspace's own identity, its declared
    format, and the two roots the caller already named on the command line. No
    lock payload, no service-instance identity, no lease, no fencing generation --
    none of which a caller of `init` has any use for, and the first two of which
    are facts about a process rather than about a workspace.
    """

    status: WorkspaceInitStatus
    reason: str
    refusal: WorkspaceInitRefusal | None = None
    workspace_id: str | None = None
    workspace_root: Path | None = None
    installation_root: Path | None = None
    workspace_format_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The versioned machine-readable document, and nothing human in it."""
        return {
            "workspace_init_version": WORKSPACE_INIT_VERSION,
            "status": self.status.value,
            "refusal": None if self.refusal is None else self.refusal.value,
            "reason": self.reason,
            "workspace": self._workspace(),
        }

    def _workspace(self) -> dict[str, Any] | None:
        if self.workspace_id is None:
            return None
        return {
            "workspace_id": self.workspace_id,
            "workspace_root": str(self.workspace_root),
            "installation_state": str(self.installation_root),
            "workspace_format_version": self.workspace_format_version,
        }


def initialise_workspace(
    *,
    workspace_root: Path,
    installation_root: Path,
    core_version: str = "0.1.0",
) -> WorkspaceInitResult:
    """Make `workspace_root` a workspace a service can own, or refuse and say why.

    Safe to repeat. A second call re-runs the whole sequence against the workspace
    that is there: it keeps the existing manifest, and it reads the ownership
    substrate out of the database rather than inferring it from the manifest file
    existing.
    """
    try:
        with _windows_initialisation_guard(workspace_root, installation_root):
            return _initialise_workspace(
                workspace_root=workspace_root,
                installation_root=installation_root,
                core_version=core_version,
                harden_windows_on_success=True,
            )
    except OSError:
        return _windows_path_refusal(workspace_root, installation_root)


def _initialise_workspace(
    *,
    workspace_root: Path,
    installation_root: Path,
    core_version: str,
    harden_windows_on_success: bool,
) -> WorkspaceInitResult:
    """Implementation seam used to defer DACL repair until registration accepts."""
    unsafe = _windows_unsafe_initialisation_tree(workspace_root, installation_root)
    if unsafe is not None:
        return unsafe

    qualification = qualify_filesystem(workspace_root)
    if not qualification.writable:
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            refusal=WorkspaceInitRefusal.UNQUALIFIED_FILESYSTEM,
            reason=(
                f"{workspace_root} cannot hold a directly writable workspace: "
                f"{qualification.reason}. Nothing was written"
            ),
        )

    unrecognised = _unrecognised_installation_state(installation_root)
    if unrecognised is not None:
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            refusal=WorkspaceInitRefusal.UNRECOGNISED_INSTALLATION_STATE,
            reason=(
                f"{installation_root} is not an OmniVia installation-state "
                f"directory; it holds {unrecognised}. Nothing was written -- pass a "
                "different location rather than adopting this one"
            ),
        )

    layout = WorkspaceLayout(root=workspace_root)
    if _workspace_manifest_exists(layout):
        existing = _existing_manifest(layout, core_version)
        if isinstance(existing, WorkspaceInitResult):
            return existing
        manifest, minted = existing, False
    else:
        unrelated = _chosen_by_somebody(layout.root, layout.unexpected_entries())
        if unrelated:
            return WorkspaceInitResult(
                status=WorkspaceInitStatus.REFUSED,
                refusal=WorkspaceInitRefusal.UNRELATED_DIRECTORY,
                reason=(
                    f"{workspace_root} holds no workspace manifest but is not empty "
                    f"({', '.join(unrelated)}); nothing was written. Initialising "
                    "here would mix a workspace into an unrelated directory"
                ),
            )
        manifest, minted = _new_manifest(core_version), True

    return _bootstrap(
        layout=layout,
        installation_root=installation_root,
        manifest=manifest,
        minted=minted,
        core_version=core_version,
        restrict_parent_on_windows=False,
        harden_windows_on_success=harden_windows_on_success,
    )


def initialise_allocated_workspace(
    *,
    workspace_root: Path,
    installation_root: Path,
    target_workspace_id: str,
    display_name: str,
    core_version: str = "0.1.0",
) -> WorkspaceInitResult:
    """Bootstrap one installation-authorised, server-minted workspace target.

    Unlike :func:`initialise_workspace`, this entry point never invents identity:
    the installation catalogue already durably claimed ``target_workspace_id`` and
    derived ``workspace_root`` before filesystem work began.  A retry either
    finishes that exact target or refuses; it cannot select a replacement.
    """
    try:
        with _windows_initialisation_guard(workspace_root, installation_root):
            return _initialise_allocated_workspace(
                workspace_root=workspace_root,
                installation_root=installation_root,
                target_workspace_id=target_workspace_id,
                display_name=display_name,
                core_version=core_version,
            )
    except OSError:
        return _windows_path_refusal(
            workspace_root,
            installation_root,
            workspace_id=target_workspace_id,
        )


def _initialise_allocated_workspace(
    *,
    workspace_root: Path,
    installation_root: Path,
    target_workspace_id: str,
    display_name: str,
    core_version: str,
) -> WorkspaceInitResult:
    unsafe = _windows_unsafe_initialisation_tree(
        workspace_root,
        installation_root,
        workspace_id=target_workspace_id,
    )
    if unsafe is not None:
        return unsafe

    qualification = qualify_filesystem(workspace_root)
    if not qualification.writable:
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            refusal=WorkspaceInitRefusal.UNQUALIFIED_FILESYSTEM,
            reason="the allocated target is not on a qualified local filesystem",
            workspace_id=target_workspace_id,
            workspace_root=workspace_root,
            installation_root=installation_root,
        )

    unrecognised = _unrecognised_installation_state(installation_root)
    if unrecognised is not None:
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            refusal=WorkspaceInitRefusal.UNRECOGNISED_INSTALLATION_STATE,
            reason="the installation state directory is not recognised",
            workspace_id=target_workspace_id,
            workspace_root=workspace_root,
            installation_root=installation_root,
        )

    layout = WorkspaceLayout(root=workspace_root)
    if _workspace_manifest_exists(layout):
        existing = _existing_manifest(layout, core_version)
        if isinstance(existing, WorkspaceInitResult):
            return existing
        if existing.workspace_id != target_workspace_id:
            return WorkspaceInitResult(
                status=WorkspaceInitStatus.REFUSED,
                refusal=WorkspaceInitRefusal.WORKSPACE_IDENTITY_MISMATCH,
                reason="the allocated target contains a different workspace identity",
                workspace_id=target_workspace_id,
                workspace_root=workspace_root,
                installation_root=installation_root,
                workspace_format_version=(
                    existing.compatibility.workspace_format_version
                ),
            )
        manifest, minted = existing, False
    else:
        unrelated = _chosen_by_somebody(layout.root, layout.unexpected_entries())
        if unrelated:
            return WorkspaceInitResult(
                status=WorkspaceInitStatus.REFUSED,
                refusal=WorkspaceInitRefusal.UNRELATED_DIRECTORY,
                reason="the allocated target contains unrelated content",
                workspace_id=target_workspace_id,
                workspace_root=workspace_root,
                installation_root=installation_root,
            )
        manifest = _new_manifest(
            core_version, workspace_id=target_workspace_id, name=display_name
        )
        minted = True

    return _bootstrap(
        layout=layout,
        installation_root=installation_root,
        manifest=manifest,
        minted=minted,
        core_version=core_version,
        restrict_parent_on_windows=True,
        harden_windows_on_success=True,
    )


def _unrecognised_installation_state(root: Path) -> str | None:
    """The foreign entries an existing installation-state root holds, if any.

    An absent root is not unrecognised -- it is what a fresh machine has, and
    creating it is this command's job. What is refused is a directory that exists
    and is plainly something else, because writing `backups/`, `attempts/`,
    `catalogue/` and `runtime/` into it would adopt it.  The catalogue name is not
    a blanket exception: it must be a real directory and may contain only the
    installation database, its lifetime lock, and ordinary OS litter.
    """
    if not root.is_dir():
        return None
    foreign = _chosen_by_somebody(
        root,
        sorted(
            entry.name
            for entry in root.iterdir()
            if entry.name not in INSTALLATION_ENTRIES
        ),
    )
    catalogue = root / CATALOGUE_DIR
    if catalogue.exists() or catalogue.is_symlink():
        if catalogue.is_symlink() or not catalogue.is_dir():
            foreign.append(CATALOGUE_DIR)
        else:
            nested = _chosen_by_somebody(
                catalogue,
                sorted(
                    entry.name
                    for entry in catalogue.iterdir()
                    if entry.name not in INSTALLATION_CATALOGUE_ENTRIES
                ),
            )
            foreign.extend(f"{CATALOGUE_DIR}/{name}" for name in nested)
    return ", ".join(foreign) if foreign else None


def _chosen_by_somebody(root: Path, names: Iterable[str]) -> list[str]:
    """`names` under `root` without the entries no person put there.

    `root` is needed because two of the three rules are about what the entry *is*
    and not only what it is called. See `OS_GENERATED_ENTRIES`.
    """
    return [name for name in names if not _os_generated(root / name)]


def _workspace_manifest_exists(layout: WorkspaceLayout) -> bool:
    """Decide manifest presence without ever following a Windows redirection.

    The Windows initialization guard pins an entry that exists, but deliberately
    makes no promise that an absent name will remain absent.  A regular manifest is
    therefore pinned at the decision itself.  An absent name is only a provisional
    answer: :func:`_bootstrap` repeats it after the workspace directory has been
    quiesced and made owner-only, before any database or manifest write.
    """
    if not _WINDOWS_OWNER_CONTROL:
        return layout.exists()
    try:
        os.lstat(layout.manifest_path)
    except FileNotFoundError:
        return False
    except OSError as failure:
        raise OSError("workspace manifest presence could not be proved") from failure
    if not _is_real_file_no_follow(layout.manifest_path):
        raise OSError("workspace manifest is not a regular no-follow file")
    _pin_current_windows_path(layout.manifest_path, directory=False)
    return True


def _os_generated(entry: Path) -> bool:
    """Whether the operating system, rather than a person, put this here.

    **The two *prefix* rules are checked against the entry's type. The closed list is
    not, and this docstring used to say it was.** For the prefixes both directions of
    the test are load-bearing: an AppleDouble sidecar is a file, so
    `._my_private_repo/` -- a name that looks like litter and holds a `.git` -- is
    somebody's, not ours to initialise into; a freedesktop trash is a directory.
    Anything failing its own type test falls through to being somebody's, which is
    the safe direction: the cost is a refusal a person can act on, and the cost of
    the other direction is a workspace mixed into their files.

    `OS_GENERATED_ENTRIES` is matched on **name alone**, so that safe direction does
    not apply to any of its seventeen entries. A regular *file* named `lost+found`,
    or `Thumbs.db`, or any other name on the list, is admitted -- and admitting it is
    what lets the directory holding it be adopted and initialised into. That is the
    same overreach the `._` rule was repaired for, left in place for every name the
    list covers, and it is stated here rather than rounded up to a type test the code
    does not perform.

    Closing it needs an expected type per name, which is a wider change than a
    correction: several of these entries are not reliably one type across the
    filesystems they appear on, and a wrong entry re-bricks precisely the
    installation the list was added to unbrick. Recorded as follow-up rather than
    guessed at here.
    """
    if entry.name in OS_GENERATED_ENTRIES:
        return True
    if entry.name.startswith(APPLEDOUBLE_PREFIX):
        return entry.is_file()
    return entry.name.startswith(TRASH_PREFIX) and entry.is_dir()


def _existing_manifest(
    layout: WorkspaceLayout, core_version: str
) -> WorkspaceManifest | WorkspaceInitResult:
    """The manifest already on disk, or the refusal that it is not usable.

    Both failure modes are one refusal, because they are one fact to a caller: the
    manifest that is there is not one this build may initialise against. An
    unreadable or malformed document and a well-formed one outside this build's
    version window differ only in the sentence.
    """
    try:
        manifest = read_manifest(layout)
    except ManifestStoreError as refusal:
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            refusal=WorkspaceInitRefusal.INCOMPATIBLE_MANIFEST,
            reason=(
                f"{layout.manifest_path} is not a manifest this build can use "
                f"({refusal}); it was left exactly as it is"
            ),
        )

    outcome = evaluate_compatibility(manifest, core_version)
    if not outcome.writable:
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            refusal=WorkspaceInitRefusal.INCOMPATIBLE_MANIFEST,
            reason=(
                f"{layout.manifest_path} describes a workspace this build cannot "
                f"initialise: {outcome.reason}. It was left exactly as it is"
            ),
            workspace_id=manifest.workspace_id,
            workspace_root=layout.root,
            workspace_format_version=outcome.workspace_format_version,
        )
    return manifest


def _revalidate_windows_manifest_selection(
    layout: WorkspaceLayout,
    *,
    manifest: WorkspaceManifest,
    minted: bool,
    core_version: str,
) -> None:
    """Bind the provisional manifest decision after securing its namespace.

    Before the lifetime lock, presence checks are read-only so a busy refusal does
    not rewrite ACLs.  Once that lock is held, the Windows path secures and
    quiesces the workspace root.  Absence is stable against other local principals
    only at that point.  A name that appeared, disappeared or changed meanwhile is
    a refusal; initialization never adopts or overwrites the race winner.
    """
    if not _WINDOWS_OWNER_CONTROL:
        return
    exists = _workspace_manifest_exists(layout)
    if minted:
        if exists:
            raise OSError("workspace manifest appeared during initialization")
        return
    if not exists:
        raise OSError("workspace manifest disappeared during initialization")
    current = _existing_manifest(layout, core_version)
    if isinstance(current, WorkspaceInitResult) or current != manifest:
        raise OSError("workspace manifest changed during initialization")


def _new_manifest(
    core_version: str,
    *,
    workspace_id: str | None = None,
    name: str = DEFAULT_WORKSPACE_NAME,
) -> WorkspaceManifest:
    """A manifest for a workspace that does not exist yet.

    `min_core_version` is the build doing the creating, which is the honest claim:
    this workspace was made by Core `core_version` and an older build should not
    open it for writing. No upper bound is declared -- a workspace that excluded
    future builds by default would need re-writing to stay openable.
    """
    return WorkspaceManifest(
        workspace_id=workspace_id or f"ws-{uuid.uuid4()}",
        created_at=datetime.now(UTC).isoformat(),
        name=name,
        compatibility=CoreCompatibility(
            workspace_format_version=WORKSPACE_FORMAT_VERSION,
            min_core_version=core_version,
        ),
    )


def _is_real_directory_no_follow(path: Path) -> bool:
    """Whether ``path`` is an existing directory and not a link/reparse point."""
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        == 0
    )


def _is_real_file_no_follow(path: Path) -> bool:
    """Whether ``path`` is an existing regular file, never a reparse point."""
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and (not _WINDOWS_OWNER_CONTROL or metadata.st_nlink == 1)
        and getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        == 0
    )


def _existing_windows_path_is_safe(path: Path, *, directory: bool) -> bool:
    """Accept an absent name or an existing ordinary object of the expected kind."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return (
        _is_real_directory_no_follow(path)
        if directory
        else _is_real_file_no_follow(path)
    )


def _windows_path_chain_is_safe(path: Path) -> bool:
    """Verify every existing lexical component without resolving through links."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    for component in reversed((absolute, *absolute.parents)):
        try:
            os.lstat(component)
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if not _is_real_directory_no_follow(component):
            return False
    return True


def _windows_initialisation_entries(
    workspace_root: Path, installation_root: Path
) -> tuple[tuple[Path, bool], ...]:
    """Known paths touched by bootstrap, with ``True`` for directory entries."""
    layout = WorkspaceLayout(root=workspace_root)
    installation = InstallationLayout(root=installation_root)
    entries: dict[Path, bool] = {}
    for root in (layout.root, installation.root):
        absolute = Path(os.path.abspath(os.fspath(root)))
        for component in reversed((absolute, *absolute.parents)):
            entries[component] = True
    for directory in (
        layout.root,
        layout.blobs_path,
        layout.indexes_path,
        layout.locks_path,
        installation.root,
        installation.root / BACKUPS_DIR,
        installation.root / ATTEMPTS_DIR,
        installation.root / RUNTIME_DIR,
        installation.catalogue,
    ):
        entries[Path(os.path.abspath(os.fspath(directory)))] = True
    for file_path in (
        layout.manifest_path,
        layout.database_path,
        layout.database_path.with_name(f"{layout.database_path.name}-wal"),
        layout.database_path.with_name(f"{layout.database_path.name}-shm"),
        layout.database_path.with_name(f"{layout.database_path.name}-journal"),
        layout.locks_path / "storage.lock",
        installation.installation_database,
        installation.installation_database.with_name(
            f"{installation.installation_database.name}-wal"
        ),
        installation.installation_database.with_name(
            f"{installation.installation_database.name}-shm"
        ),
        installation.installation_database.with_name(
            f"{installation.installation_database.name}-journal"
        ),
        installation.installation_lock,
    ):
        entries[Path(os.path.abspath(os.fspath(file_path)))] = False
    return tuple(
        sorted(entries.items(), key=lambda item: (len(item[0].parts), str(item[0])))
    )


def _windows_path_refusal(
    workspace_root: Path,
    installation_root: Path,
    *,
    workspace_id: str | None = None,
) -> WorkspaceInitResult:
    """The fixed fail-closed answer for an unsafe or unpinnable Windows path."""
    return WorkspaceInitResult(
        status=WorkspaceInitStatus.REFUSED,
        refusal=WorkspaceInitRefusal.WRITE_FAILURE,
        reason=(
            "the Windows workspace or installation path contains an unsafe "
            "link, reparse point, replaced entry, or filesystem object; nothing "
            "was written"
        ),
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        installation_root=installation_root,
        workspace_format_version=(
            WORKSPACE_FORMAT_VERSION if workspace_id is not None else None
        ),
    )


def _windows_path_api() -> Any:
    """CreateFile/CloseHandle configured for no-follow namespace pinning."""
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("Windows path API is unavailable")
    try:
        kernel32 = loader("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int32
    except Exception as failure:
        raise OSError("Windows path API is unavailable") from failure
    return kernel32


def _handle_value(handle: object) -> int | None:
    value = getattr(handle, "value", handle)
    return value if isinstance(value, int) else None


@contextmanager
def _quiesce_windows_directory(path: Path) -> Iterator[None]:
    """Hold one directory with zero sharing while its DACL becomes authoritative.

    A DACL change stops new opens but cannot revoke a handle another principal
    already owns.  Opening the directory with share mode zero first therefore
    rejects any live reader, writer or deleter and prevents another such handle
    from being acquired until the repair and child validation finish.  The normal
    initialization pin (desired access zero) can coexist with this handle.
    """
    if os.name != "nt":
        yield
        return

    api = _windows_path_api()
    before = os.lstat(path)
    if not _is_real_directory_no_follow(path):
        raise OSError("SQLite parent is not a real directory")
    handle = api.CreateFileW(
        str(path),
        0,
        0,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    value = _handle_value(handle)
    if value in (None, 0, _INVALID_HANDLE_VALUE):
        raise OSError("SQLite parent has a live external directory handle")
    try:
        after = os.lstat(path)
        if (
            before.st_dev,
            before.st_ino,
            stat.S_IFMT(before.st_mode),
        ) != (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)) or not (
            _is_real_directory_no_follow(path)
        ):
            raise OSError("SQLite parent changed while being quiesced")
        yield
    finally:
        api.CloseHandle(handle)


def _secure_existing_windows_directory(path: Path) -> None:
    """Quiesce, restrict and pin one directory before using a child name."""
    if not _WINDOWS_OWNER_CONTROL:
        return
    with _quiesce_windows_directory(path):
        if not _is_real_directory_no_follow(path):
            raise OSError("directory is not a real no-follow object")
        restrict_to_owner(path, directory=True)
        if not _is_real_directory_no_follow(path):
            raise OSError("directory changed while being secured")
        _pin_current_windows_path(path, directory=True)
        _refresh_current_windows_paths()


def _windows_sqlite_sidecars(
    workspace_root: Path, installation_root: Path
) -> frozenset[Path]:
    workspace = WorkspaceLayout(root=workspace_root).database_path
    installation = InstallationLayout(root=installation_root).installation_database
    return frozenset(
        Path(os.path.abspath(os.fspath(path)))
        for path in (
            workspace.with_name(f"{workspace.name}-wal"),
            workspace.with_name(f"{workspace.name}-shm"),
            workspace.with_name(f"{workspace.name}-journal"),
            installation.with_name(f"{installation.name}-wal"),
            installation.with_name(f"{installation.name}-shm"),
            installation.with_name(f"{installation.name}-journal"),
        )
    )


class _WindowsInitialisationPins:
    """No-share-delete handles for stable entries in one initialization call."""

    def __init__(self, api: Any, workspace_root: Path, installation_root: Path) -> None:
        self._api = api
        self._workspace_root = workspace_root
        self._installation_root = installation_root
        self._handles: list[object] = []
        self._held: dict[Path, tuple[int, int, int]] = {}
        self._sqlite_sidecars = _windows_sqlite_sidecars(
            workspace_root, installation_root
        )

    def pin(self, path: Path, *, directory: bool) -> None:
        """Pin one existing stable entry, comparing identity across its open."""
        absolute = Path(os.path.abspath(os.fspath(path)))
        try:
            before = os.lstat(absolute)
        except FileNotFoundError:
            return
        if not _existing_windows_path_is_safe(absolute, directory=directory):
            raise OSError("unsafe Windows path component")
        identity = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
        prior = self._held.get(absolute)
        if prior is not None:
            if prior != identity:
                raise OSError("pinned Windows path component changed")
            return

        # WAL, SHM and rollback-journal files are deletion-managed by SQLite.
        # Holding them without FILE_SHARE_DELETE would prevent checkpoint cleanup;
        # their parent and database stay pinned, and every refresh validates their
        # no-follow kind immediately before and after database operations instead.
        if absolute in self._sqlite_sidecars:
            return

        handle = self._api.CreateFileW(
            str(absolute),
            0,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        value = _handle_value(handle)
        if value in (None, 0, _INVALID_HANDLE_VALUE):
            raise OSError("Windows path component could not be pinned")
        try:
            after = os.lstat(absolute)
            observed = (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
            if identity != observed or not _existing_windows_path_is_safe(
                absolute, directory=directory
            ):
                raise OSError("Windows path component changed while opening")
        except BaseException:
            self._api.CloseHandle(handle)
            raise
        self._handles.append(handle)
        self._held[absolute] = identity

    def refresh(self) -> None:
        """Pin every known entry observed during a bounded rescan.

        This is intentionally not an absence lock: a directory handle does not
        stop creation of a child.  Callers consume an existing entry only after a
        no-follow pin, create files with an exclusive syscall, and repeat the
        provisional manifest decision after its parent has been quiesced and made
        owner-only.  The rescan closes creation windows between our own steps; it
        never promotes a missing pathname into a security fact.
        """
        for _attempt in range(4):
            held_before = len(self._held)
            entries = _windows_initialisation_entries(
                self._workspace_root, self._installation_root
            )
            for path, directory in entries:
                self.pin(path, directory=directory)

            missing_stable_entry = False
            for path, directory in _windows_initialisation_entries(
                self._workspace_root, self._installation_root
            ):
                absolute = Path(os.path.abspath(os.fspath(path)))
                try:
                    os.lstat(absolute)
                except FileNotFoundError:
                    continue
                if not _existing_windows_path_is_safe(absolute, directory=directory):
                    raise OSError("unsafe Windows path component")
                if absolute not in self._sqlite_sidecars and absolute not in self._held:
                    missing_stable_entry = True
            if not missing_stable_entry:
                return
            if len(self._held) == held_before:
                raise OSError("Windows path namespace did not stabilise")
        raise OSError("Windows path namespace did not stabilise")

    def close(self) -> None:
        for handle in reversed(self._handles):
            self._api.CloseHandle(handle)
        self._handles.clear()
        self._held.clear()


_CURRENT_WINDOWS_PINS: ContextVar[_WindowsInitialisationPins | None] = ContextVar(
    "omnivia_windows_initialisation_pins", default=None
)


def _pin_current_windows_path(path: Path, *, directory: bool) -> None:
    pins = _CURRENT_WINDOWS_PINS.get()
    if pins is not None:
        pins.pin(path, directory=directory)


def _refresh_current_windows_paths() -> None:
    pins = _CURRENT_WINDOWS_PINS.get()
    if pins is not None:
        pins.refresh()


@contextmanager
def _windows_initialisation_guard(
    workspace_root: Path, installation_root: Path
) -> Iterator[None]:
    """Pin Windows components against replacement for this entire call.

    ``CreateFileW`` opens the entry itself (including a reparse point) and omits
    ``FILE_SHARE_DELETE``. Windows consequently refuses delete, rename, or replace
    while the handle is held. Existing components are pinned before the call; every
    creation boundary refreshes the same guard so newly published directories,
    databases, manifests and locks join it. Absence is never inferred from a scan:
    creation remains exclusive, and the workspace root is quiesced and owner-only
    before a provisional absent manifest or SQLite sidecar name is consumed.
    """
    if os.name != "nt":
        yield
        return

    pins = _WindowsInitialisationPins(
        _windows_path_api(), workspace_root, installation_root
    )
    token = _CURRENT_WINDOWS_PINS.set(pins)
    try:
        pins.refresh()
        yield
        pins.refresh()
    finally:
        _CURRENT_WINDOWS_PINS.reset(token)
        pins.close()


def _windows_unsafe_initialisation_tree(
    workspace_root: Path,
    installation_root: Path,
    *,
    workspace_id: str | None = None,
) -> WorkspaceInitResult | None:
    """Refuse a Windows tree whose existing objects can redirect later writes.

    ``Path.is_dir`` and ``Path.is_file`` follow junctions and symbolic links.  That
    is unsuitable before workspace creation or ACL hardening: a junction at the
    managed home, ``workspaces`` parent, workspace root, or installation root can
    redirect every later child operation outside the selected installation.  The
    known file entries are checked too, because a symlinked manifest, database, or
    lock would otherwise be opened through that otherwise-clean directory chain.
    """
    if not _WINDOWS_OWNER_CONTROL:
        return None

    safe = _windows_path_chain_is_safe(workspace_root) and _windows_path_chain_is_safe(
        installation_root
    )
    safe = safe and all(
        _existing_windows_path_is_safe(path, directory=directory)
        for path, directory in _windows_initialisation_entries(
            workspace_root, installation_root
        )
    )
    if safe:
        return None
    return _windows_path_refusal(
        workspace_root,
        installation_root,
        workspace_id=workspace_id,
    )


def _ensure_workspace_directory(path: Path) -> bool:
    """Create ``path`` and missing parents, securing each Windows creation at once.

    A returned ``FileExistsError`` is the race-safe answer that the directory was
    not created by this call; such an object is verified by kind and left exactly
    as found. Every directory this call did create is owner-only before a child is
    made beneath it. A restriction failure removes only that new, still-empty
    directory and propagates as the existing write-failure result.
    """
    if not _WINDOWS_OWNER_CONTROL:
        path.mkdir(parents=True, exist_ok=True)
        return False
    if path.parent != path:
        # Validate (or create and secure) every parent before asking the filesystem
        # to create a child.  Calling ``mkdir`` on the child first follows an
        # existing junction in the parent and creates outside the intended tree.
        _ensure_workspace_directory(path.parent)
    path_already_existed = False
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        path_already_existed = True
    if path_already_existed:
        if not _is_real_directory_no_follow(path):
            raise OSError(f"refusing non-directory or reparse-point path: {path}")
        _pin_current_windows_path(path, directory=True)
        return False
    if not _is_real_directory_no_follow(path):
        raise OSError(f"created path is not a real directory: {path}")
    try:
        # A just-created directory can inherit a permissive DACL on Windows.
        # Reject a racing access-capable handle, hold the exact name stable, and
        # make the DACL owner-only before any child is created beneath it.
        _secure_existing_windows_directory(path)
    except OSError:
        try:
            path.rmdir()
        except OSError:
            pass
        raise
    return True


def _create_empty_database_file(path: Path) -> None:
    """Create one empty regular database name atomically and without link-following."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        created = os.fstat(descriptor)
        if not stat.S_ISREG(created.st_mode):
            raise OSError("new workspace database is not a regular file")
        _pin_current_windows_path(path, directory=False)
    finally:
        os.close(descriptor)
    observed = os.lstat(path)
    if not _is_real_file_no_follow(path) or (observed.st_dev, observed.st_ino) != (
        created.st_dev,
        created.st_ino,
    ):
        raise OSError("new workspace database was replaced during creation")


def _restrict_windows_workspace_layout(
    layout: WorkspaceLayout,
    installation_root: Path,
    *,
    restrict_parent: bool,
) -> None:
    """Make the managed-client authorization chain writable by this user alone.

    POSIX creation modes already give the accepted client what it needs.  Windows
    ignores those modes and inherits its parent's DACL, commonly including writable
    SYSTEM and Administrators ACEs.  The client intentionally refuses that shape,
    so successful initialization establishes the matching owner-only DACL on the
    managed home trust anchor, the allocated ``workspaces`` parent when present,
    the workspace root, and an existing manifest. A freshly written manifest is
    restricted on its temporary file before publication by ``write_manifest``.

    This broader trust-chain repair runs only after the storage ownership/refusal
    decisions. A pre-existing workspace root is secured separately, after its
    lifetime lock is acquired but before SQLite vetting, because its transient WAL,
    shared-memory and journal names cannot safely live in a directory writable by
    another local principal. Busy refusal still changes no ACL; identity or foreign
    database refusal may therefore leave only that workspace-root security repair.
    Newly created directories are handled separately, at their creating syscall,
    because there is no prior ACL on those to preserve.
    """
    if not _WINDOWS_OWNER_CONTROL:
        return
    home = installation_root.parent
    directories: tuple[Path, ...]
    if layout.root == home / "workspace":
        directories = (home, layout.root)
    elif restrict_parent and layout.root.parent == home / "workspaces":
        directories = (home, layout.root.parent, layout.root)
    else:
        directories = (
            (layout.root.parent, layout.root) if restrict_parent else (layout.root,)
        )
    for directory in directories:
        if not _is_real_directory_no_follow(directory):
            raise OSError(
                f"refusing to harden non-directory or reparse point: {directory}"
            )
        restrict_to_owner(directory, directory=True)
    try:
        manifest_exists = os.lstat(layout.manifest_path)
    except FileNotFoundError:
        manifest_exists = None
    if manifest_exists is not None:
        if not _is_real_file_no_follow(layout.manifest_path):
            raise OSError(
                "refusing to harden a non-file or reparse-point workspace manifest"
            )
        restrict_to_owner(layout.manifest_path, directory=False)


def _restrict_windows_sqlite_parent(path: Path) -> None:
    """Secure a pinned SQLite parent before the driver can touch sidecar names.

    WAL, shared-memory and rollback-journal files have to remain deletable by
    SQLite, so they cannot be held with the no-share-delete handles used for stable
    initialization paths. The parent directory is the durable authorization
    boundary instead: once its DACL is owner-only, an untrusted local principal
    cannot create, replace or redirect one of those transient names.
    """
    if not _WINDOWS_OWNER_CONTROL:
        return
    if not _is_real_directory_no_follow(path):
        raise OSError(f"refusing to secure non-directory or reparse point: {path}")
    restrict_to_owner(path, directory=True)
    if not _is_real_directory_no_follow(path):
        raise OSError(f"SQLite parent was replaced while securing it: {path}")
    _pin_current_windows_path(path, directory=True)
    _refresh_current_windows_paths()


def _secure_existing_windows_file(path: Path, *, subject: str) -> None:
    """Quiesce and secure one existing file without following links.

    Securing only the parent does not revoke an already-open outsider handle or an
    explicit file ACL. On Windows a zero-access, zero-share handle proves no reader,
    writer or deleter is already present; while that exact object is pinned, its ACL
    is reduced to the owning OS user. The secured parent then prevents a new
    competing pathname open after this short-lived handle closes.
    """
    if not _WINDOWS_OWNER_CONTROL:
        return
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return
    if not _is_real_file_no_follow(path):
        raise OSError(f"refusing an unsafe {subject}")

    api: Any | None = None
    handle: object | None = None
    if os.name == "nt":  # pragma: no cover - exercised on the hosted Windows row
        api = _windows_path_api()
        handle = api.CreateFileW(
            str(path),
            0,
            0,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        value = _handle_value(handle)
        if value in (None, 0, _INVALID_HANDLE_VALUE):
            raise OSError(f"{subject} is already open")
    try:
        opened = os.lstat(path)
        identity = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
        observed = (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
        if identity != observed or not _is_real_file_no_follow(path):
            raise OSError(f"{subject} changed while opening")
        restrict_to_owner(path, directory=False)
        secured = os.lstat(path)
        if identity != (
            secured.st_dev,
            secured.st_ino,
            stat.S_IFMT(secured.st_mode),
        ) or not _is_real_file_no_follow(path):
            raise OSError(f"{subject} changed while securing")
    finally:
        if api is not None and handle is not None:
            api.CloseHandle(handle)


def _prepare_windows_sqlite_database(path: Path) -> None:
    """Make an existing-or-fresh SQLite namespace safe before its first open."""
    if not _WINDOWS_OWNER_CONTROL:
        return
    # The zero-share directory handle rejects pre-existing access-capable handles
    # before the DACL repair and remains held until every existing child is proved
    # closed and owner-only.  Once released, the repaired DACL prevents an
    # untrusted principal from acquiring a new handle or publishing a sidecar.
    with _quiesce_windows_directory(path.parent):
        _restrict_windows_sqlite_parent(path.parent)
        for candidate in (
            path,
            path.with_name(f"{path.name}-wal"),
            path.with_name(f"{path.name}-shm"),
            path.with_name(f"{path.name}-journal"),
        ):
            _secure_existing_windows_file(
                candidate, subject="SQLite database or sidecar"
            )
        _refresh_current_windows_paths()


def harden_windows_workspace_layout(
    *,
    workspace_root: Path,
    installation_root: Path,
    restrict_parent: bool = False,
) -> None:
    """Apply the managed-client DACL only after every public refusal is decided."""
    _refresh_current_windows_paths()
    _restrict_windows_workspace_layout(
        WorkspaceLayout(root=workspace_root),
        installation_root,
        restrict_parent=restrict_parent,
    )
    _refresh_current_windows_paths()


def _bootstrap(
    *,
    layout: WorkspaceLayout,
    installation_root: Path,
    manifest: WorkspaceManifest,
    minted: bool,
    core_version: str,
    restrict_parent_on_windows: bool,
    harden_windows_on_success: bool,
) -> WorkspaceInitResult:
    """Steps 1 to 6, under the workspace's own lifetime storage lock.

    The lock is the same one `runner.py` takes and it is taken for the same
    reason: this process is about to hold the sole exclusive connection to the
    database. Without it, running `init` against a workspace a service already owns
    would meet that service's exclusive SQLite lock as a busy timeout rather than
    as an answer, and two concurrent inits would race on the substrate.

    **The order below is the whole of "a refusal leaves the data tree as it found
    it", and it is an order rather than a rollback.** The claim covers the three
    refusals that decide *whether this workspace is ours to touch* --
    `WORKSPACE_BUSY`, `WORKSPACE_IDENTITY_MISMATCH`, and the `UNRELATED_DIRECTORY`
    a foreign database earns. Each is decided before the manifest, layout
    directories and installation-state tree are created. On Windows, after the
    lifetime lock is acquired, a pre-existing workspace root is deliberately made
    owner-only before SQLite opens it; this is a security precondition for its
    transient sidecar namespace, not adoption of the database. It does not occur on
    the busy path. It used to run the other way round, and both refusals that then
    existed wrote:

    - `WORKSPACE_BUSY` created ten directories and, on a workspace with no manifest,
      wrote one -- so refusing a workspace a running service owned left a tree
      behind on every attempt.
    - `WORKSPACE_IDENTITY_MISMATCH` did not exist. A workspace whose manifest was
      lost while its database survived took the mint branch, because `layout.exists()`
      asks about the manifest *path*; a fresh `workspace_id` was invented, written to
      disk by `create_workspace`, and only then found to disagree with the database
      by the exclusive open. **The manifest survived the refusal**, so every later
      run refused against it and no shipped command could initialise that
      installation again.

    **`WRITE_FAILURE` is not one of the three, and this paragraph used to imply it
    was** by counting two refusals where there are now three. It is the storage
    layer or the filesystem saying no; it can arrive at any point in the sequence,
    and the last two things it guards -- `create_workspace` and
    `InstallationLayout.create` -- run *after* the database work has succeeded. An
    installation-state root that is a regular file reaches it with a whole workspace
    already on disk, by a route with nothing injected in it:
    `_unrecognised_installation_state` returns early because `root.is_dir()` is
    false, and `InstallationLayout.create` then raises `FileExistsError` trying to
    establish the root itself.
    `test_a_write_failure_is_not_bounded_by_the_reordering_and_says_so` holds that
    open. Ordering is what makes the three refusals above cost nothing; it is not a
    transaction, and no ordering turns a failing `mkdir` into one.

    Three filesystem writes precede all database work on every path, and the claim
    is bounded rather than absolute.

    `layout.root.mkdir(parents=True, exist_ok=True)` creates the workspace root
    *and every missing parent*. It is nonetheless a no-op on every path reaching one
    of the three refusals, because each of them needs content that was already under
    that root -- a lock holder for `WORKSPACE_BUSY`, a database for the other two --
    so the root existed before this line ran. `WRITE_FAILURE` is again the exception,
    and again it is stated rather than claimed away.

    `layout.database_path.touch()` is no exception, because **a refusal is
    unreachable on the tree where this call is what created it**: a workspace with
    no database cannot hold the wrong workspace, cannot be somebody else's and
    cannot disagree with this manifest. That was an argument about *creation*, and
    the call it defended was unconditional -- `Path.touch` on an existing file is
    `os.utime`, so every refusal moved the mtime and atime of the database it was
    declining to adopt. It is guarded by `exists()` now, and
    `test_a_database_that_is_not_ours_is_refused_rather_than_bootstrapped` pins the
    mtime, because the digest that proves the rest of this paragraph cannot: it
    records mode and content, and a timestamp is neither.

    `locks/` and the lock file inside it are the genuine bound: every refusal here
    is decided under the lock, so one taken on a tree that had no `locks/` leaves
    that directory and its lock file behind. The lock file moves even when `locks/`
    already existed -- `acquire()` writes the holder's pid into it as advisory
    diagnostics -- which is a fact the test digest normalises rather than hides, and
    `test_the_identity_refusal_creates_the_lock_it_needs_and_nothing_else` asserts
    that residue by name on the smallest tree the refusal is reachable on. It is the
    whole of what "as it found it" excludes: no manifest, no layout directory, no
    installation state, and not a byte of an existing database.

    **"As it found it" is bounded by what the metric can see, and the metric is not
    the filesystem.** `_digest` compares each entry's `lstat` mode and its content
    hash, so outside the Windows workspace-root security repair these tests prove
    that no entry was created, removed, retyped, re-permissioned or rewritten.
    Ownership and extended attributes are outside it entirely. Timestamps are
    outside it too, and the mtime the `touch` above used to move is the one case
    pinned by hand rather than by the digest -- atime is not pinned even there,
    because the vet has to read the file and a read moves it.

    **The database's bytes are part of that claim, and making them so is why the vet
    is a separate open.** `journal_mode = WAL` rewrites the header of whatever file
    it opened, so deciding the refusal on the exclusive connection meant a foreign
    database's bytes moved on the way to declining to touch it. Vetting on an
    `enable_wal=False` connection and closing it first leaves them untouched. A
    `READ_ONLY` probe was the obvious alternative and does not work: it cannot
    checkpoint or delete the `-wal`/`-shm` sidecars it creates, so it strands them
    beside a workspace that *is* ours.

    Two residuals, stated rather than papered over.

    Two `init` runs racing on a genuinely fresh tree can have the loser refuse
    `WORKSPACE_BUSY` over a `locks/` the winner made -- and the winner is creating
    the whole workspace anyway.

    An `OSError` writing the manifest *after* the database was successfully
    bootstrapped leaves a database with no manifest; the next run then refuses
    `WORKSPACE_IDENTITY_MISMATCH` naming the workspace the database holds, having
    written nothing. A stuck state that reports itself accurately, rather than one
    that grows worse on every attempt.
    """
    lock_path = layout.locks_path / "storage.lock"
    # Sampled here, before the two `mkdir`s below, because `locks/` is one of the
    # directories the no-op path repairs and the very next line would hide it.
    absent = _absent_directories(layout, installation_root, manifest.workspace_id)
    try:
        workspace_root_created = _ensure_workspace_directory(layout.root)
        locks_path_created = _ensure_workspace_directory(layout.locks_path)
        lock = create_lock(
            lock_path, LockRole.LIFETIME_STORAGE, {"holder": LOCK_HOLDER}
        )
        held = lock.acquire()
        _refresh_current_windows_paths()
    except OSError as failure:
        return _write_failure(layout, manifest, installation_root, failure)
    if not held:
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            refusal=WorkspaceInitRefusal.WORKSPACE_BUSY,
            reason=(
                f"another process holds the storage lock for {layout.root}; stop the "
                "service that owns this workspace and try again"
            ),
            workspace_id=manifest.workspace_id,
            workspace_root=layout.root,
            installation_root=installation_root,
            workspace_format_version=manifest.compatibility.workspace_format_version,
        )

    try:
        try:
            if not workspace_root_created:
                # Stable entries are pinned by the surrounding Windows guard, but
                # SQLite sidecars cannot be: SQLite must be able to delete them.
                # Restrict the containing namespace only after the storage lock is
                # held (so busy is still mutation-free) and before the first
                # connection can create, replace or follow a sidecar name.
                _prepare_windows_sqlite_database(layout.database_path)
                _secure_existing_windows_file(
                    layout.manifest_path, subject="workspace manifest"
                )
            if _WINDOWS_OWNER_CONTROL and not locks_path_created:
                # The lock path had to remain untouched until this acquisition
                # decided the busy result. Once held, its directory can be
                # quiesced and made owner-only before any later child access.
                _secure_existing_windows_directory(layout.locks_path)
            _revalidate_windows_manifest_selection(
                layout,
                manifest=manifest,
                minted=minted,
                core_version=core_version,
            )
            # `open_database` creates a file in `EPHEMERAL` alone, so a fresh
            # workspace needs one to exist before an exclusive open can be asked
            # for. SQLite reads a zero-byte file as an empty database, which is
            # precisely what the pristine branch of `bootstrap_generation_one`
            # requires. This is the second and last thing created ahead of a
            # refusal, and unlike `locks/` it is not an exception to the rule: it
            # runs only when there was no database, and a database that does not
            # exist cannot be the wrong one, cannot be somebody else's and cannot
            # hold a workspace this manifest disagrees with. Every refusal below
            # needs a database that was already there.
            #
            # **The `exists()` guard is what makes that true of the call and not
            # only of the creation, and it was missing.** `Path.touch` on a file
            # that is already there is `os.utime(path, None)`, so running it
            # unconditionally moved the mtime and atime of a database this run was
            # about to refuse to adopt. The refusal was still correct and no byte
            # of the contents changed -- which is exactly why nothing caught it:
            # the digest every refusal test in this suite compares records mode and
            # content, and a timestamp is neither. Under the lifetime storage lock,
            # so the check and the create cannot be raced apart.
            try:
                os.lstat(layout.database_path)
            except FileNotFoundError:
                _create_empty_database_file(layout.database_path)
            else:
                if not _is_real_file_no_follow(layout.database_path):
                    raise OSError("workspace database is not a regular file")
            _refresh_current_windows_paths()
            # Vetted first, through a connection that does not enable WAL. Setting
            # `journal_mode = WAL` rewrites the header of whatever file it opened,
            # so an exclusive open taken *before* the refusal is decided changes a
            # foreign database's bytes on its way to declining to touch it. This
            # open leaves them byte for byte -- and unlike the `READ_ONLY` probe
            # that was tried first, it can still checkpoint and remove the
            # `-wal`/`-shm` sidecars, which a read-only connection may create and
            # then is not permitted to clean up.
            vetting = open_database(
                layout.database_path, OpenMode.EXCLUSIVE_MAINTENANCE, enable_wal=False
            )
            try:
                # The substrate, read out of the database. This is what makes a
                # repeat run idempotent rather than short-circuited: a workspace
                # whose manifest exists but whose database was never bootstrapped is
                # finished here, and that is the state `runner.py` refuses to serve.
                existing = read_workspace_state(vetting)
                # The one thing the substrate row cannot tell us apart: a database
                # with no workspace state that is nonetheless somebody's. Same
                # condition `bootstrap_generation_one`'s pristine branch refuses on,
                # read through the same helper, and the real bootstrap still applies
                # it below -- so drift makes this vet redundant, never wrong.
                populated = existing is None and fingerprint_schema(vetting).tables > 0
            finally:
                vetting.close()
            _refresh_current_windows_paths()

            if existing is not None and existing.workspace_id != manifest.workspace_id:
                return _identity_mismatch(
                    layout, installation_root, existing.workspace_id, minted
                )
            if populated:
                # **Named for what it is, and it used to be `WRITE_FAILURE`.** This
                # reached the caller as the `SchemaCreationRefused` that
                # `bootstrap_generation_one`'s pristine branch raises, caught by the
                # `except` below and reported as "the filesystem said no" -- which
                # the enum's own docstring says is untrue for this class, and which
                # is the same misnaming `WORKSPACE_IDENTITY_MISMATCH` was carved out
                # of. Nothing failed to write. This is R004-10's second refusal
                # arriving one layer down: a directory holding somebody else's data,
                # found by reading the database rather than by listing the root,
                # because `workspace.sqlite` is a *permitted* layout entry and so is
                # invisible to `unexpected_entries()`.
                return WorkspaceInitResult(
                    status=WorkspaceInitStatus.REFUSED,
                    refusal=WorkspaceInitRefusal.UNRELATED_DIRECTORY,
                    reason=(
                        f"{layout.database_path} is a database this build did not "
                        "create and cannot adopt: it already holds tables and no "
                        "OmniVia workspace state. Nothing was written -- "
                        "initialising here would mix a workspace into somebody "
                        "else's data"
                    ),
                    workspace_root=layout.root,
                    installation_root=installation_root,
                )

            connection = open_database(
                layout.database_path, OpenMode.EXCLUSIVE_MAINTENANCE
            )
            try:
                bootstrapped = existing is None
                state = bootstrap_generation_one(
                    connection,
                    workspace_id=manifest.workspace_id,
                    workspace_format_version=(
                        manifest.compatibility.workspace_format_version
                    ),
                    mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                    expect_phase0_baseline=False,
                )
                applied = apply_pending_migrations(
                    connection,
                    mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                    service_instance_id=LOCK_HOLDER,
                    fencing_generation=state.fencing_generation,
                    workspace_id=manifest.workspace_id,
                )
            finally:
                connection.close()
            _refresh_current_windows_paths()

            # The filesystem, last, and that ordering is the repair. Every refusal
            # this function can reach is decided above, so until this line runs
            # there is nothing written to take back -- and it takes no enumeration
            # of what the storage layer refuses to keep it that way, which a
            # pre-check duplicating `bootstrap_generation_one`'s conditions would
            # have needed and would have drifted from.
            if harden_windows_on_success:
                _restrict_windows_workspace_layout(
                    layout,
                    installation_root,
                    restrict_parent=restrict_parent_on_windows,
                )
            if minted:
                if _WINDOWS_OWNER_CONTROL:
                    _ensure_workspace_directory(layout.blobs_path)
                    _ensure_workspace_directory(layout.indexes_path)
                create_workspace(layout.root, manifest)
                _refresh_current_windows_paths()
            else:
                # Repair, not rewrite. A missing `blobs/`, `indexes/` or `locks/`
                # is created; the manifest already on disk is untouched.
                if _WINDOWS_OWNER_CONTROL:
                    for directory in (
                        layout.root,
                        layout.blobs_path,
                        layout.indexes_path,
                        layout.locks_path,
                    ):
                        _ensure_workspace_directory(directory)
                else:
                    layout.create_directories()
            installation = InstallationLayout(root=installation_root)
            if _WINDOWS_OWNER_CONTROL:
                # Use the guard-aware creator here rather than the backup helper's
                # otherwise-equivalent owner-private creation. Every top-level and
                # workspace-specific component joins this call's no-share-delete
                # pin set before a later catalogue, credential or runtime open.
                for directory in (
                    installation.root / BACKUPS_DIR,
                    installation.root / BACKUPS_DIR / manifest.workspace_id,
                    installation.root / ATTEMPTS_DIR,
                    installation.attempts_for(manifest.workspace_id),
                    installation.root / RUNTIME_DIR,
                    installation.runtime_for(manifest.workspace_id),
                    installation.catalogue,
                ):
                    _ensure_workspace_directory(directory)
            else:
                installation.create(manifest.workspace_id)
            _refresh_current_windows_paths()
        except (StorageError, OSError) as failure:
            return _write_failure(layout, manifest, installation_root, failure)
    finally:
        lock.release()

    # **What "changed" counts, and what it used to.** This was `minted or
    # bootstrapped`, which sees the manifest and the workspace-state row and
    # nothing else. The migration ledger is a third thing that moves, and it moves
    # on its own: `bootstrap_generation_one` and `apply_pending_migrations` are
    # separate transactions, so an interruption between them leaves the substrate
    # row committed and every migration pending. A run against that state applied
    # ten migrations and reported "already initialised; nothing was changed".
    #
    # The directories are the fourth, and adding the ledger was not enough. Both
    # `layout.create_directories()` and `InstallationLayout.create()` repair on the
    # no-op path, and neither was counted: deleting `blobs/`, `indexes/` and the
    # whole of `installation-state/` and re-running created nine directories and
    # still answered "already initialised; nothing was changed". `absent` is the
    # sample taken before any of them were made.
    created = minted or bootstrapped
    pending = len(applied)
    changes: list[str] = []
    if pending:
        reported = "" if pending == 1 else "s"
        changes.append(f"applied {pending} pending migration{reported}")
    if absent:
        reported = "y" if len(absent) == 1 else "ies"
        changes.append(f"recreated {len(absent)} missing director{reported}")
    if created:
        reason = f"initialised {manifest.workspace_id} at {layout.root}"
    elif changes:
        reason = (
            f"completed {manifest.workspace_id} at {layout.root}: {', '.join(changes)}"
        )
    else:
        reason = f"{layout.root} is already initialised; nothing was changed"

    return WorkspaceInitResult(
        status=(
            WorkspaceInitStatus.INITIALISED
            if created or changes
            else WorkspaceInitStatus.ALREADY_INITIALISED
        ),
        reason=reason,
        workspace_id=manifest.workspace_id,
        workspace_root=layout.root,
        installation_root=installation_root,
        workspace_format_version=manifest.compatibility.workspace_format_version,
    )


def _absent_directories(
    layout: WorkspaceLayout, installation_root: Path, workspace_id: str
) -> list[Path]:
    """The directories the two `create` calls below would have to make.

    The six leaves those calls are responsible for, and not the parents they make on
    the way: `installation-state/backups/` exists only to hold `<workspace-id>`, so
    counting both would report two repairs for one missing directory.

    The workspace root is not among them. A run that creates it is minting a
    workspace, and "initialised" already says so; on the repeat path the manifest is
    what proved the root was there.
    """
    installation = InstallationLayout(root=installation_root)
    return [
        path
        for path in (
            layout.blobs_path,
            layout.indexes_path,
            layout.locks_path,
            installation_root / BACKUPS_DIR / workspace_id,
            installation.attempts_for(workspace_id),
            installation.runtime_for(workspace_id),
        )
        if not path.is_dir()
    ]


def _identity_mismatch(
    layout: WorkspaceLayout,
    installation_root: Path,
    holder: str,
    minted: bool,
) -> WorkspaceInitResult:
    """The database and the manifest name different workspaces. Nothing was written.

    The identity reported is the *database's*, not the one this run would have
    claimed: it is the recoverable fact, and the one a user needs in order to put
    the right manifest back.
    """
    claim = (
        "no manifest names it and this run would have created a new workspace here"
        if minted
        else f"but {layout.manifest_path} names another"
    )
    return WorkspaceInitResult(
        status=WorkspaceInitStatus.REFUSED,
        refusal=WorkspaceInitRefusal.WORKSPACE_IDENTITY_MISMATCH,
        reason=(
            f"{layout.database_path} already holds workspace {holder}: {claim}. "
            "Nothing was written -- restore that workspace's manifest, or pass a "
            "different location"
        ),
        workspace_id=holder,
        workspace_root=layout.root,
        installation_root=installation_root,
    )


def _write_failure(
    layout: WorkspaceLayout,
    manifest: WorkspaceManifest,
    installation_root: Path,
    failure: Exception,
) -> WorkspaceInitResult:
    """A refusal from the storage layer, carried out rather than raised.

    The database is left as it was found: every write above is inside one
    `BEGIN IMMEDIATE` transaction, so a failure rolls back to the prior state --
    an unbootstrapped database, or the substrate a previous run committed.
    """
    return WorkspaceInitResult(
        status=WorkspaceInitStatus.REFUSED,
        refusal=WorkspaceInitRefusal.WRITE_FAILURE,
        reason=f"{layout.root} could not be initialised: {failure}",
        workspace_id=manifest.workspace_id,
        workspace_root=layout.root,
        installation_root=installation_root,
        workspace_format_version=manifest.compatibility.workspace_format_version,
    )


def render_result(result: WorkspaceInitResult) -> str:
    """The result as the one document this mode writes to stdout."""
    return json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"


__all__ = [
    "DEFAULT_WORKSPACE_NAME",
    "WORKSPACE_FORMAT_VERSION",
    "WORKSPACE_INIT_VERSION",
    "WorkspaceInitRefusal",
    "WorkspaceInitResult",
    "WorkspaceInitStatus",
    "harden_windows_workspace_layout",
    "initialise_allocated_workspace",
    "initialise_workspace",
    "render_result",
]
