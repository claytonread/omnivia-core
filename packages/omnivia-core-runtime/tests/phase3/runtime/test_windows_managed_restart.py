"""Hosted-Windows proof that initialized workspaces pass the managed client.

The ACL writer and reader live in sibling distributions and can each satisfy their
own doubled tests while disagreeing on a real host. These cases deliberately begin
under pytest's inherited temp-directory ACL, run the production initializer, then
drive the installed client through a real named-pipe service start and attachment.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnivia_core_client import (
    Deadline,
    InstallationServiceConfig,
    connect_managed_local,
    stop_managed_local,
)
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitRefusal,
    WorkspaceInitStatus,
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
