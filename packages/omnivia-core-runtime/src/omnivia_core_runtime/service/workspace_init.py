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

**And a refusal leaves the tree as it found it.** That is a stronger claim than
"nothing is overwritten" and it used not to hold. `init` wrote the manifest and the
installation-state tree first and consulted the database last, so a workspace whose
manifest was lost while its database survived had a *fresh* `workspace_id` minted
and written -- `layout.exists()` asks about the manifest path -- before the
exclusive open discovered it disagreeing. The refusal was correct and the manifest
it had just written stayed, so every later run refused too and no shipped command
could initialise that installation again. The order above is the fix, and it is an
order rather than a rollback: no manifest, no layout directory and no installation
state is written until the database work has succeeded, so there is nothing to take
back and no window in which a crash could leave a half-taken-back tree.
`WORKSPACE_BUSY` was the same defect with a different trigger -- it created ten
directories on its way to refusing -- and the same reordering closes it.

**The claim is bounded, and `_bootstrap`'s docstring states the bound rather than
rounding it up.** It covers the three refusals that decide whether this workspace is
ours to touch, and not `WRITE_FAILURE`, which the storage layer or the filesystem
can raise after a manifest is already on disk. What those three leave behind is
`workspace/locks/` and the lock file they were decided under.

**What "changed" counts.** Four things move, not one, and all four are reported.
The manifest and the substrate row were always counted. `apply_pending_migrations`
is the third and moves on its own, so a run that applied every pending migration to
an interrupted bootstrap used to answer "already initialised; nothing was changed".
The fourth is the filesystem: both `create_directories()` and
`InstallationLayout.create()` repair on the repeat path, and a run that recreated
`blobs/`, `indexes/` and the whole installation-state tree gave that same answer
until `_absent_directories` started counting them.

**This starts no service.** `init` establishes state; `start` or MCP managed start
establishes the process.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Final

from omnivia_core.workspace.compatibility import evaluate_compatibility
from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest
from omnivia_core_runtime.ownership.locks import LockRole, create_lock
from omnivia_core_runtime.storage.backup import (
    ATTEMPTS_DIR,
    BACKUPS_DIR,
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
WORKSPACE_INIT_VERSION: Final = "1.0"

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

#: The only top-level entries an installation-state root may hold. Anything else
#: means this directory is not one of ours, and `InstallationLayout` is the single
#: source of all three names.
INSTALLATION_ENTRIES: Final = frozenset({BACKUPS_DIR, ATTEMPTS_DIR, RUNTIME_DIR})

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

    `WORKSPACE_IDENTITY_MISMATCH` is the newest and was carved out of
    `WRITE_FAILURE`, which is where it used to surface. Two reasons, and the second
    is the one that mattered: nothing failed to write, so a name meaning "the
    filesystem said no" was untrue; and the state it names has its own remedy --
    restore the manifest the database's workspace had, or point `--workspace`
    somewhere else -- which is not the remedy for a full disk.

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
    """

    INCOMPATIBLE_MANIFEST = "incompatible_manifest"
    UNRELATED_DIRECTORY = "unrelated_directory"
    UNRECOGNISED_INSTALLATION_STATE = "unrecognised_installation_state"
    WORKSPACE_IDENTITY_MISMATCH = "workspace_identity_mismatch"
    WORKSPACE_BUSY = "workspace_busy"
    WRITE_FAILURE = "write_failure"


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
    if layout.exists():
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
    )


def _unrecognised_installation_state(root: Path) -> str | None:
    """The foreign entries an existing installation-state root holds, if any.

    An absent root is not unrecognised -- it is what a fresh machine has, and
    creating it is this command's job. What is refused is a directory that exists
    and is plainly something else, because writing `backups/`, `attempts/` and
    `runtime/` into it would adopt it.
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
    return ", ".join(foreign) if foreign else None


def _chosen_by_somebody(root: Path, names: Iterable[str]) -> list[str]:
    """`names` under `root` without the entries no person put there.

    `root` is needed because two of the three rules are about what the entry *is*
    and not only what it is called. See `OS_GENERATED_ENTRIES`.
    """
    return [name for name in names if not _os_generated(root / name)]


def _os_generated(entry: Path) -> bool:
    """Whether the operating system, rather than a person, put this here.

    The two prefix rules are checked against the entry's type, and both directions
    of that test are load-bearing. An AppleDouble sidecar is a file, so
    `._my_private_repo/` -- a name that looks like litter and holds a `.git` -- is
    somebody's, not ours to initialise into. A freedesktop trash is a directory.
    Anything failing its own type test falls through to being somebody's, which is
    the safe direction: the cost is a refusal a person can act on, and the cost of
    the other direction is a workspace mixed into their files.
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


def _new_manifest(core_version: str) -> WorkspaceManifest:
    """A manifest for a workspace that does not exist yet.

    `min_core_version` is the build doing the creating, which is the honest claim:
    this workspace was made by Core `core_version` and an older build should not
    open it for writing. No upper bound is declared -- a workspace that excluded
    future builds by default would need re-writing to stay openable.
    """
    return WorkspaceManifest(
        workspace_id=f"ws-{uuid.uuid4()}",
        created_at=datetime.now(UTC).isoformat(),
        name=DEFAULT_WORKSPACE_NAME,
        compatibility=CoreCompatibility(
            workspace_format_version=WORKSPACE_FORMAT_VERSION,
            min_core_version=core_version,
        ),
    )


def _bootstrap(
    *,
    layout: WorkspaceLayout,
    installation_root: Path,
    manifest: WorkspaceManifest,
    minted: bool,
) -> WorkspaceInitResult:
    """Steps 1 to 6, under the workspace's own lifetime storage lock.

    The lock is the same one `runner.py` takes and it is taken for the same
    reason: this process is about to hold the sole exclusive connection to the
    database. Without it, running `init` against a workspace a service already owns
    would meet that service's exclusive SQLite lock as a busy timeout rather than
    as an answer, and two concurrent inits would race on the substrate.

    **The order below is the whole of "a refusal leaves the tree as it found it",
    and it is an order rather than a rollback.** The claim covers the three refusals
    that decide *whether this workspace is ours to touch* -- `WORKSPACE_BUSY`,
    `WORKSPACE_IDENTITY_MISMATCH`, and the `UNRELATED_DIRECTORY` a foreign database
    earns. Each is decided before the manifest, the layout directories and the
    installation-state tree are created, so there is nothing to undo and no window
    in which a crash could leave a half-undone one behind. It used to run the other
    way round, and both refusals that then existed wrote:

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
    false, and `InstallationLayout.create` then raises `NotADirectoryError`.
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
    hash, so what these tests prove is that no entry was created, removed, retyped,
    re-permissioned or rewritten. Ownership and extended attributes are outside it
    entirely. Timestamps are outside it too, and the mtime the `touch` above used to
    move is the one case pinned by hand rather than by the digest -- atime is not
    pinned even there, because the vet has to read the file and a read moves it.

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
        layout.root.mkdir(parents=True, exist_ok=True)
        layout.locks_path.mkdir(exist_ok=True)
        lock = create_lock(lock_path, LockRole.LIFETIME_STORAGE, {"holder": LOCK_HOLDER})
        held = lock.acquire()
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
            if not layout.database_path.exists():
                layout.database_path.touch()
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

            # The filesystem, last, and that ordering is the repair. Every refusal
            # this function can reach is decided above, so until this line runs
            # there is nothing written to take back -- and it takes no enumeration
            # of what the storage layer refuses to keep it that way, which a
            # pre-check duplicating `bootstrap_generation_one`'s conditions would
            # have needed and would have drifted from.
            if minted:
                create_workspace(layout.root, manifest)
            else:
                # Repair, not rewrite. A missing `blobs/`, `indexes/` or `locks/`
                # is created; the manifest already on disk is untouched.
                layout.create_directories()
            InstallationLayout(root=installation_root).create(manifest.workspace_id)
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
    "initialise_workspace",
    "render_result",
]
