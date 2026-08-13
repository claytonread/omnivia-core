"""Service-owned workspace bootstrap (R004-10, owner resolution 004 Packet B).

Owner resolution 004 §11 lists five things `omnivia init` has to be shown to do.
Three of them are claims about *this* module -- the workspace becomes bootstrapped,
repeating is safe, and nothing existing is overwritten -- and they are exercised
here as function calls, because the thing under test is a filesystem and database
transformation rather than a process. The other two are claims about the CLI and
about `start`, and they live in `packages/omnivia-core-cli/tests/test_init.py`,
which drives real processes.

**The load-bearing case is `test_idempotence_is_read_from_the_database`.** Deciding
"already initialised" from `workspace.json` existing would pass every other test in
this file while reporting success for a workspace whose database was never
bootstrapped -- which is exactly the state `runner.py` refuses to serve with
"workspace has no ownership substrate". So the second run is made against a
workspace whose manifest is intact and whose database is gone, and it has to notice.

**Every refusal is checked twice**: that it refused, and that what was already on
disk is byte-for-byte what it was. A refusal that reported correctly after
truncating a file would pass a status assertion on its own.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.ownership import locks as locks_module
from omnivia_core_runtime.service.workspace_init import (
    WORKSPACE_FORMAT_VERSION,
    WORKSPACE_INIT_VERSION,
    WorkspaceInitRefusal,
    WorkspaceInitResult,
    WorkspaceInitStatus,
    initialise_allocated_workspace,
    initialise_workspace,
)
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    load_migrations,
    read_workspace_state,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.workspace.compatibility import SUPPORTED_WORKSPACE_FORMATS
from omnivia_core.workspace.manifest import MANIFEST_VERSION


def _init(home: Path) -> WorkspaceInitResult:
    """Initialise the deterministic default layout beneath one home."""
    return initialise_workspace(
        workspace_root=home / "workspace",
        installation_root=home / "installation-state",
    )


#: The lock file every refusal below is decided under.
LOCK_PAYLOAD = "workspace/locks/storage.lock"

#: The payload's one genuinely volatile field. `_BaseFileLock._write_payload`
#: records the holder's pid as advisory diagnostics each time the lock is taken --
#: ownership is decided by the OS lock and never by reading this file -- so two
#: runs in different processes write different bytes for identical state.
VOLATILE_LOCK_FIELD = "pid"

#: What a refusal may leave behind, and the whole of it. Every refusal `_bootstrap`
#: decides is decided under the lifetime storage lock, so one taken on a tree that
#: had no `locks/` creates that directory and the lock file inside it. Named once
#: here rather than restated in each test, and *asserted* rather than filtered out
#: of the comparison.
LOCK_RESIDUE = frozenset({"workspace/locks", LOCK_PAYLOAD})


def _lock_state(path: Path) -> str:
    """The lock payload with its pid removed, or its bytes if it is not one.

    **`_digest` used to skip this path outright, and that made the flagship
    assertion vacuous on the identity-mismatch tree.** That refusal acquires the
    lock *successfully*, so `_write_payload` truncates and rewrites this file on
    every refusing run -- proved across two processes by the pid changing from one
    to the next. It was the only entry those runs wrote, and it was the one entry
    filtered out of the metric, so `_digest(tmp_path) == before` held for a reason
    unconnected to the property.

    Normalising the pid rather than skipping the file keeps the exclusion's real
    justification -- a successful second init in another process must not read as a
    changed workspace -- while leaving the file's existence, mode, role and holder
    inside the comparison, where a refusal that deleted the lock, adopted it under
    another holder, or created one on a tree that had none is now visible.

    **That justification is a claim about two processes, and for a while nothing
    exercised it.** Every test in this file called `initialise_workspace` in-process,
    so the pid written by one run and the next was `os.getpid()` both times and never
    differed. Replacing this function's body with a plain `sha256(path.read_bytes())`
    left all 42 tests green, and so did pointing `VOLATILE_LOCK_FIELD` at a key the
    payload does not have. `test_two_refusing_processes_differ_only_by_the_lock_pid`
    is what closes that: it runs two real refusing `init` processes, asserts their
    pids differ and that the lock file's raw bytes really moved, and then asserts the
    digest reads the tree as unchanged. Without the normalisation that last assertion
    fails.

    Anything that is not a JSON object is digested whole: an empty lock file, which
    is what a *failed* acquire leaves, is a value here rather than an error.
    """
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return hashlib.sha256(raw).hexdigest()
    return json.dumps(
        {key: value for key, value in payload.items() if key != VOLATILE_LOCK_FIELD},
        sort_keys=True,
    )


def _digest(root: Path) -> dict[str, str]:
    """Every entry under `root`, by relative path, mode and content.

    The whole tree rather than one file: "non-destructive" is a claim about
    everything that was there, and a refusal that removed a sibling while leaving
    the file a test happened to name would satisfy a narrower check.

    **Directories, symlinks and modes are in it, and that is the repair.** This
    used to filter on `path.is_file()`, which made an empty directory invisible:
    a refusal that created the whole installation-state tree and the three
    workspace subdirectories on its way to refusing compared equal to one that
    created nothing, and every "nothing was written" assertion in this file passed
    under it. `lstat().st_mode` carries the entry's type and permissions together,
    and it is `lstat` rather than `stat` so a dangling symlink is a value here
    rather than an error.

    **Nothing is excluded.** The lock payload was, and `_lock_state` says what that
    cost; it is normalised now instead, so every path under `root` is in the
    comparison and what a refusal legitimately writes is asserted by name.

    **It is a mode-and-content metric, not a filesystem one, and "nothing was
    written" means exactly that much.** What an equal digest proves is that no entry
    was created, removed, retyped, re-permissioned or rewritten. Three things are
    outside it. Timestamps: an unconditional `Path.touch()` moved a refused
    database's mtime and atime for as long as it existed with every assertion in
    this file green, which is why
    `test_a_database_that_is_not_ours_is_refused_rather_than_bootstrapped` pins that
    mtime by hand. Ownership: uid and gid are not read. Extended attributes and ACLs:
    not read either. Adding timestamps to the digest wholesale is not the fix -- the
    lock file's mtime moves on every refusal by design, so it would need an exclusion
    exactly where `_lock_state` exists to avoid one.
    """
    entries: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if path.is_symlink():
            content = f"-> {os.readlink(path)}"
        elif path.is_dir():
            content = "directory"
        elif name == LOCK_PAYLOAD:
            content = _lock_state(path)
        else:
            content = hashlib.sha256(path.read_bytes()).hexdigest()
        entries[name] = f"{path.lstat().st_mode:o} {content}"
    return entries


def _changed(before: dict[str, str], after: dict[str, str]) -> set[str]:
    """Every entry the two digests disagree about -- created, removed or rewritten."""
    return {name for name in set(before) | set(after) if before.get(name) != after.get(name)}


def _applied(layout: WorkspaceLayout) -> set[int]:
    """The migration versions the ledger records, read read-only."""
    connection = open_database(layout.database_path, OpenMode.READ_ONLY)
    try:
        return set(applied_migrations(connection))
    finally:
        connection.close()


def _shipped() -> set[int]:
    return {migration.version for migration in load_migrations()}


def _substrate(layout: WorkspaceLayout) -> tuple[str, int] | None:
    """The workspace state the database itself holds, read read-only."""
    if not layout.database_path.is_file():
        return None
    connection = open_database(layout.database_path, OpenMode.READ_ONLY)
    try:
        state = read_workspace_state(connection)
    finally:
        connection.close()
    return None if state is None else (state.workspace_id, state.fencing_generation)


# ---------------------------------------------------------------------------
# A fresh default home becomes startable
# ---------------------------------------------------------------------------


def test_a_fresh_workspace_gets_the_ownership_substrate_a_service_needs(
    tmp_path: Path,
) -> None:
    """§11: a fresh default home becomes startable, at the storage level.

    `runner.py` reads `read_workspace_state(connection)` and raises "workspace has
    no ownership substrate; migrate it before serving" when it is `None`. That row,
    and the full migration ledger beneath it, is what makes the difference between
    a directory and a workspace -- so it is asserted here rather than inferred from
    the command having exited 0. The end-to-end proof that a service really comes
    up on it is `test_init.py`'s.
    """
    result = _init(tmp_path)
    assert result.status is WorkspaceInitStatus.INITIALISED
    assert result.refusal is None
    assert result.workspace_id is not None

    layout = WorkspaceLayout(root=tmp_path / "workspace")
    # The portable five-path form, with the database, and nothing foreign in it.
    assert layout.validate(require_database=True) == []

    substrate = _substrate(layout)
    assert substrate == (result.workspace_id, 1)

    # Every shipped migration, not merely the substrate: a workspace missing one
    # would start and then fail on the first operation that needs its table.
    assert _applied(layout) == _shipped()


def test_the_installation_state_root_is_created_for_the_new_workspace(
    tmp_path: Path,
) -> None:
    """The runtime directory `start` publishes its descriptor into has to exist.

    `InstallationLayout.create` is the second half of "startable" and is easy to
    leave out, because the workspace alone looks complete without it.
    """
    result = _init(tmp_path)
    assert result.workspace_id is not None
    installation = tmp_path / "installation-state"
    for name in ("backups", "attempts", "runtime"):
        assert (installation / name / result.workspace_id).is_dir()


def test_init_starts_no_service_and_advertises_nothing(tmp_path: Path) -> None:
    """R004-10: `init` establishes state; `start` establishes the process.

    The last two assertions replace `assert not (tmp_path / "run").exists()`, which
    could not fail: `run/` is the *CLI's* convention, named nowhere in the runtime,
    so nothing this function calls could ever create one and the assertion held for
    a reason unconnected to the property. What is asserted instead is falsifiable
    over the whole tree in the two ways this claim can break -- a bound socket
    anywhere, and a published descriptor anywhere -- rather than the absence of one
    named directory a service would not have used.

    The end-to-end proof that no *process* results is `test_init.py`'s
    `_service_pids`, which is unchanged and stays the stronger evidence.
    """
    result = _init(tmp_path)
    assert result.workspace_id is not None
    runtime = tmp_path / "installation-state" / "runtime" / result.workspace_id
    assert runtime.is_dir()
    # No descriptor, so nothing discovers a service, and no socket was bound.
    assert list(runtime.iterdir()) == []
    assert [path for path in tmp_path.rglob("*") if path.is_socket()] == []
    assert list(tmp_path.rglob("service.json")) == []


# ---------------------------------------------------------------------------
# Repeating init is safe and idempotent
# ---------------------------------------------------------------------------


def test_repeating_init_changes_nothing_at_all(tmp_path: Path) -> None:
    """§11: repeating init is safe and idempotent.

    Identity is the part that would hurt most if it moved: a second run that minted
    a fresh `workspace_id` would leave the descriptor, the runtime directory and the
    database state row all naming a workspace the manifest no longer describes.
    """
    first = _init(tmp_path)
    before = _digest(tmp_path)

    second = _init(tmp_path)
    assert second.status is WorkspaceInitStatus.ALREADY_INITIALISED
    assert second.refusal is None
    assert second.workspace_id == first.workspace_id
    assert _digest(tmp_path) == before


def test_idempotence_is_read_from_the_database_not_from_the_manifest(
    tmp_path: Path,
) -> None:
    """The one that fails if "already initialised" is a path check.

    A manifest on disk does not mean a workspace a service can own: `create_workspace`
    writes the manifest and creates no database at all, so the half-state this
    reconstructs is reachable by any interruption between the two. If this command
    answered "already initialised" to it, `omnivia start` would then refuse the
    workspace `omnivia init` had just called done.
    """
    first = _init(tmp_path)
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    layout.database_path.unlink()
    assert layout.manifest_path.is_file()
    assert _substrate(layout) is None

    second = _init(tmp_path)
    # Finished, not skipped -- and under the identity the surviving manifest names.
    assert second.status is WorkspaceInitStatus.INITIALISED
    assert second.workspace_id == first.workspace_id
    assert _substrate(layout) == (first.workspace_id, 1)


def test_a_database_whose_manifest_is_gone_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    """The refusal that used to write, and permanently brick the workspace.

    `layout.exists()` is a check on the manifest *path*, so a workspace whose
    manifest was lost while its database survived took the mint branch: a fresh
    `workspace_id` was invented, `create_workspace` wrote it to disk, and only then
    did the exclusive open find it disagreeing with the database. The refusal itself
    was right; what was wrong is that the manifest it had just written survived it,
    so every later `init` refused against a manifest no user had ever asked for.
    Recoverable by hand before the first `omnivia init`, and un-initialisable by any
    shipped command after it.

    The identity is now compared before anything is written, which is why this is a
    refusal rather than a rollback: there is nothing to roll back. The run is
    repeated because the defect was not the first refusal but the second -- one run
    that refuses cleanly proves nothing about a state the run itself created.
    """
    first = _init(tmp_path)
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    layout.manifest_path.unlink()
    before = _digest(tmp_path)

    for attempt in (1, 2):
        result = _init(tmp_path)
        assert result.status is WorkspaceInitStatus.REFUSED, attempt
        assert result.refusal is WorkspaceInitRefusal.WORKSPACE_IDENTITY_MISMATCH
        # The database's own identity, named so it can be recovered by hand -- and
        # not an identity this run minted, which is what used to reach disk.
        assert first.workspace_id is not None
        assert first.workspace_id in result.reason
        assert not layout.manifest_path.exists()
        assert _changed(before, _digest(tmp_path)) == set(), attempt


def test_the_identity_refusal_creates_the_lock_it_needs_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The bound on "leaves the tree as it found it", asserted rather than asserted away.

    The test above starts from a workspace `init` itself created, so `locks/`, the
    lock file, `blobs/`, `indexes/` and the whole installation-state tree already
    exist and re-creating them is a no-op -- it cannot distinguish "created nothing"
    from "created everything that was already there". This starts from the tree a
    tar, a zip or a `git restore` produces, since all three drop empty directories:
    a surviving `workspace.sqlite`, no manifest, and no `locks/`.

    On that tree the run really does create something, and `LOCK_RESIDUE` is the
    whole of it. Everything else is named absent, because those are the entries
    whose creation was the original defect.

    The digest is rooted at `home` rather than at `tmp_path`, and that is not a
    detail. `LOCK_PAYLOAD` is a path relative to a home, so digesting a directory
    one level above it produced `home/workspace/locks/storage.lock`, which matches
    nothing -- `_lock_state` never ran and the lock file was hashed as an ordinary
    file. A mutation that put the old `continue` back left this test green. The
    assertion that the entry is in the digest at all is what keeps that from
    recurring: it is the whole point of the repair, so it is stated rather than
    assumed.
    """
    seed = tmp_path / "seed"
    assert _init(seed).status is WorkspaceInitStatus.INITIALISED
    database = (seed / "workspace" / "workspace.sqlite").read_bytes()
    shutil.rmtree(seed)

    home = tmp_path / "home"
    layout = WorkspaceLayout(root=home / "workspace")
    layout.root.mkdir(parents=True)
    layout.database_path.write_bytes(database)
    before = _digest(home)

    result = initialise_workspace(
        workspace_root=layout.root,
        installation_root=home / "installation-state",
    )
    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_IDENTITY_MISMATCH

    after = _digest(home)
    # The file the refusal writes is *in* the metric, which is the repair itself.
    assert LOCK_PAYLOAD in after
    assert _changed(before, after) == LOCK_RESIDUE
    assert not layout.manifest_path.exists()
    assert not layout.blobs_path.exists()
    assert not layout.indexes_path.exists()
    assert not (home / "installation-state").exists()


#: One refusing `init`, as a separate programme. The pid in the lock payload is
#: only volatile across a process boundary, so the normalisation `_lock_state`
#: performs cannot be exercised by any in-process call.
#:
#: It reports the module it actually imported. `omnivia_core_runtime` resolves
#: through the environment's installed distribution rather than through this file's
#: location, so a child interpreter can silently import a *different* checkout's
#: copy and report a clean result for code nobody edited. Asserted below rather
#: than assumed.
REFUSING_INIT = """
import json
import os
import sys
from pathlib import Path

from omnivia_core_runtime.service import workspace_init

home = Path(sys.argv[1])
result = workspace_init.initialise_workspace(
    workspace_root=home / "workspace",
    installation_root=home / "installation-state",
)
print(
    json.dumps(
        {
            "refusal": None if result.refusal is None else result.refusal.value,
            "pid": os.getpid(),
            "module": workspace_init.__file__,
        }
    )
)
"""


def _refuse_in_a_child(home: Path) -> dict[str, object]:
    """Run one `init` against `home` in its own interpreter and report what it did."""
    completed = subprocess.run(
        [sys.executable, "-c", REFUSING_INIT, str(home)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    reported: dict[str, object] = json.loads(completed.stdout)
    # The child imported the module this test is about, and not another checkout's.
    assert reported["module"] == sys.modules[initialise_workspace.__module__].__file__
    return reported


def test_two_refusing_processes_differ_only_by_the_lock_pid(tmp_path: Path) -> None:
    """The one claim `_lock_state` exists for, and the only test that can make it.

    `_lock_state` drops the pid from the lock payload before digesting it, and its
    justification is that a refusing run in *another* process must not read as a
    changed workspace. Every other test here calls `initialise_workspace` in-process,
    where `os.getpid()` is the same integer on every run -- so the normalisation was
    inert. Gutting it to `sha256(path.read_bytes())`, or pointing
    `VOLATILE_LOCK_FIELD` at a key that is not in the payload, left all 42 tests
    green. A helper nothing exercises is a claim nothing checks.

    The tree is the one `test_the_identity_refusal_creates_the_lock_it_needs_and_nothing_else`
    builds: a surviving `workspace.sqlite`, no manifest, no `locks/`. That refusal
    *acquires* the lock successfully, which is what makes it rewrite the payload --
    a refusal that failed to acquire would leave the file alone and prove nothing.

    Three assertions, and the middle one is what stops this passing vacuously. The
    pids differ, so the premise holds. The lock file's raw bytes differ, so there is
    genuinely something for the normalisation to absorb. And the digest reads the
    tree as unchanged -- which is false the moment `_lock_state` stops removing the
    pid.
    """
    seed = tmp_path / "seed"
    assert _init(seed).status is WorkspaceInitStatus.INITIALISED
    database = (seed / "workspace" / "workspace.sqlite").read_bytes()
    shutil.rmtree(seed)

    home = tmp_path / "home"
    layout = WorkspaceLayout(root=home / "workspace")
    layout.root.mkdir(parents=True)
    layout.database_path.write_bytes(database)
    lock = layout.locks_path / "storage.lock"

    first = _refuse_in_a_child(home)
    before, raw_before = _digest(home), lock.read_bytes()
    second = _refuse_in_a_child(home)
    after, raw_after = _digest(home), lock.read_bytes()

    assert first["refusal"] == WorkspaceInitRefusal.WORKSPACE_IDENTITY_MISMATCH.value
    assert second["refusal"] == first["refusal"]
    # Two real processes, so the pid is genuinely volatile here and nowhere else.
    assert first["pid"] != second["pid"]
    assert json.loads(raw_after)["pid"] == second["pid"]
    # The file really was rewritten. Without this the assertion below would hold
    # for a lock nobody touched.
    assert raw_before != raw_after
    # And the metric says the workspace is as it was, which is the property.
    assert _changed(before, after) == set()


def test_a_manifest_naming_another_workspace_is_refused_and_says_which(
    tmp_path: Path,
) -> None:
    """`_identity_mismatch`'s `minted=False` sentence, which no test reached.

    Both branches are production-reachable and they say different things. The
    `minted=True` half -- no manifest at all -- is covered by
    `test_a_database_whose_manifest_is_gone_is_refused_before_anything_is_written`.
    This is the other half: a manifest that is present, well-formed and compatible,
    and names a workspace the database does not hold. Copying one workspace's
    `workspace.json` beside another's database produces it.

    The database's identity is the one reported, because it is the recoverable
    fact; the manifest's is what the user has to replace.
    """
    first = _init(tmp_path)
    assert first.workspace_id is not None
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    manifest = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
    manifest["workspace_id"] = "ws-from-another-workspace"
    layout.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = _digest(tmp_path)

    result = _init(tmp_path)

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_IDENTITY_MISMATCH
    # The database's identity, and the manifest named as the thing that disagrees.
    assert result.workspace_id == first.workspace_id
    assert first.workspace_id in result.reason
    assert str(layout.manifest_path) in result.reason
    assert _changed(before, _digest(tmp_path)) == set()


def test_a_write_failure_is_not_bounded_by_the_reordering_and_says_so(
    tmp_path: Path,
) -> None:
    """The residual the ordering claim does *not* cover, pinned so it cannot be forgotten.

    `_bootstrap`'s docstring used to say "both refusals reachable from here are
    decided before the manifest, the layout directories and the installation-state
    tree are created". Three are reachable, and `WRITE_FAILURE` is the third: it is
    the storage layer or the filesystem saying no, it can arrive at any point in the
    sequence, and the last two things it guards -- `create_workspace` and
    `InstallationLayout.create` -- run *after* the database work has succeeded.

    An installation-state root that is a regular file reaches it by a route with no
    mocking in it: `_unrecognised_installation_state` returns early because
    `root.is_dir()` is false, and `InstallationLayout.create` then raises
    `NotADirectoryError` with a whole workspace already on disk. The claim is
    therefore about the three refusals that decide *whether this workspace is ours
    to touch*, and this test is what keeps that qualification honest.
    """
    (tmp_path / "installation-state").write_text("not a directory", encoding="utf-8")

    result = _init(tmp_path)

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WRITE_FAILURE
    # A refusal that wrote a manifest, three directories and a database.
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    assert layout.manifest_path.is_file()
    assert layout.database_path.is_file()
    assert layout.blobs_path.is_dir()


def test_a_busy_workspace_is_refused_before_any_directory_is_created(
    tmp_path: Path,
) -> None:
    """`WORKSPACE_BUSY` used to refuse having written a manifest and ten directories.

    `_bootstrap` ran `create_workspace`, `create_directories` and
    `InstallationLayout.create` and only then took the lock, so a refusal for a
    workspace a service already owns left the whole tree behind it.
    `test_a_workspace_another_process_owns_is_refused_without_waiting` cannot see
    that: it initialises first, so everything already exists and creating it again
    is a no-op.

    Here the only thing on disk is the `locks/` directory the lock file must live
    in. That is the smallest tree in which a lock can be held at all, and it is the
    one shape in which "created nothing" is a claim with content.

    `before` is taken *after* this test's own `acquire()`, because that call is what
    creates the lock file and writes a payload into it. Taking it earlier and then
    excluding the lock from the comparison is what made this assertion vacuous; the
    digest now sees that file, so the baseline has to be the tree `_init` was
    actually handed.
    """
    from omnivia_core_runtime.ownership.locks import LockRole, create_lock

    layout = WorkspaceLayout(root=tmp_path / "workspace")
    layout.locks_path.mkdir(parents=True)

    held = create_lock(layout.locks_path / "storage.lock", LockRole.LIFETIME_STORAGE)
    assert held.acquire()
    before = _digest(tmp_path)
    try:
        result = _init(tmp_path)
    finally:
        held.release()

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_BUSY
    assert _changed(before, _digest(tmp_path)) == set()
    assert not layout.manifest_path.exists()
    assert not (tmp_path / "installation-state").exists()


def test_a_workspace_missing_its_migrations_is_finished_and_reported_as_changed(
    tmp_path: Path,
) -> None:
    """"nothing was changed" used to be emitted after applying every pending migration.

    `bootstrap_generation_one` and `apply_pending_migrations` are separate
    transactions, so a workspace holding the substrate row and none of the
    migrations is reachable by any interruption between them. `created = minted or
    bootstrapped` counted the substrate row alone and never the ledger, so this run
    applied ten migrations and told the user the workspace was already initialised
    and nothing had changed.

    The count is asserted in the reason rather than merely a different status: a
    caller reading the sentence is the one the lie was told to.
    """
    from omnivia_core_runtime.storage.migrations import bootstrap_generation_one
    from omnivia_core_runtime.workspace.manifest_store import create_workspace

    from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

    layout = WorkspaceLayout(root=tmp_path / "workspace")
    manifest = WorkspaceManifest(
        workspace_id="ws-interrupted-between-two-transactions",
        created_at="2026-01-01T00:00:00+00:00",
        name="OmniVia workspace",
        compatibility=CoreCompatibility(
            workspace_format_version=WORKSPACE_FORMAT_VERSION,
            min_core_version="0.1.0",
        ),
    )
    create_workspace(layout.root, manifest)
    layout.database_path.touch()
    connection = open_database(layout.database_path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        bootstrap_generation_one(
            connection,
            workspace_id=manifest.workspace_id,
            workspace_format_version=WORKSPACE_FORMAT_VERSION,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
        )
    finally:
        connection.close()

    pending = _shipped() - _applied(layout)
    assert pending, "the substrate alone must leave migrations pending"

    result = _init(tmp_path)

    assert result.status is WorkspaceInitStatus.INITIALISED
    assert result.refusal is None
    assert "nothing was changed" not in result.reason
    assert f"applied {len(pending)} pending migration" in result.reason
    assert _applied(layout) == _shipped()


def test_a_repeat_run_repairs_a_missing_layout_directory(tmp_path: Path) -> None:
    """Non-destructive includes not refusing to finish its own half-done work.

    **This test used to assert `ALREADY_INITIALISED`, and that made it a guard
    pinning a defect.** That status has one reason string -- "is already initialised;
    nothing was changed" -- so the assertion required the command to repair a
    directory and then deny having done anything. The report is the whole of what a
    caller sees, and it was false.
    """
    _init(tmp_path)
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    manifest_before = layout.manifest_path.read_bytes()
    layout.indexes_path.rmdir()

    result = _init(tmp_path)
    assert result.status is WorkspaceInitStatus.INITIALISED
    assert "nothing was changed" not in result.reason
    assert "recreated 1 missing directory" in result.reason
    assert layout.indexes_path.is_dir()
    # Repaired around the manifest, never over it.
    assert layout.manifest_path.read_bytes() == manifest_before


def test_a_repeat_run_that_repairs_nothing_says_so(tmp_path: Path) -> None:
    """The other side of the count, so "nothing was changed" stays falsifiable.

    Without this, reporting every repeat run as `INITIALISED` would satisfy the test
    above and the idempotence tests would still pass -- `_digest` compares the tree,
    not the sentence.
    """
    _init(tmp_path)
    result = _init(tmp_path)
    assert result.status is WorkspaceInitStatus.ALREADY_INITIALISED
    assert "nothing was changed" in result.reason


def test_a_repeat_run_reports_every_directory_it_had_to_recreate(
    tmp_path: Path,
) -> None:
    """D2, which round 2 fixed for the ledger and not for the tree.

    Deleting `blobs/`, `indexes/` and the whole of `installation-state/` and
    re-running created nine directories -- both `layout.create_directories()` and
    `InstallationLayout.create()` repair here -- and answered "already initialised;
    nothing was changed". `created` counted the manifest, the substrate row and,
    after round 2, the migration ledger. It never counted the filesystem.

    Five, not nine: the six leaves those two calls are responsible for, less
    `locks/`, which survived. `installation-state/backups/` and its siblings are
    parents made on the way and are not separate repairs.
    """
    _init(tmp_path)
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    shutil.rmtree(layout.blobs_path)
    shutil.rmtree(layout.indexes_path)
    shutil.rmtree(tmp_path / "installation-state")

    result = _init(tmp_path)

    assert result.status is WorkspaceInitStatus.INITIALISED
    assert "recreated 5 missing directories" in result.reason
    assert layout.validate(require_database=True) == []
    assert result.workspace_id is not None
    for name in ("backups", "attempts", "runtime"):
        assert (tmp_path / "installation-state" / name / result.workspace_id).is_dir()


def test_a_missing_locks_directory_is_counted_as_a_repair(tmp_path: Path) -> None:
    """`locks/` is repaired before the lock is taken, which is why it is sampled first.

    `_bootstrap` creates it on line two, so a count taken anywhere after that would
    report a workspace whose `locks/` had been lost as unchanged.
    """
    _init(tmp_path)
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    shutil.rmtree(layout.locks_path)

    result = _init(tmp_path)
    assert result.status is WorkspaceInitStatus.INITIALISED
    assert "recreated 1 missing directory" in result.reason
    assert layout.locks_path.is_dir()


# ---------------------------------------------------------------------------
# Incompatible or unrelated existing content is not overwritten
# ---------------------------------------------------------------------------


def test_an_incompatible_manifest_is_refused_and_left_exactly_as_it_is(
    tmp_path: Path,
) -> None:
    """R004-10 refusal 1."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "workspace.json").write_text(
        json.dumps(
            {
                "manifest_version": MANIFEST_VERSION,
                "workspace_id": "ws-from-a-newer-build",
                "created_at": "2026-01-01T00:00:00+00:00",
                "compatibility": {
                    "workspace_format_version": "99",
                    "min_core_version": "0.1.0",
                },
            }
        ),
        encoding="utf-8",
    )
    before = _digest(tmp_path)

    result = _init(tmp_path)
    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.INCOMPATIBLE_MANIFEST
    assert _digest(tmp_path) == before
    # Not merely unchanged -- untouched. Nothing was created beside it either.
    assert not (workspace / "workspace.sqlite").exists()
    assert not (tmp_path / "installation-state").exists()


def test_a_manifest_that_does_not_parse_is_refused_rather_than_replaced(
    tmp_path: Path,
) -> None:
    """The other half of refusal 1, and the more dangerous one.

    A file this build cannot read is the case where "just write a fresh one" is
    most tempting and most destructive: whatever it is, it is not this command's to
    discard.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "workspace.json").write_text("{ not json", encoding="utf-8")
    before = _digest(tmp_path)

    result = _init(tmp_path)
    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.INCOMPATIBLE_MANIFEST
    assert _digest(tmp_path) == before


def test_an_unrelated_non_empty_directory_is_refused_and_left_alone(
    tmp_path: Path,
) -> None:
    """R004-10 refusal 2, with content a user would not forgive losing."""
    workspace = tmp_path / "workspace"
    (workspace / "photos").mkdir(parents=True)
    (workspace / "taxes.txt").write_text("2025 return", encoding="utf-8")
    before = _digest(tmp_path)

    result = _init(tmp_path)
    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.UNRELATED_DIRECTORY
    assert _digest(tmp_path) == before
    assert not (workspace / "workspace.json").exists()


def test_an_unrecognised_installation_state_is_refused_and_left_alone(
    tmp_path: Path,
) -> None:
    """R004-10 refusal 3.

    Refused before the workspace is looked at, because adopting somebody else's
    directory as installation state is the step that would write into it.
    """
    installation = tmp_path / "installation-state"
    installation.mkdir(parents=True)
    (installation / "notes.txt").write_text("not ours", encoding="utf-8")
    before = _digest(tmp_path)

    result = _init(tmp_path)
    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.UNRECOGNISED_INSTALLATION_STATE
    assert _digest(tmp_path) == before
    assert not (tmp_path / "workspace").exists()


# ---------------------------------------------------------------------------
# The filesystem is qualified before anything is created
# ---------------------------------------------------------------------------


def _init_allocated(home: Path) -> WorkspaceInitResult:
    """`initialise_allocated_workspace` over the same deterministic layout."""
    return initialise_allocated_workspace(
        workspace_root=home / "workspace",
        installation_root=home / "installation-state",
        target_workspace_id="ws-allocated-0001",
        display_name="Allocated",
    )


#: Both entry points, every time.
#:
#: `runner.py` has always refused to serve a workspace on a remote or unrecognised
#: filesystem, and both of these could create a manifest, a layout, a database, a
#: lock and an installation tree on one long before anything reached that refusal.
#: A gate on the entry point a test happened to name would have left the other
#: exactly as it was, which is how this pair of defects existed at once.
INIT_ENTRY_POINTS = [_init, _init_allocated]
INIT_ENTRY_POINT_IDS = ["initialise_workspace", "initialise_allocated_workspace"]


@pytest.mark.parametrize("bootstrap", INIT_ENTRY_POINTS, ids=INIT_ENTRY_POINT_IDS)
@pytest.mark.parametrize(
    "filesystem", ["nfs", "nfs4", "smbfs", "cifs", "fuse.sshfs", "afpfs", "webdav"]
)
def test_a_remote_filesystem_is_refused_before_anything_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bootstrap: Any,
    filesystem: str,
) -> None:
    """ADR-037: a remote mount has no reliable cross-host lock semantics.

    The refusal is the one `qualify_filesystem` already decides -- patched at
    `detect_filesystem`, so the refused list, the qualified list and the verdict are
    the production ones and only the answer to "what is this volume" is faked. A
    second copy of those lists here would be the thing this shares them to avoid.

    `== {}` rather than `== before` is deliberate: on an empty home the digest proves
    not merely that nothing existing moved but that nothing was created at all --
    not the workspace root, not `locks/`, not the lock file every *later* refusal
    legitimately leaves behind.
    """
    monkeypatch.setattr(locks_module, "detect_filesystem", lambda path: filesystem)
    before = _digest(tmp_path)

    result = bootstrap(tmp_path)

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.UNQUALIFIED_FILESYSTEM
    assert _digest(tmp_path) == before == {}
    assert not (tmp_path / "workspace").exists()
    assert not (tmp_path / "installation-state").exists()


@pytest.mark.parametrize("bootstrap", INIT_ENTRY_POINTS, ids=INIT_ENTRY_POINT_IDS)
@pytest.mark.parametrize("filesystem", ["unknown", "", "reiserfs", "exfat", "refs"])
def test_an_unqualified_filesystem_is_refused_before_anything_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bootstrap: Any,
    filesystem: str,
) -> None:
    """Default-deny, which is the half a list of remote names cannot cover.

    "We did not recognise it" is not evidence that locking works, and the empty
    string is the case that says so most plainly: a probe that could not identify
    the volume at all has to refuse rather than fall through to a default.
    """
    monkeypatch.setattr(locks_module, "detect_filesystem", lambda path: filesystem)

    result = bootstrap(tmp_path)

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.UNQUALIFIED_FILESYSTEM
    assert _digest(tmp_path) == {}


@pytest.mark.parametrize("bootstrap", INIT_ENTRY_POINTS, ids=INIT_ENTRY_POINT_IDS)
def test_the_filesystem_refusal_quotes_nothing_a_user_put_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bootstrap: Any
) -> None:
    """The reason is structural: a filesystem type, not the contents of a volume.

    A refusal decided over somebody's directory is exactly where a message built by
    listing that directory would carry their filenames off the machine -- this one
    is published on stdout as a machine-readable document. The workspace root is
    given content the other refusals *do* enumerate, so a reason assembled the way
    `UNRELATED_DIRECTORY`'s is would fail here.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "client-passwords.kdbx").write_bytes(b"secret")
    monkeypatch.setattr(locks_module, "detect_filesystem", lambda path: "nfs")
    before = _digest(tmp_path)

    result = bootstrap(tmp_path)
    document = json.dumps(result.to_dict())

    assert result.refusal is WorkspaceInitRefusal.UNQUALIFIED_FILESYSTEM
    assert "client-passwords" not in document
    assert "secret" not in document
    assert _digest(tmp_path) == before


def test_the_local_filesystem_refusal_still_names_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural is not the same as empty, on the entry point a person reads.

    `--init` prints this document to a user at a terminal, and "we will not
    initialise here" without "because it is an NFS mount" is a refusal nobody can
    act on. The allocated entry point deliberately says less -- its result crosses
    a service boundary and its siblings name no paths and no volumes either -- so
    what is shared between them is the machine-readable code, not the sentence.
    """
    monkeypatch.setattr(locks_module, "detect_filesystem", lambda path: "nfs")

    result = _init(tmp_path)

    assert result.refusal is WorkspaceInitRefusal.UNQUALIFIED_FILESYSTEM
    assert "nfs" in result.reason
    assert "networked Core Service" in result.reason, "the remedy is named"


def test_a_workspace_root_that_does_not_exist_yet_still_qualifies(
    tmp_path: Path,
) -> None:
    """The gate refuses unqualified filesystems, not workspaces that are not there.

    `detect_filesystem` used to answer for `path` or `path.parent` and nothing
    further up, and a workspace root is created with `parents=True` -- so a gate
    reading a target several levels below anything that exists would get `"unknown"`
    and default-deny every fresh workspace on a perfectly ordinary local disk. The
    real, unpatched qualification is used here for that reason: faking it would make
    this test pass over the bug.
    """
    deep = tmp_path / "not" / "created" / "yet" / "workspace"
    assert not deep.parent.exists()

    result = initialise_workspace(
        workspace_root=deep, installation_root=tmp_path / "installation-state"
    )

    assert result.status is WorkspaceInitStatus.INITIALISED, result.reason
    assert _substrate(WorkspaceLayout(root=deep)) is not None


@pytest.mark.parametrize("litter", [".DS_Store", "Thumbs.db", "._workspace.json"])
def test_a_file_manager_visiting_the_home_does_not_make_it_somebody_elses(
    tmp_path: Path, litter: str
) -> None:
    """Opening `~/.omnivia` in Finder used to make `omnivia init` refuse for good.

    A `.DS_Store` in `workspace/` was counted as content a person had put there and
    refused as `UNRELATED_DIRECTORY`; the same file in `installation-state/` was
    refused as `UNRECOGNISED_INSTALLATION_STATE`. Neither is recoverable by any
    shipped command, and the default home is precisely the directory a user is most
    likely to open in a file manager. Both are checked in one run because the two
    refusals are decided by different code and both had it.

    The file is asserted still present afterwards: ignoring it is a judgement about
    whose directory this is, not a licence to tidy it away.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / litter).write_bytes(b"\x00\x01")
    installation = tmp_path / "installation-state"
    installation.mkdir(parents=True)
    (installation / litter).write_bytes(b"\x00\x01")

    result = _init(tmp_path)

    assert result.status is WorkspaceInitStatus.INITIALISED, result.reason
    assert (workspace / litter).read_bytes() == b"\x00\x01"
    assert (installation / litter).read_bytes() == b"\x00\x01"


@pytest.mark.parametrize(
    "litter",
    [
        "lost+found",
        ".Trash-1000",
        ".Trash-1001",
        "$RECYCLE.BIN",
        "Temporary Items",
        ".TemporaryItems",
        ".DocumentRevisions-V100",
        "Network Trash Folder",
        ".AppleDouble",
        ".com.apple.timemachine.donotpresent",
    ],
)
def test_a_directory_the_filesystem_made_does_not_make_the_home_somebody_elses(
    tmp_path: Path, litter: str
) -> None:
    """The same permanent brick as the `.DS_Store` case, from a directory instead.

    The list above stopped at what a *file manager* writes. These are what a
    *filesystem* writes, and nobody asks for any of them: `mke2fs` puts `lost+found`
    at the root of every ext2/3/4 volume, Windows puts `$RECYCLE.BIN` on every one
    of its own, and macOS makes the rest the first time a volume is written to or
    backed up. A user whose home is the root of its own disk had `omnivia init`
    refuse permanently over a directory they never created and cannot remove.

    `.Trash-1001` is in the list beside `.Trash-1000` on purpose: freedesktop.org's
    trash is per-uid, so a closed list of names would unbrick the machine's first
    login account and leave the second refused. It is matched by prefix.
    """
    workspace = tmp_path / "workspace"
    (workspace / litter).mkdir(parents=True)
    (workspace / litter / "keep").write_bytes(b"\x00\x01")
    installation = tmp_path / "installation-state"
    (installation / litter).mkdir(parents=True)

    result = _init(tmp_path)

    assert result.status is WorkspaceInitStatus.INITIALISED, result.reason
    # Ignored, not tidied away.
    assert (workspace / litter / "keep").read_bytes() == b"\x00\x01"
    assert (installation / litter).is_dir()


def test_an_appledouble_name_on_a_directory_is_still_somebody_elses(
    tmp_path: Path,
) -> None:
    """The `._` rule was a bare prefix match, and that is wider than its warrant.

    An AppleDouble sidecar is a *file* -- the resource fork a copy onto a non-native
    filesystem leaves beside each real file. A directory is nobody's sidecar, so
    `._my_private_repo/` holding a `.git` was being admitted and a workspace mixed
    into it. Low severity next to the refusals that brick a home, and the wrong
    direction: this is exactly what R004-10's second refusal exists to prevent.

    The file half is still admitted, and is asserted here so the repair cannot be
    made by deleting the rule.
    """
    workspace = tmp_path / "workspace"
    repository = workspace / "._my_private_repo"
    (repository / ".git").mkdir(parents=True)

    refused = _init(tmp_path)
    assert refused.status is WorkspaceInitStatus.REFUSED
    assert refused.refusal is WorkspaceInitRefusal.UNRELATED_DIRECTORY
    assert "._my_private_repo" in refused.reason
    assert not (workspace / "workspace.json").exists()

    shutil.rmtree(repository)
    (workspace / "._an_actual_sidecar").write_bytes(b"\x00\x05\x16\x07")
    assert _init(tmp_path).status is WorkspaceInitStatus.INITIALISED


def test_an_installation_state_this_command_made_is_not_unrecognised(
    tmp_path: Path,
) -> None:
    """The refusal above must not fire on the tree init itself creates.

    Without this, refusal 3 would be indistinguishable from "init may never run
    twice", and every idempotence test above would be passing for the wrong reason.
    """
    assert _init(tmp_path).status is WorkspaceInitStatus.INITIALISED
    assert _init(tmp_path).status is WorkspaceInitStatus.ALREADY_INITIALISED


def test_a_database_that_is_not_ours_is_refused_rather_than_bootstrapped(
    tmp_path: Path,
) -> None:
    """A `workspace.sqlite` carrying somebody's tables is not empty scaffolding.

    This proves the refusal is carried out to the caller as a result rather than
    escaping as a traceback -- and that the file survives it byte for byte, not
    merely with its rows readable.

    **The refusal is `UNRELATED_DIRECTORY`, and it used to be `WRITE_FAILURE`.** It
    arrived as the `SchemaCreationRefused` that `bootstrap_generation_one`'s
    pristine branch raises, was caught with the `OSError`s, and was reported under a
    name whose own docstring says it means the filesystem said no. Nothing failed to
    write. It is R004-10's second refusal reached one layer down: `workspace.sqlite`
    is a permitted layout entry, so `unexpected_entries()` cannot see that this
    directory is somebody else's and only the database can say so.

    **`_digest` used to be excluded here, and the exclusion was a real limit rather
    than a stylistic one.** `init` took its exclusive connection before deciding
    anything, and that open sets `journal_mode = WAL`, which rewrites the header of
    whatever file it opened -- so refusing somebody else's database still changed
    its bytes. The vet now runs on a separate `enable_wal=False` connection that is
    closed before the WAL-enabling one is taken, so the whole-tree comparison every
    other refusal test uses applies to this one too.

    `locks/` is *not* pre-created, and that is the point: this is the smallest tree
    the refusal is reachable on, so what the run leaves behind is asserted by name
    rather than assumed away. `LOCK_RESIDUE` is the whole of it -- the directory the
    lock file must live in, and the lock file -- because the refusal is decided
    under the lock.

    **The mtime is asserted by hand, because `_digest` is structurally unable to see
    it.** The digest carries mode and content; a timestamp is neither. `init` ran
    `layout.database_path.touch()` unconditionally, and `Path.touch` on a file that
    exists is `os.utime(path, None)` -- so refusing somebody else's database moved
    its mtime and atime on every attempt while `_changed(...) == LOCK_RESIDUE`, the
    flagship "not a byte moved" metric, stayed green. Extending the digest with
    timestamps was the alternative and is wrong for this suite: the lock file's
    mtime legitimately moves on every refusal, so it would have to be excluded
    exactly where the last repair went to the trouble of putting it back.

    `atime` is deliberately not asserted. The vet opens and reads this file, and a
    read moves atime on a filesystem that records one; only mtime is a fact about
    whether we wrote.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    connection = sqlite3.connect(str(workspace / "workspace.sqlite"))
    try:
        connection.execute("CREATE TABLE receipts (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO receipts VALUES (1, 'keep me')")
        connection.commit()
    finally:
        connection.close()
    # Pinned to a fixed instant rather than sampled, so this does not depend on the
    # filesystem's timestamp resolution or on the run taking measurable time.
    stamped = 1_000_000_000
    os.utime(workspace / "workspace.sqlite", (stamped, stamped))
    before = _digest(tmp_path)

    result = _init(tmp_path)
    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.UNRELATED_DIRECTORY
    # The database is named, so a user can see which file was declined.
    assert str(workspace / "workspace.sqlite") in result.reason
    assert _changed(before, _digest(tmp_path)) == LOCK_RESIDUE
    assert (workspace / "workspace.sqlite").stat().st_mtime == stamped

    connection = sqlite3.connect(str(workspace / "workspace.sqlite"))
    try:
        assert connection.execute("SELECT note FROM receipts").fetchall() == [
            ("keep me",)
        ]
    finally:
        connection.close()


def test_a_workspace_another_process_owns_is_refused_without_waiting(
    tmp_path: Path,
) -> None:
    """A service holds the lifetime storage lock for the whole ownership lifetime.

    Taking the same lock is what turns "the database is locked" -- which arrives as
    a busy timeout from SQLite, seconds later and with no advice in it -- into an
    answer that names the thing to do.

    `before` follows this test's own `acquire()` for the reason
    `test_a_busy_workspace_is_refused_before_any_directory_is_created` gives: that
    call rewrites the lock payload, and the digest no longer looks away from it.
    """
    from omnivia_core_runtime.ownership.locks import LockRole, create_lock

    first = _init(tmp_path)
    layout = WorkspaceLayout(root=tmp_path / "workspace")

    held = create_lock(layout.locks_path / "storage.lock", LockRole.LIFETIME_STORAGE)
    assert held.acquire()
    before = _digest(tmp_path)
    try:
        result = _init(tmp_path)
    finally:
        held.release()

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_BUSY
    assert result.workspace_id == first.workspace_id
    assert _changed(before, _digest(tmp_path)) == set()


# ---------------------------------------------------------------------------
# The result document
# ---------------------------------------------------------------------------


def test_the_result_identifies_the_workspace_and_carries_no_secret(
    tmp_path: Path,
) -> None:
    """R004-10: a clear result identifying the workspace, without exposing secrets.

    The field list is asserted exactly rather than by presence, because the property
    is what the document does *not* carry: a lease, a fencing generation, a service
    or installation identity, a lock payload. Adding one has to be a deliberate edit
    to this list.
    """
    document = _init(tmp_path).to_dict()
    assert set(document) == {
        "workspace_init_version",
        "status",
        "refusal",
        "reason",
        "workspace",
    }
    assert document["workspace_init_version"] == WORKSPACE_INIT_VERSION == "1.1"
    workspace = document["workspace"]
    assert isinstance(workspace, dict)
    assert set(workspace) == {
        "workspace_id",
        "workspace_root",
        "installation_state",
        "workspace_format_version",
    }
    assert json.dumps(document)  # the whole result is serialisable as it stands


#: The whole wire vocabulary of `workspace_init_version` 1.0, written out by hand.
#:
#: Written out, and not derived from the enums, because deriving it from the thing
#: under test is how the suite arrived here: every refusal assertion in this file
#: compares against `WorkspaceInitRefusal.X.value`, which moves with any rename, so
#: renaming `unrelated_directory` left 2895 tests green. This is the one place the
#: strings exist independently of the code that emits them, and editing it is
#: therefore the deliberate act of changing a published contract.
#:
#: Member *names* are the keys only so a failure names which code moved. What is
#: contractual is the values: an out-of-repo adapter branches on
#: `"unrelated_directory"` and never sees the identifier beside it.
#:
#: **Kept after 1.1 shipped, and kept frozen.** It is what makes "additive" a
#: measured claim rather than a sentence in a docstring: 1.1's fixture below is
#: compared against it as a superset, so a later packet that renames one of these
#: six or moves a case between them fails here even though the 1.1 fixture beside
#: it would have been edited to agree.
WORKSPACE_INIT_WIRE_1_0 = {
    "status": {
        "INITIALISED": "initialised",
        "ALREADY_INITIALISED": "already_initialised",
        "REFUSED": "refused",
    },
    "refusal": {
        "INCOMPATIBLE_MANIFEST": "incompatible_manifest",
        "UNRELATED_DIRECTORY": "unrelated_directory",
        "UNRECOGNISED_INSTALLATION_STATE": "unrecognised_installation_state",
        "WORKSPACE_IDENTITY_MISMATCH": "workspace_identity_mismatch",
        "WORKSPACE_BUSY": "workspace_busy",
        "WRITE_FAILURE": "write_failure",
    },
}

#: The whole wire vocabulary of `workspace_init_version` 1.1, on the same terms.
#:
#: One code more than 1.0 and not one character different anywhere else.
#: `unqualified_filesystem` is the state 1.0 had no name for: a workspace root on a
#: remote or unrecognised filesystem, which `runner.py` has always refused to serve
#: and which both init entry points now refuse to create anything on.
WORKSPACE_INIT_WIRE_1_1 = {
    "status": dict(WORKSPACE_INIT_WIRE_1_0["status"]),
    "refusal": {
        **WORKSPACE_INIT_WIRE_1_0["refusal"],
        "UNQUALIFIED_FILESYSTEM": "unqualified_filesystem",
    },
}


def test_the_published_vocabulary_widened_additively_from_1_0() -> None:
    """The 1.0 -> 1.1 compatibility claim, as an assertion rather than a comment.

    Additive means two things and both are checked. Every 1.0 code still exists with
    the *same* string, so a 1.0 reader branching on the six it knows still reads
    every document 1.0 could produce; and what 1.1 adds is exactly one refusal, so
    "one code more" cannot quietly become five.

    The version moved because the closed vocabulary widened. A reader that treats an
    unknown `refusal` as fatal is the case this bump exists to warn, and the bump is
    a minor one because the codes it already understands are untouched.
    """
    assert WORKSPACE_INIT_VERSION == "1.1"
    assert WORKSPACE_INIT_WIRE_1_1["status"] == WORKSPACE_INIT_WIRE_1_0["status"]
    for name, wire in WORKSPACE_INIT_WIRE_1_0["refusal"].items():
        assert WORKSPACE_INIT_WIRE_1_1["refusal"][name] == wire, (
            f"{name} changed its wire value; that is a break, not a widening"
        )
    added = set(WORKSPACE_INIT_WIRE_1_1["refusal"]) - set(
        WORKSPACE_INIT_WIRE_1_0["refusal"]
    )
    assert added == {"UNQUALIFIED_FILESYSTEM"}


def test_every_published_code_serialises_to_its_pinned_wire_value() -> None:
    """R006-07: the published vocabulary of 1.1, by exact serialised value.

    Two hops are checked, because a value can be right in the enum and wrong on
    the wire. First the enums against the fixture above, by dict equality in both
    directions -- so an added code, a removed code and a changed string all fail,
    and a code that exists but is never published cannot hide. Then every member
    through `to_dict()`, which is what an adapter actually reads.

    **What this replaces.** Nothing pinned these strings. `unrelated_directory` was
    renamed to `somebody_elses_directory` and the full suite -- 2840 phase 3 plus 55
    CLI -- stayed green, because the only two assertions that looked at a serialised
    code compared it to `WorkspaceInitRefusal.X.value` and to `WORKSPACE_INIT_VERSION`,
    both of which move with the mutation.

    `1.1` is asserted as a literal for the same reason.
    """
    assert WORKSPACE_INIT_VERSION == "1.1"
    assert {
        member.name: member.value for member in WorkspaceInitStatus
    } == WORKSPACE_INIT_WIRE_1_1["status"]
    assert {
        member.name: member.value for member in WorkspaceInitRefusal
    } == WORKSPACE_INIT_WIRE_1_1["refusal"]

    for name, wire in WORKSPACE_INIT_WIRE_1_1["refusal"].items():
        document = WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            reason="pinning the wire value",
            refusal=WorkspaceInitRefusal[name],
        ).to_dict()
        assert document["refusal"] == wire
        assert document["status"] == "refused"
        assert document["workspace_init_version"] == "1.1"


def test_a_refusal_code_never_depends_on_its_declaration_position() -> None:
    """R006-07: reordering the declarations must not move the wire contract.

    The property is about the *mechanism*, so it is checked over the source rather
    than over the imported enum: every member of both published enums is assigned a
    string literal. `auto()`, an implicit `_generate_next_value_` and a bare integer
    counter are each excluded by that, and each is a way for inserting one member
    above another to silently renumber every member below it -- which is exactly the
    failure R006-07 exists to prevent, and which no fixture comparison would catch
    until an adapter in another repository broke.

    Mutation-tested by actually reordering. The six refusals were reversed in the
    source, and this test and the fixture one above both stayed green: that is the
    contract holding, and it is the point. Replacing one literal with `auto()` turns
    both red -- this one because the value is not a string constant, the other
    because `auto()` numbers from 1.

    The source-declared pairs are then compared to the imported enum, so this cannot
    pass by reading a class that no longer exists or a member the runtime dropped.
    """
    import ast

    source = Path(inspect.getfile(WorkspaceInitRefusal)).read_text(encoding="utf-8")
    declared: dict[str, dict[str, str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name not in {"WorkspaceInitStatus", "WorkspaceInitRefusal"}:
            continue
        members: dict[str, str] = {}
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            target = statement.targets[0]
            assert isinstance(target, ast.Name), f"{node.name}: unexpected member form"
            value = statement.value
            assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
                f"{node.name}.{target.id} is not a string literal, so its wire value "
                "depends on where it is declared"
            )
            members[target.id] = value.value
        declared[node.name] = members

    assert declared["WorkspaceInitStatus"] == {
        member.name: member.value for member in WorkspaceInitStatus
    }
    assert declared["WorkspaceInitRefusal"] == {
        member.name: member.value for member in WorkspaceInitRefusal
    }


def test_a_new_workspace_is_created_in_a_format_this_build_can_open(
    tmp_path: Path,
) -> None:
    """The constant and the supported set cannot drift apart unnoticed.

    `WORKSPACE_FORMAT_VERSION` is restated rather than derived -- `SUPPORTED_WORKSPACE_FORMATS`
    is a set with no ordering that would name a current member -- so the thing that
    keeps a build from creating a workspace it then refuses to open is this test.
    """
    assert WORKSPACE_FORMAT_VERSION in SUPPORTED_WORKSPACE_FORMATS
    result = _init(tmp_path)
    assert result.workspace_format_version == WORKSPACE_FORMAT_VERSION


@pytest.mark.parametrize(
    "core_version, expected",
    [
        ("0.1.0", WorkspaceInitStatus.ALREADY_INITIALISED),
        ("9.9.9", WorkspaceInitStatus.ALREADY_INITIALISED),
        ("0.0.1", WorkspaceInitStatus.REFUSED),
    ],
)
def test_the_minted_manifest_records_the_build_that_created_it(
    tmp_path: Path, core_version: str, expected: WorkspaceInitStatus
) -> None:
    """`min_core_version` is the creating build, and it is load-bearing.

    A workspace made by Core 0.1.0 must refuse an *older* build for writing and
    admit a newer one. Asserting the manifest field alone would not show that
    anything reads it, so the second run is made as each build in turn.
    """
    assert (
        initialise_workspace(
            workspace_root=tmp_path / "workspace",
            installation_root=tmp_path / "installation-state",
            core_version="0.1.0",
        ).status
        is WorkspaceInitStatus.INITIALISED
    )

    again = initialise_workspace(
        workspace_root=tmp_path / "workspace",
        installation_root=tmp_path / "installation-state",
        core_version=core_version,
    )
    assert again.status is expected
    if expected is WorkspaceInitStatus.REFUSED:
        assert again.refusal is WorkspaceInitRefusal.INCOMPATIBLE_MANIFEST


def test_the_manifest_carries_no_machine_local_path(tmp_path: Path) -> None:
    """The manifest is portable, so nothing in it may describe this machine.

    A human label derived from the directory would carry a user's home directory
    into every copy of the workspace.
    """
    _init(tmp_path)
    stored = (tmp_path / "workspace" / "workspace.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in stored
