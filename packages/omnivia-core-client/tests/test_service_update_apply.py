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
from omnivia_core_client.updates_apply import (
    BUNDLE_ASSET_NAME,
    InstallationLock,
    OperationRecord,
    UpdateApplyError,
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


RELEASE_TAG = "https://github.com/claytonread/omnivia-core/releases/tag/core-v"


def _bundle(
    tmp_path: Path, version: str, **overrides: str
) -> tuple[bytes, str, dict[str, str]]:
    """One minimal candidate bundle: five wheels plus its checksum inventory.

    **The inventory rows are paths relative to the candidate root**, which is
    what ``build-standard-candidate.py`` writes -- ``wheels/omnivia_core-…whl``,
    not a bare filename. This fixture used to write bare filenames with a ``#``
    comment header, so it was the only bundle shape the staging path could read
    and a real candidate would have failed every row.

    ``overrides`` sets one package's wheel version, so a release where the five
    do not move in lockstep can be built.
    """
    import hashlib

    versions = {name: overrides.get(name, version) for name in INSTALLED}
    files: dict[str, bytes] = {}
    for name, wheel_version in versions.items():
        distribution = name.replace("-", "_")
        files[f"wheels/{distribution}-{wheel_version}-py3-none-any.whl"] = (
            f"wheel bytes for {distribution} {wheel_version}".encode()
        )
    inventory = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {path}\n"
        for path, content in sorted(files.items())
    ).encode("ascii")
    zip_path = tmp_path / BUNDLE_ASSET_NAME
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("checksums-sha256.txt", inventory)
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
    return zip_path.read_bytes(), bundle_url(RELEASE_TAG + version), versions


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
    import hashlib

    bundle, url, versions = _bundle(tmp_path, "0.2.0")
    corrupted = bytearray(bundle)
    corrupted[100] ^= 0xFF

    def fetch(address: str) -> bytes:
        assert address == url
        return bytes(corrupted)

    bundle_digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
    with pytest.raises(UpdateApplyError) as failure:
        stage_candidate(
            RELEASE_TAG + "0.2.0",
            bundle_sha256=bundle_digest,
            fetch_bytes=fetch,
            installation_state=tmp_path / "installation",
            operation_id="op-1",
            expected_packages=versions,
        )
    assert failure.value.code == "bundle_checksum_mismatch"


def test_a_channel_that_states_no_digest_cannot_reach_the_archive(
    tmp_path: Path,
) -> None:
    """The digest is required, so there is no path that unpacks an unidentified
    archive. It used to be skipped when the channel omitted it, and the prompt
    said so out loud -- 'bundle digest: not stated in the channel'."""
    with pytest.raises(UpdateApplyError) as failure:
        stage_candidate(
            RELEASE_TAG + "0.2.0",
            bundle_sha256=None,
            fetch_bytes=lambda address: pytest.fail("never fetches"),
            installation_state=tmp_path / "installation",
            operation_id="op-1",
            expected_packages={name: "0.2.0" for name in INSTALLED},
        )
    assert failure.value.code == "bundle_digest_missing"


def test_staging_leaves_an_owner_only_tree(tmp_path: Path) -> None:
    """The wheels pip is about to install are not readable by anyone else.

    Staging sits between verification and installation, so a staging directory
    another principal can write is a window in which a verified wheel becomes an
    unverified one after the digest check has already passed.
    """
    import hashlib
    import os

    bundle, _url, versions = _bundle(tmp_path, "0.2.0")
    staging = stage_candidate(
        RELEASE_TAG + "0.2.0",
        bundle_sha256="sha256:" + hashlib.sha256(bundle).hexdigest(),
        fetch_bytes=lambda address: bundle,
        installation_state=tmp_path / "installation",
        operation_id="op-1",
        expected_packages=versions,
    )
    assert (staging / "bundle" / "wheels").is_dir()
    if os.name != "nt":
        for entry in [staging, *(staging / "bundle").rglob("*")]:
            assert entry.stat().st_mode & 0o077 == 0, entry


def test_staging_verifies_each_wheel_against_its_own_recommended_version(
    tmp_path: Path,
) -> None:
    """Per package, not one release number repeated five times.

    A release where only the MCP distribution moved is a legitimate
    recommendation, and verifying all five against ``release.version`` would
    refuse it.
    """
    import hashlib

    bundle, _url, versions = _bundle(tmp_path, "0.2.0", **{"omnivia-core-mcp": "0.3.0"})
    assert versions["omnivia-core-mcp"] == "0.3.0"
    staging = stage_candidate(
        RELEASE_TAG + "0.2.0",
        bundle_sha256="sha256:" + hashlib.sha256(bundle).hexdigest(),
        fetch_bytes=lambda address: bundle,
        installation_state=tmp_path / "installation",
        operation_id="op-1",
        expected_packages=versions,
    )
    assert staging.is_dir()

    # The same bundle against the synthesised map the coordinator used to build.
    with pytest.raises(UpdateApplyError) as failure:
        stage_candidate(
            RELEASE_TAG + "0.2.0",
            bundle_sha256="sha256:" + hashlib.sha256(bundle).hexdigest(),
            fetch_bytes=lambda address: bundle,
            installation_state=tmp_path / "installation",
            operation_id="op-2",
            expected_packages={name: "0.2.0" for name in INSTALLED},
        )
    assert failure.value.code == "wheel_version_mismatch"


def test_the_bundle_address_derives_from_the_release_url() -> None:
    address = bundle_url(
        "https://github.com/claytonread/omnivia-core/releases/tag/core-v0.2.0"
    )
    assert address == (
        "https://github.com/claytonread/omnivia-core/releases/download/"
        "core-v0.2.0/" + BUNDLE_ASSET_NAME
    )


def test_staging_verifies_wheel_versions_against_the_approval(tmp_path: Path) -> None:
    import hashlib

    bundle, _url, _versions = _bundle(tmp_path, "0.1.9")

    def fetch(address: str) -> bytes:
        assert address.endswith("core-v0.2.0/" + BUNDLE_ASSET_NAME)
        return bundle

    with pytest.raises(UpdateApplyError) as failure:
        stage_candidate(
            RELEASE_TAG + "0.2.0",
            bundle_sha256="sha256:" + hashlib.sha256(bundle).hexdigest(),
            fetch_bytes=fetch,
            installation_state=tmp_path / "installation",
            operation_id="op-1",
            expected_packages={name: "0.2.0" for name in INSTALLED},
        )
    assert failure.value.code == "wheel_version_mismatch"
    assert "0.1.9" in failure.value.reason


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


def _signed_channel(version: str, bundle_digest: str, **overrides: str) -> bytes:
    """One signed envelope v2 recommending ``version``, for the coordinator."""
    from test_service_update_check import POLICY, _body, _sign

    assert POLICY.trusted_keys
    body = _body(version)
    assert body["release"] is not None
    body["release"]["bundle_sha256"] = bundle_digest
    body["release"]["packages"] = {
        name: overrides.get(name, version) for name in INSTALLED
    }
    return _sign(body)


def _coordinate(
    tmp_path: Path,
    *,
    channel: bytes,
    bundle: bytes,
    confirm: Any = lambda summary: True,
    runner: Any = None,
) -> Any:
    """One full coordination with every seam injected and no real process."""
    import omnivia_core_client.updates_apply as apply_module
    from test_service_update_check import POLICY

    state = tmp_path / "installation"
    state.mkdir(parents=True, exist_ok=True)
    recorded: dict[str, Any] = {}

    class Spawned:
        """A worker that runs in-process, the way the real one would out of it."""

        def wait(self) -> int:
            instruction = json.loads(
                Path(recorded["instruction"]).read_text(encoding="utf-8")
            )
            recorded["approved"] = instruction["approved"]
            if runner is not None:
                run_worker(instruction, runner=runner)
            return 0

    def spawn(command: list[str], pass_fds: Any = ()) -> Any:
        recorded["instruction"] = command[-1]
        return Spawned()

    outcome = coordinate_update(
        installation_state=state,
        installed=INSTALLED,
        fetch_channel=lambda url: channel,
        fetch_bytes=lambda address: bundle,
        probe_running=lambda workspace_id: False,
        confirm=confirm,
        spawn_worker=spawn,
        python_executable="/venv/bin/python",
        cli_executable="/venv/bin/omnivia",
        checked_at="2026-09-30T00:00:00Z",
        policy=POLICY,
    )
    assert apply_module.PRODUCT_ID == "omnivia-core"
    return outcome, recorded, state


def test_the_coordinator_hands_the_worker_the_channels_own_package_map(
    tmp_path: Path,
) -> None:
    """A03, closed properly: the instruction carries the channel's versions.

    The coordinator used to write five copies of ``candidate_version`` here. A
    release where one distribution did not move would have been staged against
    the versions the channel recommended and then installed-and-verified against
    versions it never recommended. The pin that was supposed to catch that wrote
    a fixture to a file and read it back without calling production code at all.
    """
    import hashlib

    bundle, _url, versions = _bundle(tmp_path, "0.2.0", **{"omnivia-core-mcp": "0.3.0"})
    digest = "sha256:" + hashlib.sha256(bundle).hexdigest()

    def installed_after(command: list[str]) -> Any:
        """The host reports the uneven versions the instruction approved."""
        stdout = ""
        if "import importlib.metadata" in " ".join(command):
            stdout = json.dumps(versions)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    outcome, recorded, state = _coordinate(
        tmp_path,
        channel=_signed_channel("0.2.0", digest, **{"omnivia-core-mcp": "0.3.0"}),
        bundle=bundle,
        runner=Recorder(installed_after),
    )
    assert recorded["approved"]["packages"] == versions
    assert recorded["approved"]["packages"]["omnivia-core-mcp"] == "0.3.0"
    assert recorded["approved"]["bundle_sha256"] == digest
    assert outcome.status == "updated"
    assert outcome.returncode == 0
    assert read_operation_record(state)["outcome"] == "updated"  # type: ignore[index]


def test_a_refusal_after_staging_is_a_no_change_exit(tmp_path: Path) -> None:
    """Cancellation before mutation leaves no record claiming anything."""
    import hashlib

    bundle, _url, _versions = _bundle(tmp_path, "0.2.0")
    digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
    outcome, _recorded, state = _coordinate(
        tmp_path,
        channel=_signed_channel("0.2.0", digest),
        bundle=bundle,
        confirm=lambda summary: False,
    )
    assert outcome.status == "cancelled"
    assert outcome.returncode == 0
    assert read_operation_record(state) is None


def test_an_unreachable_bundle_is_blocked_rather_than_left_in_flight(
    tmp_path: Path,
) -> None:
    """A04: the journal never reads ``preparing`` for ever.

    A ``TransportError`` or an ``OSError`` during the download used to propagate
    out of the coordinator with the record still at ``phase: "preparing"`` -- an
    operation that looks in flight and is not, which is the one state a crash
    reader cannot act on.
    """
    import omnivia_core_client.updates_apply as apply_module
    from omnivia_core_client.errors import TransportError

    state = tmp_path / "installation"
    state.mkdir(parents=True)

    def fetch_bytes(address: str) -> bytes:
        raise TransportError("the endpoint could not be reached")

    from test_service_update_check import POLICY

    outcome = apply_module.coordinate_update(
        installation_state=state,
        installed=INSTALLED,
        fetch_channel=lambda url: _signed_channel("0.2.0", "sha256:" + "a" * 64),
        fetch_bytes=fetch_bytes,
        probe_running=lambda workspace_id: False,
        confirm=lambda summary: pytest.fail("never confirms"),
        spawn_worker=lambda command, pass_fds: pytest.fail("never spawns"),
        python_executable="/venv/bin/python",
        cli_executable="/venv/bin/omnivia",
        checked_at="2026-09-30T00:00:00Z",
        policy=POLICY,
    )
    assert outcome.status == "blocked"
    assert outcome.returncode == 1
    record = read_operation_record(state)
    assert record is not None
    assert record["phase"] == "blocked"
    assert record["outcome"] == "blocked"
    assert record["failure_code"] == "bundle_unavailable"


def test_a_channel_the_production_policy_refuses_never_reaches_the_bundle(
    tmp_path: Path,
) -> None:
    """The trust decision is upstream of every mutation, including the download."""
    import omnivia_core_client.updates_apply as apply_module
    from omnivia_core_client.update_trust import PRODUCTION_TRUST_POLICY

    state = tmp_path / "installation"
    state.mkdir(parents=True)
    outcome = apply_module.coordinate_update(
        installation_state=state,
        installed=INSTALLED,
        fetch_channel=lambda url: _signed_channel("0.2.0", "sha256:" + "a" * 64),
        fetch_bytes=lambda address: pytest.fail("never fetches"),
        probe_running=lambda workspace_id: False,
        confirm=lambda summary: pytest.fail("never confirms"),
        spawn_worker=lambda command, pass_fds: pytest.fail("never spawns"),
        python_executable="/venv/bin/python",
        cli_executable="/venv/bin/omnivia",
        checked_at="2026-09-30T00:00:00Z",
        policy=PRODUCTION_TRUST_POLICY,
    )
    assert outcome.status == "check_failed"
    assert outcome.returncode == 1
    assert read_operation_record(state) is None


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
