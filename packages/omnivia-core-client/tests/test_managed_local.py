"""Managed-local startup is one shared client operation, not adapter code."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from omnivia_core_client import (
    Deadline,
    EndpointUnavailableError,
    InstallationServiceConfig,
    ManagedStartError,
    ServiceClient,
    TransportError,
    connect_managed_local,
    managed_local,
)

WORKSPACE_ID = "ws-managed-client-01"
SECRET = "secret-child-output-and-endpoint"


def config(root: Path) -> InstallationServiceConfig:
    return InstallationServiceConfig(
        installation_state=(root / "installation-state").resolve(),
        workspace_id=WORKSPACE_ID,
    )


def initialise(root: Path) -> None:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "workspace.json").write_text("{}", encoding="utf-8")


def result(status: str = "started", **extra: object) -> str:
    return json.dumps(
        {
            "managed_start_version": "1.0",
            "status": status,
            "service": {"endpoint_uri": SECRET},
            **extra,
        }
    )


def client() -> ServiceClient:
    return cast(ServiceClient, object())


def connects(
    monkeypatch: pytest.MonkeyPatch, answers: list[ServiceClient | None]
) -> list[tuple[InstallationServiceConfig, Deadline]]:
    seen: list[tuple[InstallationServiceConfig, Deadline]] = []
    remaining: Iterator[ServiceClient | None] = iter(answers)

    def connect(
        _cls: type[ServiceClient],
        service_config: InstallationServiceConfig,
        *,
        deadline: Deadline,
        **_kwargs: Any,
    ) -> ServiceClient | None:
        seen.append((service_config, deadline))
        return next(remaining)

    monkeypatch.setattr(ServiceClient, "connect", classmethod(connect))
    return seen


def launcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str | None = None,
    returncode: int = 0,
) -> list[tuple[list[str], float]]:
    seen: list[tuple[list[str], float]] = []
    monkeypatch.setattr(
        managed_local, "locate_service", lambda: "/fixed/omnivia-core-service"
    )

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append((argv, kwargs["timeout"]))
        rendered = result() if stdout is None else stdout
        kwargs["stdout"].write(rendered.encode("utf-8"))
        kwargs["stdout"].flush()
        return subprocess.CompletedProcess(
            argv,
            returncode,
        )

    monkeypatch.setattr(managed_local.subprocess, "run", run)
    return seen


def test_a_live_service_is_attached_without_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = client()
    seen = connects(monkeypatch, [expected])
    monkeypatch.setattr(
        managed_local,
        "_invoke",
        lambda *_args, **_kwargs: pytest.fail(
            "an attached service must not be started"
        ),
    )
    deadline = Deadline.after(30)
    connected = connect_managed_local(config(tmp_path), deadline=deadline)
    assert connected.client is expected
    assert connected.status == "attached"
    assert seen == [(config(tmp_path), deadline)]


def test_start_and_reconnect_reuse_the_exact_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialise(tmp_path)
    expected = client()
    seen_connects = connects(monkeypatch, [None, expected])
    seen_launches = launcher(monkeypatch)
    now = [100.0]
    deadline = Deadline(clock=lambda: now[0], end=125.0)

    connected = connect_managed_local(config(tmp_path), deadline=deadline)

    assert connected.client is expected
    assert connected.status == "started"
    assert len(seen_connects) == 2
    assert all(item[1] is deadline for item in seen_connects)
    assert seen_launches == [
        (
            [
                "/fixed/omnivia-core-service",
                "--managed-start",
                "--workspace",
                str(tmp_path / "workspace"),
                "--installation-state",
                str(tmp_path / "installation-state"),
                "--endpoint",
                f"unix://{tmp_path}/run/s.sock",
                "--managed-start-log",
                str(tmp_path / "run/service.log"),
            ],
            25.0,
        )
    ]


def test_unreachable_published_service_reaches_the_runtime_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed owner leaves a valid descriptor; the launcher cleans it safely."""
    initialise(tmp_path)
    expected = client()
    seen: list[tuple[InstallationServiceConfig, Deadline]] = []
    attempts = iter([EndpointUnavailableError("unreachable"), expected])

    def connect(
        _cls: type[ServiceClient],
        service_config: InstallationServiceConfig,
        *,
        deadline: Deadline,
        **_kwargs: Any,
    ) -> ServiceClient | None:
        seen.append((service_config, deadline))
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ServiceClient, "connect", classmethod(connect))
    seen_launches = launcher(monkeypatch)
    deadline = Deadline.after(30)

    connected = connect_managed_local(config(tmp_path), deadline=deadline)

    assert connected.client is expected
    assert connected.status == "started"
    assert seen == [(config(tmp_path), deadline), (config(tmp_path), deadline)]
    assert len(seen_launches) == 1


def test_untrusted_descriptor_transport_refusal_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialise(tmp_path)

    def refuse(*_args: Any, **_kwargs: Any) -> ServiceClient | None:
        raise TransportError("descriptor provenance check failed")

    monkeypatch.setattr(ServiceClient, "connect", classmethod(refuse))
    monkeypatch.setattr(
        managed_local,
        "_invoke",
        lambda *_args, **_kwargs: pytest.fail("an untrusted descriptor reached launch"),
    )

    with pytest.raises(TransportError, match="provenance"):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))


def test_launcher_attached_status_survives_a_startup_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialise(tmp_path)
    connects(monkeypatch, [None, client()])
    launcher(monkeypatch, stdout=result("attached"))
    assert (
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30)).status
        == "attached"
    )


def test_an_unrecognised_installation_layout_never_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = InstallationServiceConfig(
        installation_state=(tmp_path / "some-state").resolve(),
        workspace_id=WORKSPACE_ID,
    )
    connects(monkeypatch, [None])
    monkeypatch.setattr(
        managed_local,
        "_invoke",
        lambda *_args, **_kwargs: pytest.fail("invalid layout reached the launcher"),
    )
    with pytest.raises(ManagedStartError, match="managed service") as refusal:
        connect_managed_local(wrong, deadline=Deadline.after(30))
    assert str(tmp_path) not in str(refusal.value)


def test_a_missing_workspace_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    connects(monkeypatch, [None])
    before = sorted(home.rglob("*"))
    with pytest.raises(ManagedStartError):
        connect_managed_local(config(home), deadline=Deadline.after(30))
    assert sorted(home.rglob("*")) == before == []


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json " + SECRET,
        json.dumps(["not", "an", "object", SECRET]),
        result("unknown"),
        json.dumps({"managed_start_version": "2.0", "status": "started"}),
        "x" * (managed_local.MANAGED_START_RESULT_MAXIMUM_BYTES + 1),
    ],
)
def test_malformed_or_oversized_results_are_fixed_redacted_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
) -> None:
    initialise(tmp_path)
    connects(monkeypatch, [None])
    launcher(monkeypatch, stdout=stdout)
    with pytest.raises(ManagedStartError) as refusal:
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))
    assert str(refusal.value)
    assert SECRET not in str(refusal.value)
    assert str(tmp_path) not in str(refusal.value)
    assert refusal.value.__cause__ is None
    assert refusal.value.__context__ is None


def test_child_failure_and_output_are_never_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialise(tmp_path)
    connects(monkeypatch, [None])
    launcher(monkeypatch, stdout=result("failed", child_output=SECRET), returncode=1)
    with pytest.raises(ManagedStartError) as refusal:
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))
    assert str(refusal.value)
    assert SECRET not in str(refusal.value)


def test_nonzero_exit_cannot_claim_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialise(tmp_path)
    connects(monkeypatch, [None])
    launcher(monkeypatch, stdout=result("started"), returncode=7)
    with pytest.raises(ManagedStartError) as refusal:
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))
    assert str(refusal.value) == "the managed service could not be started"
    assert refusal.value.__context__ is None


def test_a_start_that_does_not_publish_a_live_service_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialise(tmp_path)
    seen = connects(monkeypatch, [None, None])
    launcher(monkeypatch)
    deadline = Deadline.after(30)
    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=deadline)
    assert len(seen) == 2
    assert seen[0][1] is seen[1][1] is deadline
