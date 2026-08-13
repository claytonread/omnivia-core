"""Installed-shape lifecycle administration against a real local service."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

WORKSPACE_ID = "ws-lifecycle-tests-0001"


def _bootstrap(home: Path) -> None:
    from omnivia_core_runtime.storage.backup import InstallationLayout
    from omnivia_core_runtime.storage.legacy import migrate_legacy_database
    from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
    from omnivia_core_runtime.workspace.layout import WorkspaceLayout

    from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

    legacy = home / "legacy" / "source.sqlite"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    materialise_phase0_baseline(legacy)
    installation = InstallationLayout(root=home / "installation-state")
    installation.create(WORKSPACE_ID)
    migrate_legacy_database(
        legacy,
        WorkspaceLayout(root=home / "workspace"),
        installation,
        WorkspaceManifest(
            workspace_id=WORKSPACE_ID,
            created_at="2026-08-06T00:00:00+00:00",
            name="lifecycle tests",
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version="0.1.0"
            ),
        ),
        service_instance_id="svc-lifecycle-tests",
    )
    if os.name != "nt":
        (home / "installation-state" / "runtime").chmod(0o700)
        (home / "installation-state" / "runtime" / WORKSPACE_ID).chmod(0o700)


def _cli(home: Path, action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnivia_core_cli.main",
            "--installation-state",
            str(home / "installation-state"),
            "--workspace-id",
            WORKSPACE_ID,
            "--timeout-ms",
            "60000",
            "service",
            action,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _service_pid(home: Path) -> int | None:
    descriptor = (
        home / "installation-state" / "runtime" / WORKSPACE_ID / "service.json"
    )
    try:
        document = json.loads(descriptor.read_text(encoding="utf-8"))
        pid = document["process"]["pid"]
    except (OSError, KeyError, TypeError, ValueError):
        return None
    return pid if isinstance(pid, int) and pid > 0 else None


@pytest.fixture
def home() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="ovl-", dir="/tmp"))
    _bootstrap(root)
    try:
        yield root
    finally:
        pid = _service_pid(root)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(root, ignore_errors=True)


def _document(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.stderr == ""
    assert result.stdout.endswith("\n") and result.stdout.count("\n") == 1
    document = json.loads(result.stdout)
    assert document["lifecycle_adapter_version"] == 2
    rendered = result.stdout
    for forbidden in ('"endpoint"', '"pid"', '"service_instance_id"', '"reason"'):
        assert forbidden not in rendered
    return dict(document)


def test_status_start_status_stop_is_one_safe_namespaced_lifecycle(home: Path) -> None:
    absent = _cli(home, "status")
    assert absent.returncode == 1
    assert _document(absent)["code"] == "status_not_running"

    started = _cli(home, "start")
    assert started.returncode == 0
    assert _document(started)["code"] == "start_started"
    assert _service_pid(home) is not None

    running = _cli(home, "status")
    assert running.returncode == 0
    running_document = _document(running)
    assert running_document["code"] == "status_running"
    assert dict(running_document["safe_status"])["lifecycle_state"] == "running"

    stopped = _cli(home, "stop")
    assert stopped.returncode == 0
    assert _document(stopped)["code"] == "stop_stopped"
    assert _service_pid(home) is None


def test_a_stale_descriptor_never_authorizes_a_signal(home: Path) -> None:
    assert _cli(home, "start").returncode == 0
    pid = _service_pid(home)
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)

    stopped = _cli(home, "stop")
    assert stopped.returncode == 1
    assert _document(stopped)["code"] == "stop_service_unreachable"
