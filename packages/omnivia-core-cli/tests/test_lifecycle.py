"""`omnivia start`, `stop` and `status`, driven as real processes.

The CLI is exercised as a subprocess, as `test_workspace_show.py` is and for the
same reason: it is the only arrangement in which "the CLI does not import the
runtime" is proven rather than asserted. Here it matters twice over, because the
thing under test *is* process control -- a test that called `main()` in-process
would be checking a function that spawns, not a command that starts a service.

The runtime is imported in the test process to build the workspace the service
will own. Nothing under test imports it.

**The case that carries the file** is
`test_status_refuses_a_service_that_died_without_unwinding`. The descriptor is
written once, at startup, and never rewritten -- `publish()` has a single call
site -- so `ready: true` freezes there and a hard-killed service leaves a file
still claiming health. Reading that file is not a status check. `status` has to
dial, and this test kills a service in the one way that runs no handler and then
insists the answer is "not running".
"""

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

#: Kept short deliberately. R004-15 caps a local endpoint at `MAX_ENDPOINT_PATH_BYTES`
#: encoded bytes -- what `sockaddr_un`'s 104-byte `sun_path` leaves once the NUL
#: terminator and the runtime's fixed-width staging name are taken -- so pytest's own
#: `tmp_path` is too deep to serve from. The same reason
#: `test_transport_conformance.py` uses a short `mkdtemp`.
HOME_PREFIX = "ovl-"


def _bootstrap(home: Path) -> None:
    """A workspace a service can own. No shipped command does this yet."""
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


def _cli(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "omnivia_core_cli.main", "--home", str(home), *arguments],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _service_pids(home: Path) -> list[int]:
    """Every live `omnivia-core-service` serving this installation, by its own argv.

    Scoped to the home directory rather than to the executable name, so a service
    another test or another checkout left running cannot be counted or killed.

    `-ww` is load-bearing, not decoration. `ps` truncates each line to the
    terminal width, which is 80 columns when nothing owns a tty -- and the
    argv this searches for is ~219 characters, with the installation-state
    marker starting around column 136. Without it, every one of these tests
    passes on a developer's terminal and fails in CI, having found no process
    for a service that started perfectly well.
    """
    listing = subprocess.run(
        ["ps", "-eww", "-o", "pid=,args="], capture_output=True, text=True, check=False
    ).stdout
    marker = str(home / "installation-state")
    found = []
    for line in listing.splitlines():
        pid, _, args = line.strip().partition(" ")
        if "omnivia-core-service" in args and marker in args and pid.isdigit():
            found.append(int(pid))
    return found


#: Substrings that must never appear in a machine document, whatever produced it.
#: The v1 keys `service` and `reason` are here as literal JSON keys rather than as
#: bare words, so a `service_ref` or a legitimate prose value elsewhere would not
#: trip this while the removed fields returning would.
FORBIDDEN_SUBSTRINGS = (
    '"reason"',
    '"service"',
    '"message"',
    '"details"',
    '"child_output"',
    '"endpoint"',
    '"endpoint_uri"',
    '"pid"',
    '"stderr"',
    '"unmet"',
    '"workspace_id"',
    '"service_instance_id"',
)


def _lifecycle_json(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """Decode the adapter's single stdout document, rejecting prose around it.

    The forbidden-substring sweep runs on every machine document every test in
    this file produces, rather than in one test of its own: the leak this guards
    against is a single branch reaching for a diagnostic, and a single test would
    only cover the branch it happened to exercise.
    """
    assert result.stdout.endswith("\n"), result.stdout
    assert result.stdout.count("\n") == 1, result.stdout
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in result.stdout, (forbidden, result.stdout)
    document = json.loads(result.stdout)
    assert isinstance(document, dict)
    return dict(document)


def _safe_status(document: dict[str, object]) -> dict[str, object]:
    """The document's safe status, decoded through the contract that defines it."""
    from omnivia_core.contracts.v1 import (
        decode_core_safe_status,
        encode_core_safe_status,
    )

    payload = document["safe_status"]
    status = decode_core_safe_status(payload)
    # Round-tripped rather than merely decoded: a document that decodes but does
    # not re-encode to itself is one this adapter did not produce with the encoder.
    assert dict(encode_core_safe_status(status)) == payload
    return dict(payload)


def _descriptor(home: Path) -> dict[str, object] | None:
    runtime = home / "installation-state" / "runtime" / WORKSPACE_ID / "service.json"
    if not runtime.exists():
        return None
    return dict(json.loads(runtime.read_text(encoding="utf-8")))


@pytest.fixture
def home() -> Iterator[Path]:
    """One bootstrapped installation per test, with nothing of it left running."""
    root = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir="/tmp"))
    _bootstrap(root)
    try:
        yield root
    finally:
        for pid in _service_pids(root):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
        shutil.rmtree(root, ignore_errors=True)


def test_status_reports_not_running_before_anything_is_started(home: Path) -> None:
    result = _cli(home, "status")

    assert result.returncode == 1, result.stderr
    assert "not running" in result.stdout + result.stderr
    assert _service_pids(home) == []


def test_start_then_status_then_stop(home: Path) -> None:
    """The whole happy path, against a real service process."""
    started = _cli(home, "start")
    assert started.returncode == 0, started.stderr
    assert "started" in started.stdout
    assert len(_service_pids(home)) == 1

    status = _cli(home, "status")
    assert status.returncode == 0, status.stderr
    assert "running" in status.stdout
    assert WORKSPACE_ID in status.stdout

    stopped = _cli(home, "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert _service_pids(home) == []
    # The service withdraws its own descriptor on a clean unwind. The CLI must not
    # unlink it: `compare_and_clean` is compare-by-instance so that a stale reader
    # cannot un-advertise a healthy owner.
    assert _descriptor(home) is None

    assert _cli(home, "status").returncode == 1


def test_status_json_reports_not_running_as_one_versioned_document(home: Path) -> None:
    result = _cli(home, "status", "--json")

    assert result.returncode == 1
    assert result.stderr == ""
    document = _lifecycle_json(result)
    assert document["lifecycle_adapter_version"] == 2
    assert document["action"] == "status"
    assert document["ok"] is False
    assert document["outcome"] == "not_running"
    assert document["code"] == "status_not_running"
    status = _safe_status(document)
    assert status["lifecycle_state"] == "stopped"
    assert status["connection_state"] == "disconnected"
    assert status["permitted_actions"] == ["start"]
    assert _service_pids(home) == []

    stopped = _cli(home, "stop", "--json")
    assert stopped.returncode == 0
    stopped_document = _lifecycle_json(stopped)
    assert stopped_document["action"] == "stop"
    assert stopped_document["ok"] is True
    assert stopped_document["outcome"] == "not_running"
    assert stopped_document["code"] == "stop_not_running"
    assert _safe_status(stopped_document) == status


def test_start_twice_does_not_start_a_second_service(home: Path) -> None:
    """Idempotent by dialling, not by trusting the descriptor on disk."""
    assert _cli(home, "start").returncode == 0
    first = _service_pids(home)
    assert len(first) == 1

    again = _cli(home, "start")

    assert again.returncode == 0, again.stderr
    assert "already running" in again.stdout
    assert _service_pids(home) == first


def test_status_refuses_a_service_that_died_without_unwinding(home: Path) -> None:
    """The trap this file exists for.

    SIGKILL runs no handler, so the descriptor survives its own service and goes
    on claiming `ready: true`. A `status` that reads the file agrees with it. This
    asserts the descriptor really is stale -- otherwise the test would pass for
    the wrong reason -- and then that `status` says the service is gone anyway.
    """
    assert _cli(home, "start").returncode == 0
    (pid,) = _service_pids(home)

    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 30
    while _service_pids(home) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _service_pids(home) == [], "the service outlived SIGKILL"

    stale = _descriptor(home)
    assert stale is not None, "SIGKILL should leave the descriptor behind"
    assert stale["ready"] is True, "the descriptor should still claim readiness"

    result = _cli(home, "status")

    assert result.returncode == 1, "a dead service was reported as running"
    assert "not running" in result.stdout + result.stderr


def test_start_status_and_stop_share_one_json_adapter_contract(home: Path) -> None:
    started_result = _cli(home, "start", "--json")
    assert started_result.returncode == 0, started_result.stderr
    started = _lifecycle_json(started_result)
    assert started["lifecycle_adapter_version"] == 2
    assert started["action"] == "start"
    assert started["ok"] is True
    assert started["outcome"] == "started"
    assert started["code"] == "start_started"
    running = _safe_status(started)
    target = running["target"]
    assert isinstance(target, dict)
    assert target["kind"] == "local"
    assert target["management"] == "locally_managed"
    assert target["display_name"] == "Local Core"
    assert target["workspace_ref"] == WORKSPACE_ID
    assert target["target_ref"].startswith("core-target-")
    assert target["endpoint_profile_ref"].startswith("core-endpoint-profile-")
    assert running["lifecycle_state"] == "running"
    assert running["readiness_state"] == "ready"
    assert running["connection_state"] == "connected"
    assert running["compatibility_state"] == "compatible"
    # The one action this adapter can perform in this phase, and nothing else.
    assert running["permitted_actions"] == ["stop"]

    attached_result = _cli(home, "start", "--json")
    assert attached_result.returncode == 0, attached_result.stderr
    attached = _lifecycle_json(attached_result)
    assert attached["outcome"] == "attached"
    assert attached["code"] == "start_attached"
    assert _safe_status(attached) == running

    status_result = _cli(home, "status", "--json")
    assert status_result.returncode == 0, status_result.stderr
    status = _lifecycle_json(status_result)
    assert status["action"] == "status"
    assert status["outcome"] == "running"
    assert status["code"] == "status_running"
    live = _safe_status(status)
    # Discovery negotiated these before the call, so `status` -- unlike `start`,
    # which reads the launcher's result -- publishes what the descriptor carries.
    assert isinstance(live["server_version"], str)
    assert isinstance(live["protocol_version"], str)
    assert {key: live[key] for key in running} == running

    stopped_result = _cli(home, "stop", "--json")
    assert stopped_result.returncode == 0, stopped_result.stderr
    stopped = _lifecycle_json(stopped_result)
    assert stopped["action"] == "stop"
    assert stopped["outcome"] == "stopped"
    assert stopped["code"] == "stop_stopped"
    gone = _safe_status(stopped)
    assert gone["lifecycle_state"] == "stopped"
    assert gone["permitted_actions"] == ["start"]
    # One installation, one identity, across every phase of its life.
    assert gone["target"] == target
    assert _service_pids(home) == []


def test_start_json_failure_names_a_code_and_never_the_home_it_failed_in() -> None:
    """The failure path is where the v1 document leaked the most.

    `reason` on an uninitialised home was a sentence naming the workspace
    directory. The sentinel is the temporary directory's own name, so this fails
    if any part of the path reaches stdout in any field -- and the human stderr
    is asserted to still carry it, because the diagnostic a person reads did not
    change.
    """
    empty_home = Path(tempfile.mkdtemp(prefix=f"{HOME_PREFIX}sentinel-", dir="/tmp"))
    try:
        result = _cli(empty_home, "start", "--json")
        human = _cli(empty_home, "start")
    finally:
        shutil.rmtree(empty_home, ignore_errors=True)

    assert result.returncode == 1
    document = _lifecycle_json(result)
    assert document["lifecycle_adapter_version"] == 2
    assert document["action"] == "start"
    assert document["ok"] is False
    assert document["outcome"] == "failed"
    assert document["code"] == "start_failed"
    # No manifest, so no workspace identity, so no target can be formed at all.
    assert "safe_status" not in document
    assert empty_home.name not in result.stdout

    assert human.returncode == 1
    assert human.stdout == ""
    assert str(empty_home) in human.stderr


@pytest.mark.skipif(os.name == "nt", reason="the byte ceiling is a Unix-socket limit")
@pytest.mark.parametrize("command", ["start", "init"])
def test_the_cli_refuses_one_byte_past_the_endpoint_ceiling_and_not_at_it(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """The CLI's own copy of R004-15's ceiling, at both boundaries and both entries.

    This guard exists because ADR-036 forbids this package importing the runtime, so
    it restates the number. It used to restate 104 -- the raw `sun_path` field size
    -- and check the endpoint against it, leaving it 11 to 17 bytes looser than the
    runtime's guard: precisely the paths that passed here and then failed to bind in
    the service this command spawns.
    `test_socket_path_ceiling.py::test_the_cli_restates_the_runtime_ceiling_exactly`
    fails if the two numbers drift; this fails if the comparison around one of them
    does.

    **`init` is parametrized in because deleting its call to
    `refuse_over_long_endpoint` passed this suite 49 out of 49.** The check has its
    own argument in `request_init`'s docstring -- a home that can be initialised but
    never served is worse than a refusal, because the workspace would be real and
    every later `omnivia start` would fail on a fact that was knowable at `init` --
    and nothing objected to removing it. One parameter is the whole repair: the
    ceiling is one number and this is now one test over every entry point that
    restates it.

    `_ask_service` is substituted so that neither branch launches anything. It is
    the launch seam and nothing else: the ceiling is checked before it on both
    paths, so a refusal that is *not* the length one proves the boundary was passed.
    `request_init` would otherwise bootstrap a real workspace on the good side of
    the line, which is `test_a_fresh_home_is_initialised_and_then_starts`'s job.
    """
    from omnivia_core_cli import lifecycle
    from omnivia_core_cli.lifecycle import (
        MAX_ENDPOINT_PATH_BYTES,
        Installation,
        LifecycleError,
        request_init,
        request_managed_start,
    )

    def never_launch(*arguments: object, **keywords: object) -> dict[str, object]:
        raise LifecycleError("the launcher was reached")

    monkeypatch.setattr(lifecycle, "_ask_service", never_launch)

    def ask(installation: Installation) -> None:
        if command == "init":
            request_init(installation)
        else:
            request_managed_start(installation, endpoint_uri="unix:///x")

    root = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir="/tmp"))

    def installation_of(total: int) -> Installation:
        fill = total - len(str(root).encode()) - len("//run/s.sock")
        assert fill >= 1, f"{root} is too deep for a {total}-byte endpoint"
        made = Installation(home=root / ("h" * fill))
        assert len(str(made.socket_path).encode()) == total
        return made

    try:
        with pytest.raises(LifecycleError) as at_the_line:
            ask(installation_of(MAX_ENDPOINT_PATH_BYTES))
        assert "byte limit" not in str(at_the_line.value)

        with pytest.raises(LifecycleError, match="byte limit") as past_it:
            ask(installation_of(MAX_ENDPOINT_PATH_BYTES + 1))
        assert str(MAX_ENDPOINT_PATH_BYTES) in str(past_it.value)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_stop_json_over_a_stale_descriptor_offers_start_and_never_stop(home: Path) -> None:
    """A descriptor whose service is gone: normalized, and safe about which action.

    Nothing answered, so no ownership was established and `stop` must not be
    offered. `start` may be, and only because this installation's start path is
    attach-first and recovers from exactly this descriptor --
    `test_start_recovers_from_a_descriptor_left_by_a_dead_service` is the same
    fact from the other side.
    """
    assert _cli(home, "start").returncode == 0
    (pid,) = _service_pids(home)
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 30
    while _service_pids(home) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _descriptor(home) is not None, "SIGKILL should leave the descriptor behind"

    result = _cli(home, "stop", "--json")

    assert result.returncode == 1
    document = _lifecycle_json(result)
    assert document["outcome"] == "failed"
    assert document["code"] == "stop_service_unreachable"
    status = _safe_status(document)
    assert status["lifecycle_state"] == "unknown"
    assert status["connection_state"] == "unreachable"
    assert status["warning_codes"] == ["endpoint_unreachable"]
    assert status["permitted_actions"] == ["start"]


def test_start_recovers_from_a_descriptor_left_by_a_dead_service(home: Path) -> None:
    """A stale descriptor must not make `start` believe a service is already up."""
    assert _cli(home, "start").returncode == 0
    (first,) = _service_pids(home)
    os.kill(first, signal.SIGKILL)
    deadline = time.monotonic() + 30
    while _service_pids(home) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _descriptor(home) is not None

    result = _cli(home, "start")

    assert result.returncode == 0, result.stderr
    assert "started" in result.stdout
    replacement = _service_pids(home)
    assert len(replacement) == 1
    assert replacement != [first]
