"""Service-owned managed start, driven as real processes (R004-08, R004-11).

Owner resolution 004 §11 lists six things managed start has to be shown to do, and
this file is one test per item plus the production-integration evidence R004-09
asks for. They are exercised as processes rather than as function calls, because
every one of them is a claim about processes: that only one service exists, that a
failed child was taken away, that the *service* holds the writable lease and the
launcher does not.

**The concurrency case is the load-bearing one, and asserting "one service
process" is not enough on its own.** The runtime already refuses a loser with
"another service holds the lifetime storage lock", so several starters racing
without any mutex still converge on one surviving process -- the count passes while
proving nothing. What the mutex adds is that every starter comes back with a
*usable* result naming that one service: one `started`, the rest `attached`, one
service instance id between them. Remove the mutex and the losers come back
`spawn_failure` instead, which is what this asserts against.

`ps -eww` is not decoration. `ps` truncates each line to the terminal width, which
is 80 columns when nothing owns a tty, and the argv searched for here is far longer
than that -- without it every one of these tests passes locally and fails in CI
having found no process for a service that started perfectly well.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.ownership.discovery import discover
from omnivia_core_runtime.ownership.locks import LockRole, create_lock
from omnivia_core_runtime.service.managed_start import (
    MANAGED_START_VERSION,
    ManagedStartFailure,
    ManagedStartStatus,
    _clean_child_descriptor,
    managed_start,
)
from omnivia_core_runtime.storage.backup import InstallationLayout

WORKSPACE_ID = "ws-managed-start-0001"

#: Kept short deliberately. `sockaddr_un` caps a socket address at 104 bytes, of
#: which 103 are usable, and the runtime binds a longer staging name before
#: renaming -- so pytest's own `tmp_path` is too deep to serve from.
HOME_PREFIX = "ovms-"

#: The console script under test. Located the way an adapter would locate it.
SERVICE_EXECUTABLE = "omnivia-core-service"


def _bootstrap(home: Path) -> None:
    """A workspace a service can own. No shipped command does this yet."""
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
            created_at="2026-08-07T00:00:00+00:00",
            name="managed start tests",
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version="0.1.0"
            ),
        ),
        service_instance_id="svc-managed-start-tests",
    )


def _locate() -> str:
    """The service console script, or skip: these tests are about the real one."""
    found = shutil.which(SERVICE_EXECUTABLE)
    if found is not None:
        return found
    beside = Path(sys.executable).parent / SERVICE_EXECUTABLE
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    pytest.skip(f"{SERVICE_EXECUTABLE} is not installed in this environment")


def _service_pids(home: Path) -> list[int]:
    """Every live *service* for this installation, by its own argv.

    Scoped to the home directory, so a service another test or another checkout
    left running cannot be counted or killed. A managed-start launcher runs the
    same executable against the same installation, so it is excluded explicitly:
    counting one as a service would report two services during every start.
    """
    listing = subprocess.run(
        ["ps", "-eww", "-o", "pid=,args="], capture_output=True, text=True, check=False
    ).stdout
    marker = str(home / "installation-state")
    found = []
    for line in listing.splitlines():
        pid, _, args = line.strip().partition(" ")
        if not pid.isdigit() or marker not in args:
            continue
        if SERVICE_EXECUTABLE not in args or "--managed-start" in args:
            continue
        found.append(int(pid))
    return found


def _launcher_pids(home: Path) -> list[int]:
    """Every live managed-start launcher for this installation. The complement of
    `_service_pids`: a launcher arbitrates and exits, and owns nothing while it
    lives."""
    listing = subprocess.run(
        ["ps", "-eww", "-o", "pid=,args="], capture_output=True, text=True, check=False
    ).stdout
    marker = str(home / "installation-state")
    return [
        int(pid)
        for pid, _, args in (line.strip().partition(" ") for line in listing.splitlines())
        if pid.isdigit() and marker in args and "--managed-start" in args
    ]


def _endpoint(home: Path) -> str:
    return f"unix://{home / 's.sock'}"


def _runtime_directory(home: Path) -> Path:
    return InstallationLayout(root=home / "installation-state").runtime_for(WORKSPACE_ID)


def _descriptor_document(home: Path) -> dict[str, Any] | None:
    path = _runtime_directory(home) / "service.json"
    if not path.is_file():
        return None
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _run(home: Path, *extra: str) -> tuple[int, dict[str, Any], str]:
    """One managed start, as an adapter would invoke it: argv in, one document out."""
    completed = subprocess.run(
        [
            _locate(),
            "--managed-start",
            "--workspace",
            str(home / "workspace"),
            "--installation-state",
            str(home / "installation-state"),
            "--endpoint",
            _endpoint(home),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    return completed.returncode, json.loads(completed.stdout), completed.stderr


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


# --- R004-11: two or more concurrent starters produce one authoritative service ---


def test_concurrent_starters_converge_on_one_authoritative_service(home: Path) -> None:
    """Four managed starts, launched together, against one workspace.

    Launched with `Popen` and only then waited on, so they genuinely overlap: a loop
    that ran one to completion before starting the next would serialise the very
    thing under test and pass without any coordination at all.

    Three assertions, and the second and third are the ones the mutex is needed for:

    1. exactly one service process exists -- the lifetime storage lock would also
       produce this, so it proves the outcome and not the mechanism;
    2. every starter came back usable, exactly one having started the service and
       the rest having attached to it. Without the mutex the losers spawn too, lose
       the storage lock, and come back `spawn_failure`;
    3. all four name the same service instance, so there is one authoritative
       service and not several agreeing by coincidence.
    """
    starters = 4
    running = [
        subprocess.Popen(
            [
                _locate(),
                "--managed-start",
                "--workspace",
                str(home / "workspace"),
                "--installation-state",
                str(home / "installation-state"),
                "--endpoint",
                _endpoint(home),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(starters)
    ]
    results = []
    for launcher in running:
        stdout, stderr = launcher.communicate(timeout=240)
        assert launcher.returncode == 0, f"a starter failed: {stderr}"
        results.append(json.loads(stdout))

    assert len(_service_pids(home)) == 1, "more than one service is running"

    statuses = sorted(result["status"] for result in results)
    assert statuses == ["attached"] * (starters - 1) + ["started"], statuses

    instances = {result["service"]["service_instance_id"] for result in results}
    assert len(instances) == 1, f"starters disagree about the service: {instances}"
    assert all(result["service"]["ready"] is True for result in results)


# --- R004-11: an existing compatible ready service is reused ---


def test_an_existing_compatible_ready_service_is_reused(home: Path) -> None:
    """The second start attaches to the first service rather than making another."""
    code, first, _ = _run(home)
    assert code == 0
    assert first["status"] == ManagedStartStatus.STARTED.value
    (pid,) = _service_pids(home)

    code, second, _ = _run(home)

    assert code == 0
    assert second["status"] == ManagedStartStatus.ATTACHED.value
    assert second["failure"] is None
    assert _service_pids(home) == [pid], "the reused service was replaced"
    assert (
        second["service"]["service_instance_id"]
        == first["service"]["service_instance_id"]
    )


# --- R004-11: an incompatible service is not replaced silently ---


def test_an_incompatible_service_is_not_replaced_silently(home: Path) -> None:
    """A live service advertising a window this build cannot negotiate is left alone.

    The descriptor of a real, running, answering service is rewritten to advertise
    an API window outside this build's -- so the service is genuinely there, its pid
    is alive and its endpoint answers, and only the negotiation fails. That is the
    case worth testing: a stale file is refused by liveness long before
    compatibility is ever consulted.
    """
    code, started, _ = _run(home)
    assert code == 0
    (pid,) = _service_pids(home)

    document = _descriptor_document(home)
    assert document is not None
    document["supported_api_versions"] = {"minimum": "9.9", "maximum": "9.9"}
    (_runtime_directory(home) / "service.json").write_text(
        json.dumps(document), encoding="utf-8"
    )

    code, result, stderr = _run(home)

    assert code == 1
    assert result["status"] == ManagedStartStatus.FAILED.value
    assert result["failure"] == ManagedStartFailure.INCOMPATIBLE_SERVICE.value
    # Not replaced, not stopped, and not raced by a second service.
    assert _service_pids(home) == [pid]
    assert _descriptor_document(home) == document
    # Not silent: the refusal names what happened, on the human stream.
    assert "incompatible" in (result["reason"] + stderr).lower()
    assert (
        started["service"]["service_instance_id"]
        == result["service"]["service_instance_id"]
    )


# --- R004-11: a failed child is cleaned up and leaves no false ready descriptor ---


#: A stand-in service that advertises readiness it does not have, then does nothing.
#:
#: This is the exact failure the packet warns about: `publish()` has one call site,
#: so `ready: true` freezes at startup and nothing ever rewrites it. A launcher that
#: waited for the descriptor to appear would call this a successful start. Nothing
#: listens on the endpoint, so a launcher that dials finds out.
_FAKE_SERVICE = '''#!{python}
import argparse, json, os, sys, time
from pathlib import Path
sys.path[:0] = {syspath!r}
from omnivia_core_runtime.ownership.identity import SystemProcessEvidence
from omnivia_core_runtime.storage.backup import InstallationLayout

parser = argparse.ArgumentParser()
parser.add_argument("--workspace")
parser.add_argument("--installation-state")
parser.add_argument("--endpoint")
parser.add_argument("--core-version", default="0.1.0")
args = parser.parse_args()

evidence = SystemProcessEvidence().current()
runtime = InstallationLayout(root=Path(args.installation_state)).runtime_for(
    {workspace_id!r}
)
runtime.mkdir(parents=True, exist_ok=True)
(runtime / "service.json").write_text(json.dumps({{
    "descriptor_version": "1.2",
    "endpoint_uri": args.endpoint,
    "fencing_generation": 99,
    "installation_id": "inst-fake",
    "lifecycle_state": "ready",
    "process": {{
        "boot_id": evidence.boot_id,
        "pid": os.getpid(),
        "start_time": evidence.start_time,
    }},
    "protocol_version": "1.0",
    "published_at": "2026-08-07T00:00:00Z",
    "ready": True,
    "server_version": "0.1.0",
    "service_instance_id": "svc-fake-never-answers",
    "supported_api_versions": {{"minimum": "1.2", "maximum": "1.2"}},
    "supported_workspace_versions": {{"minimum": "1.0", "maximum": "1.0"}},
    "workspace_format_version": "1.0",
    "workspace_id": {workspace_id!r},
}}), encoding="utf-8")
sys.stdout.write("fake service advertised readiness it does not have\\n")
sys.stdout.flush()
time.sleep(600)
'''


@pytest.fixture
def fake_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a service that never answers first on `PATH`."""
    directory = tmp_path / "fake-bin"
    directory.mkdir()
    script = directory / SERVICE_EXECUTABLE
    script.write_text(
        _FAKE_SERVICE.format(
            python=sys.executable,
            syspath=[path for path in sys.path if path],
            workspace_id=WORKSPACE_ID,
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    return script


def test_a_failed_child_is_cleaned_up_and_leaves_no_false_ready_descriptor(
    home: Path, fake_service: Path
) -> None:
    """The child published `ready: true` and never answered. Both must be gone.

    Called in-process so the fake on `PATH` is what gets spawned -- the console
    script would resolve `sys.argv[0]` to itself first, which is correct in
    production and unhelpful here.

    Three things are asserted, and the third is the one the packet names: the result
    is a readiness failure rather than a success, the child is not left running, and
    the descriptor it published is gone. A launcher that only terminated the child
    would leave a file claiming a ready service at a dead pid, and the next launcher
    would believe it and connect to nothing.
    """
    assert _descriptor_document(home) is None

    result = managed_start(
        workspace_root=home / "workspace",
        installation_root=home / "installation-state",
        endpoint_uri=_endpoint(home),
        timeout_seconds=5.0,
    )

    assert result.status is ManagedStartStatus.FAILED
    assert result.failure is ManagedStartFailure.READINESS_FAILURE
    assert "advertised readiness it does not have" in result.child_output

    assert _service_pids(home) == [], "the failed child was left running"
    assert _descriptor_document(home) is None, "a false ready descriptor was left"
    assert discover(_runtime_directory(home)) is None


def test_failed_child_cleanup_compares_before_it_removes(home: Path) -> None:
    """A failed start may only un-advertise its *own* child.

    The same rule `compare_and_clean` enforces with a service-instance id, enforced
    here with the child's pid -- a launcher never learns the instance id of a child
    that died before it could answer, but it always knows which process it spawned.
    Without the comparison a failed start would take a healthy service's descriptor
    away with it, and nothing would put it back.
    """
    code, _result, _ = _run(home)
    assert code == 0
    published = _descriptor_document(home)
    assert published is not None
    (service_pid,) = _service_pids(home)

    runtime = _runtime_directory(home)

    # A different child's pid: the healthy owner's descriptor is not this failure's
    # to remove.
    _clean_child_descriptor(runtime, service_pid + 100_000)
    assert _descriptor_document(home) == published

    # Its own child's pid: this is the leftover a failed start owns.
    _clean_child_descriptor(runtime, service_pid)
    assert _descriptor_document(home) is None


# --- R004-11: the invoking adapter receives a deterministic result ---


def test_the_result_document_is_versioned_and_shaped(home: Path) -> None:
    """Every field an adapter branches on, on the success path, and stdout is clean."""
    completed = subprocess.run(
        [
            _locate(),
            "--managed-start",
            "--workspace",
            str(home / "workspace"),
            "--installation-state",
            str(home / "installation-state"),
            "--endpoint",
            _endpoint(home),
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    # Protocol data separated from human logs: stdout is the document and nothing
    # else, so an adapter needs no parser that skips prose.
    result = json.loads(completed.stdout)
    assert completed.stdout.strip() == json.dumps(result, indent=2, sort_keys=True)

    assert result["managed_start_version"] == MANAGED_START_VERSION
    assert result["status"] in {status.value for status in ManagedStartStatus}
    assert result["failure"] is None
    service = result["service"]
    assert set(service) == {
        "endpoint_uri",
        "workspace_id",
        "service_instance_id",
        "fencing_generation",
        "pid",
        "state",
        "ready",
        "unmet",
    }
    assert service["workspace_id"] == WORKSPACE_ID
    assert service["endpoint_uri"] == _endpoint(home)
    assert service["state"] == "ready"


def test_a_missing_workspace_is_a_deterministic_refusal_that_creates_nothing(
    home: Path,
) -> None:
    """R004-08 and §10: managed start never creates a workspace. `omnivia init` does."""
    empty = home / "not-a-workspace"
    empty.mkdir()

    result = managed_start(
        workspace_root=empty,
        installation_root=home / "installation-state",
        endpoint_uri=_endpoint(home),
        timeout_seconds=5.0,
    )

    assert result.status is ManagedStartStatus.FAILED
    assert result.failure is ManagedStartFailure.MISSING_WORKSPACE
    assert list(empty.iterdir()) == [], "an unbootstrapped workspace was written to"
    assert _service_pids(home) == []


def test_a_spawn_that_cannot_serve_is_a_deterministic_spawn_failure(home: Path) -> None:
    """The child refuses its endpoint before startup, and says so through the result."""
    result = managed_start(
        workspace_root=home / "workspace",
        installation_root=home / "installation-state",
        # A scheme this platform cannot serve. `service/main.py` refuses it before
        # startup rather than after, which is the exit this reads.
        endpoint_uri="http://127.0.0.1:1",
        timeout_seconds=30.0,
    )

    assert result.status is ManagedStartStatus.FAILED
    assert result.failure is ManagedStartFailure.SPAWN_FAILURE
    assert "refusing to serve" in result.child_output
    assert _service_pids(home) == []
    assert _descriptor_document(home) is None


def test_a_held_bootstrap_mutex_times_out_rather_than_failing_or_racing(
    home: Path,
) -> None:
    """`REFUSED_MUTEX_UNAVAILABLE` is not an error, so it is waited on, then timed out.

    The mutex is held for the whole budget by something that never finishes. Managed
    start must neither spawn past it nor report a mutex refusal as its own failure
    class; it waits, and the deterministic answer when the budget runs out is
    `timeout`.
    """
    runtime = _runtime_directory(home)
    runtime.mkdir(parents=True, exist_ok=True)
    mutex = create_lock(runtime / "bootstrap.lock", LockRole.BOOTSTRAP_MUTEX)
    assert mutex.acquire()
    try:
        started = time.monotonic()
        result = managed_start(
            workspace_root=home / "workspace",
            installation_root=home / "installation-state",
            endpoint_uri=_endpoint(home),
            timeout_seconds=2.0,
        )
        waited = time.monotonic() - started
    finally:
        mutex.release()

    assert result.status is ManagedStartStatus.FAILED
    assert result.failure is ManagedStartFailure.TIMEOUT
    assert waited >= 2.0, "the mutex holder was not waited for at all"
    assert _service_pids(home) == [], "a service was spawned past the held mutex"


# --- R004-11: the service, not the adapter, owns the writable workspace lease ---


def test_the_service_not_the_launcher_owns_the_writable_workspace_lease(
    home: Path,
) -> None:
    """The launcher exits; the lease and the storage lock stay with the service.

    The lifetime storage lock is what, with the sole exclusive SQLite connection,
    constitutes storage ownership -- and its own advisory payload records the pid
    and service-instance id of whoever holds it. That payload naming the *service*
    rather than the launcher is the whole property, and the launcher having already
    exited by the time it is read is the rest of it.

    The bootstrap mutex the launcher did hold grants no write authority, and
    `LockRole` says so in the type rather than in a comment.
    """
    completed = subprocess.run(
        [
            _locate(),
            "--managed-start",
            "--workspace",
            str(home / "workspace"),
            "--installation-state",
            str(home / "installation-state"),
            "--endpoint",
            _endpoint(home),
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    # The launcher has exited -- `subprocess.run` waited for it -- and the service it
    # started has not. Two processes, and only the survivor owns anything.
    assert _launcher_pids(home) == [], "a managed-start launcher outlived its answer"
    (service_pid,) = _service_pids(home)
    assert result["service"]["pid"] == service_pid

    payload = json.loads(
        (home / "workspace" / "locks" / "storage.lock").read_text(encoding="utf-8")
    )
    assert payload["role"] == LockRole.LIFETIME_STORAGE.value
    assert payload["pid"] == service_pid
    assert payload["service_instance_id"] == result["service"]["service_instance_id"]

    # The coordination lock the launcher held is not ownership, by construction.
    assert not LockRole.BOOTSTRAP_MUTEX.grants_write_authority
    assert LockRole.LIFETIME_STORAGE.grants_write_authority


# --- R004-11: authority is per writable workspace, not one service per machine ---


@dataclass(frozen=True)
class _Selection:
    """One workspace a caller can select, and the endpoint it is served on."""

    workspace_root: Path
    workspace_id: str
    endpoint_uri: str


def _runtime_for(root: Path, workspace_id: str) -> Path:
    installation = InstallationLayout(root=root / "installation-state")
    return installation.runtime_for(workspace_id)


def _initialise(root: Path, name: str) -> _Selection:
    """One workspace, made by the shipped `--init`, under the shared installation.

    `--init` mints the workspace identity itself, so the two selections here differ
    by construction rather than by a constant this file chose -- and the fixed
    `WORKSPACE_ID` the rest of the file relies on is untouched.
    """
    workspace = root / name
    completed = subprocess.run(
        [
            _locate(),
            "--init",
            "--workspace",
            str(workspace),
            "--installation-state",
            str(root / "installation-state"),
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["status"] == "initialised", document
    return _Selection(
        workspace_root=workspace,
        workspace_id=document["workspace"]["workspace_id"],
        # Beside the root rather than inside the workspace: `sockaddr_un` is the
        # binding constraint here, not tidiness.
        endpoint_uri=f"unix://{root / name}.sock",
    )


def _managed_start_selection(root: Path, selection: _Selection) -> dict[str, Any]:
    """One managed start for one selected workspace, through the console script."""
    completed = subprocess.run(
        [
            _locate(),
            "--managed-start",
            "--workspace",
            str(selection.workspace_root),
            "--installation-state",
            str(root / "installation-state"),
            "--endpoint",
            selection.endpoint_uri,
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return dict(json.loads(completed.stdout))


@pytest.fixture
def two_selections() -> Iterator[tuple[Path, tuple[_Selection, _Selection]]]:
    """Two workspaces, two identities, one installation-state root, nothing left."""
    root = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir="/tmp"))
    try:
        yield root, (_initialise(root, "a"), _initialise(root, "b"))
    finally:
        # `_service_pids` is scoped to this root's installation state, so this
        # reaches both services and nothing else on the machine.
        for pid in _service_pids(root):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
        shutil.rmtree(root, ignore_errors=True)


def test_two_writable_workspaces_each_own_one_authoritative_service(
    two_selections: tuple[Path, tuple[_Selection, _Selection]],
) -> None:
    """One authority per writable workspace -- not one authority per machine.

    The concurrency test above proves the ceiling: many starters against *one*
    workspace converge on one service. On its own that is compatible with a
    stricter and wrong reading -- one Core Service process globally -- and nothing
    in this file refuted it. This is the floor. Two workspaces selected at once run
    two services at once, and the two are related by nothing but the installation
    they share.

    **Both selections share one installation-state root, and that is the point.**
    Giving each workspace its own home would put a whole private tree between them
    -- separate catalogue, separate `runtime/`, separate backups -- and would
    demonstrate only that two unrelated installations do not collide, which no
    reading of the topology ever denied. Sharing the installation root removes that
    insulation: `runtime/<workspace-id>/` is then the *only* thing keeping the two
    services' descriptors, identities and bootstrap mutexes apart, which is exactly
    the production arrangement one user with two projects gets. If authority were
    keyed to the installation rather than the workspace, this is where it would
    show, and the second start would attach to, replace, or refuse the first.

    Round two is not a repeat. Attaching is per workspace as well: each second
    start must find *its own* prior owner, so a re-selection cannot be answered by
    whichever service happens to be running.

    Not asserted here, and observable only in this arrangement: the two services
    publish *different* `installation_id`s despite sharing an installation, because
    `InstallationIdentity` is loaded from `runtime/<workspace-id>/` -- which is the
    per-workspace keying `storage/backup.py` says the catalogue exists to prevent.
    `--init` writes no catalogue at all, so there is nothing yet for either to
    agree with. Left as a finding rather than pinned: a test that asserted either
    value would freeze one side of an unresolved question about installation
    identity, and neither side is this file's to settle.
    """
    root, (first, second) = two_selections

    started = [
        _managed_start_selection(root, first),
        _managed_start_selection(root, second),
    ]

    assert [result["status"] for result in started] == [
        ManagedStartStatus.STARTED.value
    ] * 2, started
    assert all(result["service"]["ready"] is True for result in started)

    # Two distinct workspaces, and each service owns the one it was selected for.
    assert [result["service"]["workspace_id"] for result in started] == [
        first.workspace_id,
        second.workspace_id,
    ]
    assert first.workspace_id != second.workspace_id

    instances = [result["service"]["service_instance_id"] for result in started]
    pids = [result["service"]["pid"] for result in started]
    assert len(set(instances)) == 2, (
        f"one service answered for both workspaces: {instances}"
    )
    assert len(set(pids)) == 2, f"one process answered for both workspaces: {pids}"

    # Two real processes, coexisting. Not one, and not three.
    assert sorted(_service_pids(root)) == sorted(pids)

    for selection, result in zip((first, second), started, strict=True):
        runtime = _runtime_for(root, selection.workspace_id)
        descriptor = json.loads((runtime / "service.json").read_text(encoding="utf-8"))
        assert descriptor["workspace_id"] == selection.workspace_id
        assert (
            descriptor["service_instance_id"]
            == result["service"]["service_instance_id"]
        )
        assert descriptor["process"]["pid"] == result["service"]["pid"]

        # The writable lease each service holds is its own workspace's, and the
        # advisory payload on it names that service rather than its neighbour.
        lease = json.loads(
            (selection.workspace_root / "locks" / "storage.lock").read_text(
                encoding="utf-8"
            )
        )
        assert lease["role"] == LockRole.LIFETIME_STORAGE.value
        assert lease["pid"] == result["service"]["pid"]
        assert lease["service_instance_id"] == result["service"]["service_instance_id"]

    # Re-selecting either workspace finds its own owner, not the other's.
    attached = [
        _managed_start_selection(root, first),
        _managed_start_selection(root, second),
    ]
    assert [result["status"] for result in attached] == [
        ManagedStartStatus.ATTACHED.value
    ] * 2, attached
    assert [result["service"]["pid"] for result in attached] == pids
    assert [
        result["service"]["service_instance_id"] for result in attached
    ] == instances
    assert sorted(_service_pids(root)) == sorted(pids), "a second start crossed owners"


# --- R004-09: production integration evidence ---


def test_the_cli_call_reaches_the_service_through_the_shared_managed_start_path(
    home: Path,
) -> None:
    """A CLI probe is the entry point; the launcher beneath it starts as needed.

    Run as a subprocess, which is the only arrangement in which "the CLI imports no
    runtime" is proven rather than asserted. What this adds to the CLI's own suite
    is the middle process: a managed-start launcher really is what started the
    service, and the CLI really did not spawn one itself.
    """
    # This module's legacy-migration fixture predates the shared client's
    # owner-private descriptor provenance rule. A real ``--init`` installation
    # creates these directories privately; reproduce that current precondition
    # before crossing the installed client boundary.
    for directory in (
        home / "installation-state",
        home / "installation-state" / "runtime",
        _runtime_directory(home),
    ):
        directory.chmod(0o700)
    started = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnivia_core_cli.main",
            "--installation-state",
            str(home / "installation-state"),
            "--workspace-id",
            WORKSPACE_ID,
            "service",
            "health",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    log_path = home / "run" / "service.log"
    diagnostic = (
        log_path.read_text(encoding="utf-8", errors="replace")
        if log_path.is_file()
        else "service log absent"
    )
    assert started.returncode == 0, f"{started.stderr}\n{diagnostic}"
    first_result = json.loads(started.stdout)
    assert first_result["status"] == "pass"
    descriptor = _descriptor_document(home)
    assert descriptor is not None
    assert descriptor["workspace_id"] == WORKSPACE_ID
    (pid,) = _service_pids(home)

    # The launcher wrote the service's own output where the CLI's convention says
    # it goes, so `--managed-start-log` really was honoured rather than defaulted.
    assert (home / "run" / "service.log").is_file()

    again = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnivia_core_cli.main",
            "--installation-state",
            str(home / "installation-state"),
            "--workspace-id",
            WORKSPACE_ID,
            "service",
            "health",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["status"] == "pass"
    assert _service_pids(home) == [pid]
