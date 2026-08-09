"""Resolving the installation and asking the service to make itself exist.

R004-07's startup contract from the MCP side. The launcher itself is Packet A's
and is tested there; what is tested here is the adapter's half -- the fixed path
convention, the refusals that never reach a subprocess, the argv, and the reading
of the launcher's five closed failure classes.

A fake `omnivia-core-service` stands in for the real one throughout. It is the
same interface the real launcher publishes -- one versioned JSON document on
stdout, human text on stderr -- so these exercise the contract rather than the
implementation, and they are the reason this suite needs no bootstrapped
workspace and spawns no service.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from omnivia_core_mcp import managed_start
from omnivia_core_mcp.managed_start import (
    Attachment,
    Installation,
    ManagedStartError,
    ensure_service,
    home_directory,
)

READY_SERVICE = {
    "endpoint_uri": "unix:///tmp/omnivia/run/s.sock",
    "workspace_id": "ws-test",
    "service_instance_id": "svc-1",
    "fencing_generation": 3,
    "pid": 4242,
    "state": "ready",
    "ready": True,
    "unmet": [],
}


def fake_service(directory: Path, document: object, *, stderr: str = "") -> Path:
    """An executable that answers like `omnivia-core-service --managed-start`.

    Writes the result document to stdout and everything human to stderr, which is
    the split the real launcher publishes and the one the adapter depends on.
    """
    script = directory / "omnivia-core-service"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.stdout.write({json.dumps(document)!r})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def initialised(home: Path) -> Installation:
    """An installation whose workspace has a manifest, and nothing else."""
    installation = Installation(home=home)
    installation.workspace_root.mkdir(parents=True)
    installation.manifest_path.write_text("{}", encoding="utf-8")
    return installation


# --- the fixed path convention (R004-11) -------------------------------------


def test_the_default_home_is_the_fixed_convention() -> None:
    assert home_directory() == Path.home() / ".omnivia"


def test_an_explicit_argument_overrides_the_default() -> None:
    assert home_directory(Path("/srv/omnivia")) == Path("/srv/omnivia")


def test_no_environment_variable_redirects_the_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R004-11: an environment lookup is an ambient filesystem path by another name.

    Every plausible spelling is set to a directory that must never be chosen. The
    property is that the resolved home is unchanged, not that a particular name
    was skipped.
    """
    for name in ("OMNIVIA_HOME", "OMNIVIA_ROOT", "OMNIVIA_WORKSPACE", "OMNIVIA_STATE"):
        monkeypatch.setenv(name, "/tmp/attacker-controlled")
    assert home_directory() == Path.home() / ".omnivia"


def test_the_layout_matches_the_documented_convention(tmp_path: Path) -> None:
    installation = Installation(home=tmp_path)
    assert installation.workspace_root == tmp_path / "workspace"
    assert installation.installation_state == tmp_path / "installation-state"
    assert installation.run_directory == tmp_path / "run"
    assert installation.socket_path == tmp_path / "run" / "s.sock"
    assert installation.log_path == tmp_path / "run" / "service.log"
    assert installation.manifest_path == tmp_path / "workspace" / "workspace.json"


@pytest.mark.skipif(os.name == "nt", reason="POSIX endpoint form")
def test_the_endpoint_is_the_socket_under_run(tmp_path: Path) -> None:
    assert Installation(home=tmp_path).endpoint_uri == f"unix://{tmp_path}/run/s.sock"


# --- refusals that never reach a subprocess ----------------------------------


def test_no_workspace_is_refused_with_the_init_instruction(tmp_path: Path) -> None:
    """R004-07: fail with an actionable instruction to run `omnivia init`."""
    with pytest.raises(ManagedStartError, match="omnivia init") as refusal:
        ensure_service(Installation(home=tmp_path))
    assert "creates none" in str(refusal.value)


def test_no_workspace_creates_nothing_at_all(tmp_path: Path) -> None:
    """"It must not silently create persistent state" -- and it creates none.

    The whole tree is compared before and after, so this catches a `mkdir` of the
    run directory just as surely as a written manifest.
    """
    home = tmp_path / "home"
    home.mkdir()
    before = sorted(path.relative_to(home) for path in home.rglob("*"))
    with pytest.raises(ManagedStartError):
        ensure_service(Installation(home=home))
    assert sorted(path.relative_to(home) for path in home.rglob("*")) == before == []


def test_a_missing_service_executable_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed_start, "locate_service", lambda: None)
    with pytest.raises(ManagedStartError, match="omnivia-core-runtime"):
        ensure_service(initialised(tmp_path / "home"))


# --- the invocation ----------------------------------------------------------


def test_an_attached_service_is_reported_as_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = fake_service(
        tmp_path,
        {"managed_start_version": "1.0", "status": "attached", "failure": None,
         "reason": "a compatible service is already ready", "service": READY_SERVICE},
    )
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    attachment = ensure_service(initialised(tmp_path / "home"))
    assert attachment == Attachment(
        status="attached",
        endpoint_uri=READY_SERVICE["endpoint_uri"],
        workspace_id="ws-test",
    )


def test_a_started_service_is_reported_as_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = fake_service(
        tmp_path,
        {"managed_start_version": "1.0", "status": "started", "failure": None,
         "reason": "started omnivia-core-service (pid 4242)", "service": READY_SERVICE},
    )
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    assert ensure_service(initialised(tmp_path / "home")).status == "started"


def test_the_launcher_is_invoked_with_the_documented_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One argv, and it is the service package's own published interface.

    Pinned because it is the entire coupling between this adapter and the shared
    path: five flags and nothing else. Notably absent is any flag that would let
    a model choose a workspace or a filesystem root.
    """
    recorded: dict[str, list[str]] = {}
    real_run = subprocess.run

    def record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["argv"] = argv
        return real_run(argv, **kwargs)  # type: ignore[call-overload, no-any-return]

    script = fake_service(
        tmp_path,
        {"managed_start_version": "1.0", "status": "attached", "failure": None,
         "reason": "ready", "service": READY_SERVICE},
    )
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    monkeypatch.setattr(managed_start.subprocess, "run", record)

    installation = initialised(tmp_path / "home")
    ensure_service(installation)
    assert recorded["argv"] == [
        str(script),
        "--managed-start",
        "--workspace",
        str(installation.workspace_root),
        "--installation-state",
        str(installation.installation_state),
        "--endpoint",
        installation.endpoint_uri,
        "--managed-start-log",
        str(installation.log_path),
    ]


def test_the_child_s_human_output_never_reaches_this_process_s_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R004-07: child-process output must never corrupt the stdio stream.

    The fake launcher writes a paragraph to its stderr, as the real one does. It
    must appear on neither of this process's streams: `capture_output=True` is
    what keeps that true even before the SDK transport has claimed the
    descriptors.
    """
    script = fake_service(
        tmp_path,
        {"managed_start_version": "1.0", "status": "attached", "failure": None,
         "reason": "ready", "service": READY_SERVICE},
        stderr="starting service...\nmigrations complete\n",
    )
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    ensure_service(initialised(tmp_path / "home"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "migrations complete" not in captured.err


# --- the five closed failure classes -----------------------------------------


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("missing_workspace", "omnivia init"),
        ("incompatible_service", "omnivia stop"),
        ("timeout", "in time"),
        ("spawn_failure", "could not be started"),
        ("readiness_failure", "never became ready"),
    ],
)
def test_each_failure_class_becomes_actionable_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, expected: str
) -> None:
    script = fake_service(
        tmp_path,
        {"managed_start_version": "1.0", "status": "failed", "failure": failure,
         "reason": "the launcher said so", "service": None},
    )
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    with pytest.raises(ManagedStartError) as refusal:
        ensure_service(initialised(tmp_path / "home"))
    assert failure in str(refusal.value)
    assert "the launcher said so" in str(refusal.value)
    assert expected in str(refusal.value)


def test_an_unrecognised_failure_class_is_reported_rather_than_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sixth class added upstream must surface, not vanish into a default."""
    script = fake_service(
        tmp_path,
        {"managed_start_version": "1.0", "status": "failed", "failure": "quota_exceeded",
         "reason": "no room left", "service": None},
    )
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    with pytest.raises(ManagedStartError, match="quota_exceeded"):
        ensure_service(initialised(tmp_path / "home"))


def test_a_failed_start_carries_the_child_s_own_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = fake_service(
        tmp_path,
        {"managed_start_version": "1.0", "status": "failed", "failure": "spawn_failure",
         "reason": "exited early", "service": None,
         "child_output": "OSError: AF_UNIX path too long"},
    )
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    with pytest.raises(ManagedStartError, match="AF_UNIX path too long"):
        ensure_service(initialised(tmp_path / "home"))


# --- a launcher that is not answering its own contract -----------------------


def test_unparseable_stdout_is_a_broken_launcher_not_a_failed_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "omnivia-core-service"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('not json at all')\n"
        "sys.stderr.write('the real error')\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    with pytest.raises(ManagedStartError, match="the real error"):
        ensure_service(initialised(tmp_path / "home"))


def test_a_success_without_an_endpoint_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attached-with-no-endpoint would advertise tools nothing could ever call."""
    script = fake_service(
        tmp_path,
        {"managed_start_version": "1.0", "status": "attached", "failure": None,
         "reason": "ready", "service": {"workspace_id": "ws-test"}},
    )
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    with pytest.raises(ManagedStartError, match="usable endpoint"):
        ensure_service(initialised(tmp_path / "home"))


def test_an_unknown_status_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = fake_service(
        tmp_path,
        {"managed_start_version": "1.0", "status": "maybe", "service": READY_SERVICE},
    )
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    with pytest.raises(ManagedStartError, match="unknown status"):
        ensure_service(initialised(tmp_path / "home"))


def test_a_launcher_that_never_answers_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "omnivia-core-service"
    script.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(managed_start, "locate_service", lambda: str(script))
    with pytest.raises(ManagedStartError, match="did not answer within"):
        ensure_service(initialised(tmp_path / "home"), timeout_seconds=0.5)


# --- what this module deliberately does not do -------------------------------


def test_nothing_here_stops_a_service() -> None:
    """R004-07: MCP never stops a service, including one it started.

    Enforced by there being no such call: the module's whole public surface is
    resolution and one start.
    """
    assert "stop" not in managed_start.__all__
    assert not any(
        name.startswith("stop") or name.endswith("stop")
        for name in dir(managed_start)
        if not name.startswith("__")
    )
