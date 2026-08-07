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

**The sequence, established against the code rather than taken on trust.**

    1. `create_workspace(root, manifest)` -- the portable five-path layout and an
       atomically written `workspace.json`. It creates **no database**.
    2. `InstallationLayout(installation_root).create(workspace_id)` -- the
       installation-local backups, attempts and runtime directories.
    3. Create the database *file*. This step is easy to miss and it is why the
       obvious sequence does not run: `open_database` only creates a file in
       `OpenMode.EPHEMERAL` -- `may_create` is that mode and no other -- so
       opening a fresh workspace `SERVICE_OWNED` raises `StorageError: no
       workspace database at ...` instead of bootstrapping one.
    4. `open_database(..., OpenMode.EXCLUSIVE_MAINTENANCE)`. Exclusive, which is
       what `bootstrap_generation_one` requires, and *not* `SERVICE_OWNED`: this
       process is not a service, does not advertise readiness and does not hold
       the workspace for a lifetime. It is the same mode `migrate_legacy_database`
       uses for the same reason.
    5. `bootstrap_generation_one(..., expect_phase0_baseline=False)` -- the
       pristine branch, which materialises the frozen Phase 0 scaffolding so the
       migrations from 0002 onward have the tables they add triggers to.
    6. `apply_pending_migrations(...)`.

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

**This starts no service.** `init` establishes state; `start` or MCP managed start
establishes the process.
"""

from __future__ import annotations

import json
import uuid
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

    The first three are the cases R004-10 names by hand. The last two cover the
    ways the sequence can stop once it has started, and are separate names rather
    than one bucket because an adapter's advice differs: a busy workspace means a
    service already owns it, and a write failure means the filesystem said no.
    """

    INCOMPATIBLE_MANIFEST = "incompatible_manifest"
    UNRELATED_DIRECTORY = "unrelated_directory"
    UNRECOGNISED_INSTALLATION_STATE = "unrecognised_installation_state"
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
        unrelated = layout.unexpected_entries()
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
    foreign = sorted(
        entry.name for entry in root.iterdir() if entry.name not in INSTALLATION_ENTRIES
    )
    return ", ".join(foreign) if foreign else None


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
    """
    if minted:
        create_workspace(layout.root, manifest)
    else:
        # Repair, not rewrite. A missing `blobs/`, `indexes/` or `locks/` is
        # created; the manifest already on disk is untouched.
        layout.create_directories()
    InstallationLayout(root=installation_root).create(manifest.workspace_id)

    lock = create_lock(
        layout.locks_path / "storage.lock",
        LockRole.LIFETIME_STORAGE,
        {"holder": LOCK_HOLDER},
    )
    try:
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
        # `open_database` creates a file in `EPHEMERAL` alone, so a fresh workspace
        # needs one to exist before an exclusive open can be asked for. SQLite reads
        # a zero-byte file as an empty database, which is precisely what the pristine
        # branch of `bootstrap_generation_one` requires.
        layout.database_path.touch()
        connection = open_database(layout.database_path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            # The substrate, read out of the database. This is what makes a repeat
            # run idempotent rather than short-circuited: a workspace whose manifest
            # exists but whose database was never bootstrapped is finished here,
            # and that is the state `runner.py` refuses to serve.
            bootstrapped = read_workspace_state(connection) is None
            state = bootstrap_generation_one(
                connection,
                workspace_id=manifest.workspace_id,
                workspace_format_version=(
                    manifest.compatibility.workspace_format_version
                ),
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=False,
            )
            apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=LOCK_HOLDER,
                fencing_generation=state.fencing_generation,
                workspace_id=manifest.workspace_id,
            )
        finally:
            connection.close()
    except (StorageError, OSError) as failure:
        return _write_failure(layout, manifest, installation_root, failure)
    finally:
        lock.release()

    created = minted or bootstrapped
    return WorkspaceInitResult(
        status=(
            WorkspaceInitStatus.INITIALISED
            if created
            else WorkspaceInitStatus.ALREADY_INITIALISED
        ),
        reason=(
            f"initialised {manifest.workspace_id} at {layout.root}"
            if created
            else f"{layout.root} is already initialised; nothing was changed"
        ),
        workspace_id=manifest.workspace_id,
        workspace_root=layout.root,
        installation_root=installation_root,
        workspace_format_version=manifest.compatibility.workspace_format_version,
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
