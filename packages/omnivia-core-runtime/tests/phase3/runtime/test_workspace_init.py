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
import json
import os
import sqlite3
from pathlib import Path

import pytest
from omnivia_core_runtime.service.workspace_init import (
    WORKSPACE_FORMAT_VERSION,
    WORKSPACE_INIT_VERSION,
    WorkspaceInitRefusal,
    WorkspaceInitResult,
    WorkspaceInitStatus,
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


#: The one file whose bytes legitimately move on every run. `_BaseFileLock` writes
#: the holder's pid into it as advisory diagnostics each time the lock is taken --
#: ownership is decided by the OS lock and never by reading this file -- so a
#: comparison that included it would report a *successful* second init as having
#: changed the workspace. It is excluded here and nowhere else.
LOCK_PAYLOAD = "workspace/locks/storage.lock"


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
    """
    entries: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if name == LOCK_PAYLOAD:
            continue
        if path.is_symlink():
            content = f"-> {os.readlink(path)}"
        elif path.is_dir():
            content = "directory"
        else:
            content = hashlib.sha256(path.read_bytes()).hexdigest()
        entries[name] = f"{path.lstat().st_mode:o} {content}"
    return entries


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

    connection = open_database(layout.database_path, OpenMode.READ_ONLY)
    try:
        applied = applied_migrations(connection)
    finally:
        connection.close()
    # Every shipped migration, not merely the substrate: a workspace missing one
    # would start and then fail on the first operation that needs its table.
    assert set(applied) == {migration.version for migration in load_migrations()}


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


def test_a_repeat_run_repairs_a_missing_layout_directory(tmp_path: Path) -> None:
    """Non-destructive includes not refusing to finish its own half-done work."""
    _init(tmp_path)
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    manifest_before = layout.manifest_path.read_bytes()
    layout.indexes_path.rmdir()

    assert _init(tmp_path).status is WorkspaceInitStatus.ALREADY_INITIALISED
    assert layout.indexes_path.is_dir()
    # Repaired around the manifest, never over it.
    assert layout.manifest_path.read_bytes() == manifest_before


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

    `bootstrap_generation_one`'s pristine branch refuses a database that already has
    tables, and this proves that refusal is carried out to the caller as a result
    rather than escaping as a traceback -- and that the rows survive it.
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

    result = _init(tmp_path)
    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WRITE_FAILURE

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
    """
    from omnivia_core_runtime.ownership.locks import LockRole, create_lock

    first = _init(tmp_path)
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    before = _digest(tmp_path)

    held = create_lock(layout.locks_path / "storage.lock", LockRole.LIFETIME_STORAGE)
    assert held.acquire()
    try:
        result = _init(tmp_path)
    finally:
        held.release()

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_BUSY
    assert result.workspace_id == first.workspace_id
    assert _digest(tmp_path) == before


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
    assert document["workspace_init_version"] == WORKSPACE_INIT_VERSION
    workspace = document["workspace"]
    assert isinstance(workspace, dict)
    assert set(workspace) == {
        "workspace_id",
        "workspace_root",
        "installation_state",
        "workspace_format_version",
    }
    assert json.dumps(document)  # the whole result is serialisable as it stands


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
