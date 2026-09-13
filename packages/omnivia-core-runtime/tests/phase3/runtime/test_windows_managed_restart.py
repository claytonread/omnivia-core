"""Hosted-Windows proof that initialized workspaces pass the managed client.

The ACL writer and reader live in sibling distributions and can each satisfy their
own doubled tests while disagreeing on a real host. These cases deliberately begin
under pytest's inherited temp-directory ACL, run the production initializer, then
drive the installed client through a real named-pipe service start and attachment.
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from omnivia_core_client import (
    Deadline,
    InstallationServiceConfig,
    connect_managed_local,
    owner_private_directory,
    stop_managed_local,
)
from omnivia_core_runtime.ownership.locks import LockRole, create_lock
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitRefusal,
    WorkspaceInitResult,
    WorkspaceInitStatus,
    _create_empty_database_file,
    _ensure_workspace_directory,
    _refresh_current_windows_paths,
    _windows_initialisation_guard,
    initialise_allocated_workspace,
    initialise_workspace,
)

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="exercises real Windows ACLs and named pipes"
)


def _start_attach_stop(home: Path, workspace_id: str) -> None:
    config = InstallationServiceConfig(
        installation_state=home / "installation-state",
        workspace_id=workspace_id,
    )
    started = False
    try:
        first = connect_managed_local(config, deadline=Deadline.after(120))
        started = True
        assert first.status == "started"

        second = connect_managed_local(config, deadline=Deadline.after(30))
        assert second.status == "attached"
    finally:
        if started:
            stopped = stop_managed_local(config, deadline=Deadline.after(30))
            assert stopped.status == "stopped"


def _existing_blob_tree(tmp_path: Path) -> tuple[Path, str, Path]:
    home = tmp_path / "existing-blob-home"
    result = initialise_workspace(
        workspace_root=home / "workspace",
        installation_root=home / "installation-state",
    )
    assert result.status is WorkspaceInitStatus.INITIALISED
    assert result.workspace_id is not None
    digest_directory = home / "workspace" / "blobs" / "sha256"
    digest_directory.mkdir()
    return home, result.workspace_id, digest_directory


def _reinitialise(home: Path) -> WorkspaceInitResult:
    return initialise_workspace(
        workspace_root=home / "workspace",
        installation_root=home / "installation-state",
    )


def test_legacy_init_creates_the_complete_windows_restart_trust_chain(
    tmp_path: Path,
) -> None:
    home = tmp_path / "legacy-home"
    # Existing on purpose: initialization must secure the managed trust anchor,
    # not rely on pytest's inherited Windows temp-directory DACL.
    home.mkdir()
    result = initialise_workspace(
        workspace_root=home / "workspace",
        installation_root=home / "installation-state",
    )
    assert result.status is WorkspaceInitStatus.INITIALISED
    assert result.workspace_id is not None

    _start_attach_stop(home, result.workspace_id)


def test_allocated_init_creates_the_complete_windows_restart_trust_chain(
    tmp_path: Path,
) -> None:
    home = tmp_path / "allocated-home"
    home.mkdir()
    workspace_id = "ws-windows-allocated-0001"
    result = initialise_allocated_workspace(
        workspace_root=home / "workspaces" / workspace_id,
        installation_root=home / "installation-state",
        target_workspace_id=workspace_id,
        display_name="Windows allocated restart",
    )
    assert result.status is WorkspaceInitStatus.INITIALISED

    _start_attach_stop(home, workspace_id)


def test_windows_init_refuses_a_real_junction_before_writing_through_it(
    tmp_path: Path,
) -> None:
    """The hosted row proves the native reparse attribute, not a POSIX stand-in."""
    home = tmp_path / "junction-home"
    redirected = tmp_path / "redirected"
    home.mkdir()
    redirected.mkdir()
    junction = home / "workspaces"
    command = Path(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "cmd.exe")
    created = subprocess.run(
        [str(command), "/d", "/c", "mklink", "/J", str(junction), str(redirected)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert created.returncode == 0, created.stderr or created.stdout
    try:
        workspace_id = "ws-windows-junction-0001"
        result = initialise_allocated_workspace(
            workspace_root=junction / workspace_id,
            installation_root=home / "installation-state",
            target_workspace_id=workspace_id,
            display_name="Must not be redirected",
        )
    finally:
        os.rmdir(junction)

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WRITE_FAILURE
    assert not (redirected / workspace_id).exists()


def test_windows_guard_pins_existing_directories_against_rename(
    tmp_path: Path,
) -> None:
    """No-share-delete handles close the lstat-to-use replacement window."""
    home = tmp_path / "pinned-home"
    workspace = home / "workspace"
    installation = home / "installation-state"
    workspace.mkdir(parents=True)
    installation.mkdir()
    moved = tmp_path / "moved-home"

    with _windows_initialisation_guard(workspace, installation), pytest.raises(OSError):
        home.rename(moved)

    home.rename(moved)
    moved.rename(home)


def test_windows_reinitialisation_refuses_a_retained_blob_write_handle(
    tmp_path: Path,
) -> None:
    home, _workspace_id, directory = _existing_blob_tree(tmp_path)
    blob = directory / ("a" * 64)
    blob.write_bytes(b"content")

    with blob.open("r+b"):
        refused = _reinitialise(home)

    assert refused.status is WorkspaceInitStatus.REFUSED
    assert refused.refusal is WorkspaceInitRefusal.WRITE_FAILURE
    assert _reinitialise(home).status is WorkspaceInitStatus.ALREADY_INITIALISED


def test_windows_reinitialisation_refuses_a_blob_symlink(tmp_path: Path) -> None:
    home, _workspace_id, directory = _existing_blob_tree(tmp_path)
    outside = tmp_path / "outside-symlink-blob"
    outside.write_bytes(b"content")
    blob = directory / ("b" * 64)
    try:
        blob.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"this Windows host cannot create a file symlink: {error}")

    refused = _reinitialise(home)

    assert refused.status is WorkspaceInitStatus.REFUSED
    assert refused.refusal is WorkspaceInitRefusal.WRITE_FAILURE
    assert outside.read_bytes() == b"content"


def test_windows_reinitialisation_refuses_a_blob_junction(tmp_path: Path) -> None:
    home, _workspace_id, directory = _existing_blob_tree(tmp_path)
    outside = tmp_path / "outside-junction-blobs"
    outside.mkdir()
    junction = directory / "redirected"
    command = Path(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "cmd.exe")
    created = subprocess.run(
        [str(command), "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert created.returncode == 0, created.stderr or created.stdout
    try:
        refused = _reinitialise(home)
    finally:
        os.rmdir(junction)

    assert refused.status is WorkspaceInitStatus.REFUSED
    assert refused.refusal is WorkspaceInitRefusal.WRITE_FAILURE


def test_windows_reinitialisation_refuses_a_hard_linked_blob(tmp_path: Path) -> None:
    home, _workspace_id, directory = _existing_blob_tree(tmp_path)
    outside = tmp_path / "outside-hard-linked-blob"
    outside.write_bytes(b"content")
    os.link(outside, directory / ("c" * 64))

    refused = _reinitialise(home)

    assert refused.status is WorkspaceInitStatus.REFUSED
    assert refused.refusal is WorkspaceInitRefusal.WRITE_FAILURE
    assert outside.read_bytes() == b"content"


def test_windows_guard_adds_new_directories_files_and_manifests(
    tmp_path: Path,
) -> None:
    home = tmp_path / "growing-home"
    workspace = home / "workspace"
    installation = home / "installation-state"
    workspace.mkdir(parents=True)
    installation.mkdir()
    blobs = workspace / "blobs"
    database = workspace / "workspace.sqlite"
    manifest = workspace / "workspace.json"

    with _windows_initialisation_guard(workspace, installation):
        _ensure_workspace_directory(blobs)
        _create_empty_database_file(database)
        manifest.write_text("{}", encoding="utf-8")
        _refresh_current_windows_paths()
        for path in (blobs, database, manifest):
            with pytest.raises(OSError):
                path.rename(path.with_name(f"moved-{path.name}"))


def test_windows_guard_allows_sqlite_to_delete_its_sidecars(
    tmp_path: Path,
) -> None:
    home = tmp_path / "sidecar-home"
    workspace = home / "workspace"
    installation = home / "installation-state"
    workspace.mkdir(parents=True)
    installation.mkdir()
    database = workspace / "workspace.sqlite"

    with _windows_initialisation_guard(workspace, installation):
        _create_empty_database_file(database)
        connection = sqlite3.connect(database)
        try:
            journal = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            assert journal is not None and str(journal[0]).lower() == "wal"
            connection.execute("CREATE TABLE captured (value TEXT NOT NULL)")
            connection.execute("INSERT INTO captured VALUES ('durable')")
            connection.commit()
            assert database.with_name(f"{database.name}-wal").is_file()
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            assert checkpoint is not None and int(checkpoint[0]) == 0
        finally:
            connection.close()

        # SQLite is allowed to remove or replace its deletion-managed sidecars;
        # if a version leaves either behind at close, the guard must not block it.
        for suffix in ("-wal", "-shm"):
            sidecar = database.with_name(f"{database.name}{suffix}")
            if sidecar.exists():
                sidecar.unlink()

    reopened = sqlite3.connect(database)
    try:
        assert reopened.execute("SELECT value FROM captured").fetchone() == ("durable",)
    finally:
        reopened.close()


def test_windows_lock_pins_its_parent_chain_for_its_full_lifetime(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "lock-parent"
    lock = create_lock(parent / "nested" / "storage.lock", LockRole.LIFETIME_STORAGE)
    assert lock.acquire()
    try:
        assert owner_private_directory(parent)
        assert owner_private_directory(parent / "nested")
        with pytest.raises(OSError):
            parent.rename(tmp_path / "moved-lock-parent")
    finally:
        lock.release()

    moved = tmp_path / "moved-lock-parent"
    parent.rename(moved)
    moved.rename(parent)


def test_windows_lock_refuses_a_preexisting_hard_link_without_truncating_target(
    tmp_path: Path,
) -> None:
    """A no-follow regular-file check must not mistake a hard link for our lock."""
    parent = tmp_path / "hard-link-lock"
    parent.mkdir()
    target = parent / "unrelated.txt"
    target.write_bytes(b"must remain byte-for-byte")
    lock_path = parent / "storage.lock"
    os.link(target, lock_path)

    lock = create_lock(lock_path, LockRole.LIFETIME_STORAGE)
    with pytest.raises(OSError):
        lock.acquire()

    assert target.read_bytes() == b"must remain byte-for-byte"
    assert lock_path.read_bytes() == b"must remain byte-for-byte"


def test_windows_init_refuses_a_database_open_outside_the_lifetime_lock(
    tmp_path: Path,
) -> None:
    home = tmp_path / "open-database-home"
    workspace = home / "workspace"
    installation = home / "installation-state"
    first = initialise_workspace(
        workspace_root=workspace,
        installation_root=installation,
    )
    assert first.status is WorkspaceInitStatus.INITIALISED

    outsider = sqlite3.connect(workspace / "workspace.sqlite")
    try:
        outsider.execute("SELECT 1").fetchone()
        refused = initialise_workspace(
            workspace_root=workspace,
            installation_root=installation,
        )
    finally:
        outsider.close()

    assert refused.status is WorkspaceInitStatus.REFUSED
    assert refused.refusal is WorkspaceInitRefusal.WRITE_FAILURE
    retry = initialise_workspace(
        workspace_root=workspace,
        installation_root=installation,
    )
    assert retry.status is WorkspaceInitStatus.ALREADY_INITIALISED


def test_windows_init_refuses_a_live_access_capable_workspace_directory_handle(
    tmp_path: Path,
) -> None:
    """A repaired DACL is trusted only after pre-existing handles are excluded."""
    home = tmp_path / "open-directory-home"
    workspace = home / "workspace"
    installation = home / "installation-state"
    first = initialise_workspace(
        workspace_root=workspace,
        installation_root=installation,
    )
    assert first.status is WorkspaceInitStatus.INITIALISED

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
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
    handle = kernel32.CreateFileW(
        str(workspace),
        0x00000002,  # FILE_ADD_FILE / FILE_WRITE_DATA
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    value = getattr(handle, "value", handle)
    assert isinstance(value, int) and value not in (0, invalid)
    try:
        refused = initialise_workspace(
            workspace_root=workspace,
            installation_root=installation,
        )
    finally:
        kernel32.CloseHandle(handle)

    assert refused.status is WorkspaceInitStatus.REFUSED
    assert refused.refusal is WorkspaceInitRefusal.WRITE_FAILURE
    retry = initialise_workspace(
        workspace_root=workspace,
        installation_root=installation,
    )
    assert retry.status is WorkspaceInitStatus.ALREADY_INITIALISED
