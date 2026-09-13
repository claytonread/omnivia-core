"""Hosted-Windows proof that initialized workspaces pass the managed client.

The ACL writer and reader live in sibling distributions and can each satisfy their
own doubled tests while disagreeing on a real host. These cases deliberately begin
under pytest's inherited temp-directory ACL, run the production initializer, then
drive the installed client through a real named-pipe service start and attachment.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from omnivia_core_client import (
    Deadline,
    InstallationServiceConfig,
    connect_managed_local,
    stop_managed_local,
)
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitStatus,
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
