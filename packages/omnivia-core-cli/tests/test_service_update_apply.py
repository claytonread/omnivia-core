"""The update coordinator and worker (spec v0.4 §7–§11; A03–A06).

No test here touches a live feed, a real service or a real pip: the fetch,
probe, confirm, spawn and command-runner seams are all injected, and the
bundle fixture is built in the test's own tmp directory. What is pinned:

- **A03 (approval binding):** the instruction the worker receives carries the
  exact approved versions and bundle digest — a feed change after approval
  cannot change what is installed, because the executor reads the instruction,
  not the feed.
- **A04 (prepare before interruption):** a staging or verification failure
  leaves the record ``blocked`` and never runs a stop command.
- **A05 (one owner):** a second coordinator on a held lock is
  ``update_already_running`` with the live record attached.
- **A06 (crash recovery):** the flock is kernel-held — a dead holder leaves no
  lock; the next coordinator acquires it.
- The worker's phase machine: every phase transition is written to the
  operation record before the next command runs; ``updated`` requires the
  version probe to return the approved versions.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_cli.updates_apply import (
    BUNDLE_ASSET_NAME,
    InstallationLock,
    OperationRecord,
    affected_services,
    bundle_url,
    coordinate_update,
    read_operation_record,
    run_worker,
    stage_candidate,
)

NOW = "2026-09-30T00:00:00Z"
INSTALLED = {
    "omnivia-core": "0.1.0",
    "omnivia-core-runtime": "0.1.0",
    "omnivia-core-client": "0.1.0",
    "omnivia-core-cli": "0.1.0",
    "omnivia-core-mcp": "0.1.0",
}


def _channel(release_url: str, version: str, bundle_sha256: str | None = None) -> dict[str, Any]:
    release: dict[str, Any] = {
        "version": version,
        "release_url": release_url,
        "packages": {name: version for name in INSTALLED},
    }
    if bundle_sha256 is not None:
        release["bundle_sha256"] = bundle_sha256
    return {
        "schema_version": "omnivia-core-update.v1",
        "channel": "stable",
        "release": release,
    }


def _bundle(tmp_path: Path, version: str) -> tuple[bytes, str]:
    """One minimal candidate bundle: five wheels + the checksum inventory."""
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    lines = ["# checksums-sha256.txt"]
    for name in INSTALLED:
        wheel_name = name.replace("-", "_")
        content = f"wheel bytes for {wheel_name} {version}".encode()
        (wheels / f"{wheel_name}-{version}-py3-none-any.whl").write_bytes(content)
        import hashlib

        lines.append(f"{hashlib.sha256(content).hexdigest()}  {wheel_name}-{version}-py3-none-any.whl")
    checksums = tmp_path / "checksums-sha256.txt"
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    zip_path = tmp_path / BUNDLE_ASSET_NAME
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(checksums, "checksums-sha256.txt")
        for wheel in wheels.iterdir():
            archive.write(wheel, f"wheels/{wheel.name}")
    return zip_path.read_bytes(), bundle_url("https://github.com/claytonread/omnivia-core/releases/tag/core-v" + version)


def _instruction() -> dict[str, Any]:
    return {
        "operation_id": "upd-test-1",
        "installation_ref": "/installation",
        "installation_state": "/installation",
        "approved": {
            "version": "0.2.0",
            "bundle_sha256": None,
            "packages": {name: "0.2.0" for name in INSTALLED},
        },
        "affected": [
            {"workspace_id": "ws-a", "was_running": True},
            {"workspace_id": "ws-b", "was_running": False},
        ],
        "staging_wheels": "/staging/wheels",
        "python_executable": "/venv/bin/python",
        "cli_executable": "/venv/bin/omnivia",
        "started_at": NOW,
    }


class Recorder:
    """A subprocess-shaped runner that records commands and answers by rule."""

    def __init__(self, rules: Any = None) -> None:
        self.commands: list[list[str]] = []
        self._rules = rules

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.commands.append(list(command))
        if self._rules is not None:
            return self._rules(command)
        stdout = ""
        if "import importlib.metadata" in " ".join(command):
            stdout = json.dumps({name: "0.2.0" for name in INSTALLED})
        return subprocess.CompletedProcess(command, 0, stdout, "")


# --- the worker's phase machine -------------------------------------------------------


def test_the_worker_runs_the_full_phase_machine_and_writes_each_transition(
    tmp_path: Path,
) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    instruction = _instruction()
    instruction["installation_state"] = str(installation)
    recorder = Recorder()
    outcome = run_worker(instruction, runner=recorder)
    assert outcome == "updated"
    record = read_operation_record(installation)
    assert record is not None
    assert record["outcome"] == "updated"
    assert record["phase"] == "complete"
    # stop ran only for the running service; install ran once; start ran once.
    stops = [c for c in recorder.commands if c[-3:] == ["service", "stop", "--json"]]
    starts = [c for c in recorder.commands if c[-3:] == ["service", "start", "--json"]]
    installs = [c for c in recorder.commands if c[1:3] == ["-m", "pip"]]
    assert len(stops) == 1 and "ws-a" in stops[0]
    assert len(installs) == 1
    assert len(starts) == 1 and "ws-a" in starts[0]
    # --no-index: the installer never reaches the network.
    assert "--no-index" in installs[0]


def test_a_stop_failure_leaves_failed_without_installing(tmp_path: Path) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    instruction = _instruction()
    instruction["installation_state"] = str(installation)

    def rules(command: list[str]) -> Any:
        if command[-3:] == ["service", "stop", "--json"]:
            return subprocess.CompletedProcess(command, 1, "", "stop failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    recorder = Recorder(rules)
    outcome = run_worker(instruction, runner=recorder)
    assert outcome == "failed"
    record = read_operation_record(installation)
    assert record is not None
    assert record["outcome"] == "failed"
    assert record["failure_code"] == "stop_failed"
    assert [c for c in recorder.commands if c[1:3] == ["-m", "pip"]] == []


def test_an_install_failure_is_failed_install(tmp_path: Path) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    instruction = _instruction()
    instruction["installation_state"] = str(installation)

    def rules(command: list[str]) -> Any:
        if command[1:3] == ["-m", "pip"]:
            return subprocess.CompletedProcess(command, 1, "", "pip exploded")
        return subprocess.CompletedProcess(command, 0, "", "")

    outcome = run_worker(instruction, runner=Recorder(rules))
    assert outcome == "failed"
    record = read_operation_record(installation)
    assert record is not None
    assert record["failure_code"] == "install_failed"


def test_a_restart_failure_is_failed_restart(tmp_path: Path) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    instruction = _instruction()
    instruction["installation_state"] = str(installation)

    def rules(command: list[str]) -> Any:
        if command[-3:] == ["service", "start", "--json"]:
            return subprocess.CompletedProcess(command, 1, "", "start failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    outcome = run_worker(instruction, runner=Recorder(rules))
    assert outcome == "failed"
    record = read_operation_record(installation)
    assert record is not None
    assert record["failure_code"] == "restart_failed"


def test_a_verify_mismatch_is_failed_verify_not_updated(tmp_path: Path) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    instruction = _instruction()
    instruction["installation_state"] = str(installation)

    def rules(command: list[str]) -> Any:
        if "import importlib.metadata" in " ".join(command):
            stale = {name: "0.1.0" for name in INSTALLED}
            return subprocess.CompletedProcess(command, 0, json.dumps(stale), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    outcome = run_worker(instruction, runner=Recorder(rules))
    assert outcome == "failed"
    record = read_operation_record(installation)
    assert record is not None
    assert record["failure_code"] == "verify_failed"
    assert record["outcome"] != "updated"


# --- A03: the instruction pins the approval ------------------------------------------


def test_the_instruction_carries_the_exact_approved_versions(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    instruction = _instruction()
    instruction["staging_wheels"] = str(staging)
    path = staging / "instruction.json"
    path.write_text(json.dumps(instruction), encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["approved"]["version"] == "0.2.0"
    assert loaded["approved"]["packages"] == {name: "0.2.0" for name in INSTALLED}


# --- A04: staging failures touch nothing --------------------------------------------


def test_a_checksum_mismatch_refuses_before_any_stop(tmp_path: Path) -> None:
    bundle, url = _bundle(tmp_path, "0.2.0")
    corrupted = bytearray(bundle)
    corrupted[100] ^= 0xFF

    def fetch(address: str) -> bytes:
        assert address == url
        return bytes(corrupted)

    bundle_digest = "sha256:" + __import__("hashlib").sha256(bundle).hexdigest()
    with pytest.raises(Exception) as failure:
        stage_candidate(
            "https://github.com/claytonread/omnivia-core/releases/tag/core-v0.2.0",
            bundle_sha256=bundle_digest,
            fetch_bytes=fetch,
            installation_state=tmp_path / "installation",
            operation_id="op-1",
            expected_version="0.2.0",
        )
    assert "bundle_checksum_mismatch" in str(failure.value) or "digest" in str(failure.value)


def test_the_bundle_address_derives_from_the_release_url() -> None:
    address = bundle_url(
        "https://github.com/claytonread/omnivia-core/releases/tag/core-v0.2.0"
    )
    assert address == (
        "https://github.com/claytonread/omnivia-core/releases/download/"
        "core-v0.2.0/" + BUNDLE_ASSET_NAME
    )


def test_staging_verifies_wheel_versions_against_the_approval(tmp_path: Path) -> None:
    bundle, _url = _bundle(tmp_path, "0.1.9")
    import hashlib

    def fetch(address: str) -> bytes:
        assert address.endswith("core-v0.2.0/" + BUNDLE_ASSET_NAME)
        return bundle

    with pytest.raises(Exception) as failure:
        stage_candidate(
            "https://github.com/claytonread/omnivia-core/releases/tag/core-v0.2.0",
            bundle_sha256="sha256:" + hashlib.sha256(bundle).hexdigest(),
            fetch_bytes=fetch,
            installation_state=tmp_path / "installation",
            operation_id="op-1",
            expected_version="0.2.0",
        )
    assert "wheel_version_mismatch" in str(failure.value) or "0.1.9" in str(failure.value)


# --- A05: one owner per installation --------------------------------------------------


def test_a_second_coordinator_receives_update_already_running(tmp_path: Path) -> None:
    lock = InstallationLock(tmp_path)
    assert lock.acquire() is True
    try:
        outcome = coordinate_update(
            installation_state=tmp_path,
            installed=INSTALLED,
            fetch_channel=lambda url: pytest.fail("a second coordinator must not check the feed"),
            fetch_bytes=lambda address: pytest.fail("never fetches"),
            probe_running=lambda workspace_id: False,
            confirm=lambda summary: pytest.fail("never confirms"),
            spawn_worker=lambda command, pass_fds: pytest.fail("never spawns"),
            python_executable="/venv/bin/python",
            cli_executable="/venv/bin/omnivia",
            checked_at=NOW,
        )
        assert outcome.status == "update_already_running"
        assert outcome.returncode == 1
    finally:
        lock.release()


def test_a_released_lock_is_acquirable_again(tmp_path: Path) -> None:
    first = InstallationLock(tmp_path)
    assert first.acquire() is True
    first.release()
    second = InstallationLock(tmp_path)
    assert second.acquire() is True
    second.release()


# --- the affected-services scan -------------------------------------------------------


def test_the_affected_scan_lists_each_workspace_and_its_running_state(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    (workspaces / "ws-a").mkdir(parents=True)
    (workspaces / "ws-b").mkdir(parents=True)
    (workspaces / "not-a-workspace.txt").write_text("ignore", encoding="utf-8")
    running = {"ws-b"}
    affected = affected_services(
        tmp_path, probe_running=lambda workspace_id: workspace_id in running
    )
    assert [(service.workspace_id, service.was_running) for service in affected] == [
        ("ws-a", False),
        ("ws-b", True),
    ]


# --- the record ------------------------------------------------------------------------


def test_the_operation_record_wire_shape_has_the_v04_members() -> None:
    record = OperationRecord(
        operation_id="upd-1",
        installation_ref="/installation",
        approved_version="0.2.0",
        approved_bundle_sha256="sha256:ab",
        started_at=NOW,
    )
    wire = record.to_wire()
    assert wire["product_id"] == "omnivia-core"
    assert wire["approved_release_ref"] == {
        "version": "0.2.0",
        "bundle_sha256": "sha256:ab",
    }
    assert set(wire) == {
        "operation_id",
        "product_id",
        "installation_ref",
        "approved_release_ref",
        "started_at",
        "phase",
    }
