"""Managed-local startup is one shared client operation, not adapter code."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
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


def config(root: Path, workspace_id: str = WORKSPACE_ID) -> InstallationServiceConfig:
    return InstallationServiceConfig(
        installation_state=(root / "installation-state").resolve(),
        workspace_id=workspace_id,
    )


def initialise(root: Path, workspace_id: str = WORKSPACE_ID) -> None:
    """Lay down the fixed legacy layout: the one bootstrap workspace.

    The manifest carries ``workspace_id`` because that fixed path is the same
    for every ``workspace_id`` a caller could name -- only the manifest's own
    claim tells the two apart, and that claim is what authorises a start.
    """
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "workspace.json").write_text(
        json.dumps({"workspace_id": workspace_id}), encoding="utf-8"
    )


def initialise_registered(root: Path, workspace_id: str = WORKSPACE_ID) -> Path:
    """Lay down the deterministic registered layout `workspace.create` mints."""
    workspace = root / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "workspace.json").write_text("{}", encoding="utf-8")
    return workspace


def registered_socket_directory(root: Path, workspace_id: str = WORKSPACE_ID) -> Path:
    run_directory = root / "run" / "workspaces" / workspace_id
    uid = str(os.getuid()) if hasattr(os, "getuid") else "posix"
    key = os.path.normcase(os.path.abspath(str(run_directory)))
    digest = hashlib.sha256(f"{uid}\0{key}".encode()).hexdigest()[:24]
    return managed_local._POSIX_TEMP_ROOT / f"omnivia-core-{uid}-{digest}"


def registered_endpoint(root: Path, workspace_id: str = WORKSPACE_ID) -> str:
    return f"unix://{registered_socket_directory(root, workspace_id)}/s.sock"


@pytest.fixture(autouse=True)
def _isolate_registered_socket_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Give each test a unique short socket namespace and remove only that one.

    A prior cleanup glob removed every newly seen ``/tmp/omnivia-core-*`` path,
    including one a concurrent test worker or live service could have created.
    This root is short enough for ``sockaddr_un`` and owned by this test alone.
    """
    root = Path("/tmp") / f"oc-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(managed_local, "_POSIX_TEMP_ROOT", root)
    yield
    shutil.rmtree(root, ignore_errors=True)


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


def test_a_registered_workspace_launches_at_its_catalogued_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace `workspace.create` minted restarts at its own layout.

    This is the crash-recovery path a killed registered workspace needs:
    without it, `--workspace` named the fixed legacy directory regardless of
    ``workspace_id``, and a registered workspace's service could never be
    relaunched once killed.
    """
    initialise_registered(tmp_path)
    connects(monkeypatch, [None, client()])
    seen_launches = launcher(monkeypatch)

    connected = connect_managed_local(config(tmp_path), deadline=Deadline.after(30))

    assert connected.status == "started"
    [(argv, _timeout)] = seen_launches
    assert argv == [
        "/fixed/omnivia-core-service",
        "--managed-start",
        "--workspace",
        str(tmp_path / "workspaces" / WORKSPACE_ID),
        "--installation-state",
        str(tmp_path / "installation-state"),
        "--endpoint",
        registered_endpoint(tmp_path),
        "--managed-start-log",
        str(tmp_path / "run" / "workspaces" / WORKSPACE_ID / "service.log"),
    ]


def test_legacy_layout_is_still_reached_when_nothing_is_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-registration installation's one bootstrap workspace still starts.

    No ``workspaces/<workspace_id>`` directory exists at all here -- only the
    fixed legacy layout does -- and the launch must still reach it at the
    unkeyed paths it always used.
    """
    initialise(tmp_path)
    connects(monkeypatch, [None, client()])
    seen_launches = launcher(monkeypatch)

    connected = connect_managed_local(config(tmp_path), deadline=Deadline.after(30))

    assert connected.status == "started"
    [(argv, _timeout)] = seen_launches
    assert argv == [
        "/fixed/omnivia-core-service",
        "--managed-start",
        "--workspace",
        str(tmp_path / "workspace"),
        "--installation-state",
        str(tmp_path / "installation-state"),
        "--endpoint",
        f"unix://{tmp_path}/run/s.sock",
        "--managed-start-log",
        str(tmp_path / "run" / "service.log"),
    ]


def test_a_registered_manifest_wins_over_a_stray_legacy_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registered layout is tried first: both cannot both be authoritative."""
    initialise(tmp_path)
    initialise_registered(tmp_path)
    connects(monkeypatch, [None, client()])
    seen_launches = launcher(monkeypatch)

    connect_managed_local(config(tmp_path), deadline=Deadline.after(30))

    [(argv, _timeout)] = seen_launches
    assert argv[argv.index("--workspace") + 1] == str(
        tmp_path / "workspaces" / WORKSPACE_ID
    )


def test_registered_workspaces_never_share_a_socket_or_a_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrently managed workspaces get two independent run directories."""
    first_id, second_id = "ws-one", "ws-two"
    initialise_registered(tmp_path, first_id)
    initialise_registered(tmp_path, second_id)
    connects(monkeypatch, [None, client(), None, client()])
    seen_launches = launcher(monkeypatch)

    first_connected = connect_managed_local(
        config(tmp_path, first_id), deadline=Deadline.after(30)
    )
    second_connected = connect_managed_local(
        config(tmp_path, second_id), deadline=Deadline.after(30)
    )
    assert first_connected.status == second_connected.status == "started"

    [(first_argv, _), (second_argv, _)] = seen_launches

    def endpoint_and_log(argv: list[str]) -> tuple[str, str]:
        return (
            argv[argv.index("--endpoint") + 1],
            argv[argv.index("--managed-start-log") + 1],
        )

    first_endpoint, first_log = endpoint_and_log(first_argv)
    second_endpoint, second_log = endpoint_and_log(second_argv)
    assert first_endpoint != second_endpoint
    assert first_log != second_log
    assert first_endpoint == registered_endpoint(tmp_path, first_id)
    assert second_endpoint == registered_endpoint(tmp_path, second_id)
    assert first_id in first_log
    assert second_id in second_log


def test_an_uninitialised_workspace_id_creates_nothing_at_either_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither layout has a manifest for this ``workspace_id``: fail closed."""
    home = tmp_path / "home"
    home.mkdir()
    connects(monkeypatch, [None])
    before = sorted(home.rglob("*"))
    with pytest.raises(ManagedStartError):
        connect_managed_local(config(home), deadline=Deadline.after(30))
    assert sorted(home.rglob("*")) == before == []


def test_a_registered_directory_with_no_manifest_falls_back_to_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``workspaces/<id>`` directory with no manifest is not a workspace.

    Only a manifest -- never a directory's mere existence -- authorises a
    layout, so an empty registered directory must not be preferred over a
    legacy layout that does hold one.
    """
    (tmp_path / "workspaces" / WORKSPACE_ID).mkdir(parents=True)
    initialise(tmp_path)
    connects(monkeypatch, [None, client()])
    seen_launches = launcher(monkeypatch)

    connect_managed_local(config(tmp_path), deadline=Deadline.after(30))

    [(argv, _timeout)] = seen_launches
    assert argv[argv.index("--workspace") + 1] == str(tmp_path / "workspace")


def test_a_mismatched_workspace_id_is_refused_even_with_other_workspaces_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered sibling workspace does not authorise a different one."""
    initialise_registered(tmp_path, "ws-other")
    connects(monkeypatch, [None])
    monkeypatch.setattr(
        managed_local,
        "_invoke",
        lambda *_args, **_kwargs: pytest.fail(
            "an unregistered workspace_id reached the launcher"
        ),
    )
    with pytest.raises(ManagedStartError):
        connect_managed_local(
            config(tmp_path, WORKSPACE_ID), deadline=Deadline.after(30)
        )


def test_a_symlinked_workspaces_root_cannot_redirect_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    initialise_registered(outside)
    initialise(tmp_path)
    (tmp_path / "workspaces").symlink_to(
        outside / "workspaces", target_is_directory=True
    )
    connects(monkeypatch, [None])
    monkeypatch.setattr(managed_local, "locate_service", refuse_locate_service)
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))


def test_a_symlinked_registered_workspace_cannot_redirect_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "workspaces").mkdir()
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    (outside / "workspace.json").write_text("{}", encoding="utf-8")
    (tmp_path / "workspaces" / WORKSPACE_ID).symlink_to(
        outside, target_is_directory=True
    )
    connects(monkeypatch, [None])
    monkeypatch.setattr(managed_local, "locate_service", refuse_locate_service)
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))


def test_a_symlinked_registered_manifest_cannot_authorize_a_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspaces" / WORKSPACE_ID
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside-manifest.json"
    outside.write_text("{}", encoding="utf-8")
    (workspace / "workspace.json").symlink_to(outside)
    connects(monkeypatch, [None])
    monkeypatch.setattr(managed_local, "locate_service", refuse_locate_service)
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))


@pytest.mark.parametrize("linked_component", ["run", "workspaces", WORKSPACE_ID])
def test_a_symlinked_run_component_cannot_redirect_the_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_component: str,
) -> None:
    initialise_registered(tmp_path)
    outside = tmp_path / f"outside-{linked_component}"
    outside.mkdir()
    run = tmp_path / "run"
    if linked_component == "run":
        run.symlink_to(outside, target_is_directory=True)
    else:
        run.mkdir()
        run_workspaces = run / "workspaces"
        if linked_component == "workspaces":
            run_workspaces.symlink_to(outside, target_is_directory=True)
        else:
            run_workspaces.mkdir()
            (run_workspaces / WORKSPACE_ID).symlink_to(
                outside, target_is_directory=True
            )
    connects(monkeypatch, [None])
    monkeypatch.setattr(
        managed_local, "locate_service", lambda: "/fixed/omnivia-core-service"
    )
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))


def test_a_run_component_that_cannot_be_restricted_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialise_registered(tmp_path)
    connects(monkeypatch, [None])
    monkeypatch.setattr(
        managed_local, "locate_service", lambda: "/fixed/omnivia-core-service"
    )
    monkeypatch.setattr(
        managed_local, "restrict_to_owner", lambda _path, *, directory: False
    )
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))
    assert not (tmp_path / "run").exists()


def test_a_registered_workspace_appearing_during_legacy_fallback_refuses_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialise(tmp_path)
    connects(monkeypatch, [None])
    monkeypatch.setattr(
        managed_local, "locate_service", lambda: "/fixed/omnivia-core-service"
    )
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)
    resolve = managed_local._resolve_installation
    calls = 0

    def racing_resolve(
        service_config: InstallationServiceConfig,
    ) -> managed_local._AuthorizedInstallation:
        nonlocal calls
        calls += 1
        selected = resolve(service_config)
        if calls == 1:
            initialise_registered(tmp_path)
        return selected

    monkeypatch.setattr(managed_local, "_resolve_installation", racing_resolve)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))
    assert calls == 2


def test_a_manifest_replaced_after_authorization_refuses_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialise_registered(tmp_path)
    manifest = workspace / "workspace.json"
    connects(monkeypatch, [None])
    monkeypatch.setattr(
        managed_local, "locate_service", lambda: "/fixed/omnivia-core-service"
    )
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)
    resolve = managed_local._resolve_installation
    calls = 0

    def racing_resolve(
        service_config: InstallationServiceConfig,
    ) -> managed_local._AuthorizedInstallation:
        nonlocal calls
        calls += 1
        selected = resolve(service_config)
        if calls == 1:
            replacement = workspace / "replacement.json"
            replacement.write_text("{}", encoding="utf-8")
            os.replace(replacement, manifest)
        return selected

    monkeypatch.setattr(managed_local, "_resolve_installation", racing_resolve)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))
    assert calls == 2


@pytest.mark.parametrize(
    "workspace_id",
    [
        "..",
        "../escaped",
        "a/b",
        "/etc/passwd",
        "..\\escaped",
    ],
)
def test_a_traversal_shaped_workspace_id_is_refused_before_construction(
    workspace_id: str,
) -> None:
    """`InstallationServiceConfig` itself refuses these: this module never sees one.

    ``workspace_id`` is validated where the configuration is written --
    :meth:`InstallationServiceConfig.__post_init__` calls
    :func:`~omnivia_core_client.discovery.descriptor_path`, which requires the
    public ``WorkspaceId`` pattern -- so a value shaped like a traversal never
    reaches :func:`connect_managed_local` or :func:`_resolve_installation` for
    it to resist.
    """
    with pytest.raises(ValueError):
        InstallationServiceConfig(
            installation_state=Path("/does/not/matter"), workspace_id=workspace_id
        )


@pytest.mark.parametrize("workspace_id", ["a..", "a...b", "a:b", "a.b.c"])
def test_an_admitted_workspace_id_still_resolves_inside_the_workspaces_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_id: str
) -> None:
    """A `WorkspaceId` that is a single path segment cannot escape ``workspaces``.

    Every character the pattern admits (``.``, ``:``, ``_``, ``-``) is still
    one component with no path separator, so joining it under
    ``<home>/workspaces`` can only ever name a direct child of that directory
    -- never a sibling or an ancestor -- regardless of how many dots it holds.
    """
    initialise_registered(tmp_path, workspace_id)
    connects(monkeypatch, [None, client()])
    seen_launches = launcher(monkeypatch)

    connect_managed_local(
        config(tmp_path, workspace_id), deadline=Deadline.after(30)
    )

    [(argv, _timeout)] = seen_launches
    launched_workspace = Path(argv[argv.index("--workspace") + 1])
    assert launched_workspace.parent == tmp_path / "workspaces"
    assert launched_workspace == tmp_path / "workspaces" / workspace_id


@pytest.mark.parametrize(
    "workspace_id",
    ["Workspace", "a:b", "a.", "con", "con.txt", "LPT1", "nul.json"],
)
def test_a_windows_aliasing_workspace_id_is_refused_as_a_path_component(
    workspace_id: str,
) -> None:
    assert not managed_local._windows_unambiguous_workspace_component(
        workspace_id, windows=True
    )


@pytest.mark.parametrize("workspace_id", ["ws-1234", "a...b", "a.b.c", "com10"])
def test_a_server_shaped_workspace_id_is_unambiguous_on_windows(
    workspace_id: str,
) -> None:
    assert managed_local._windows_unambiguous_workspace_component(
        workspace_id, windows=True
    )


def write_legacy_manifest(root: Path, content: str) -> None:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "workspace.json").write_text(content, encoding="utf-8")


def refuse_locate_service() -> str | None:
    pytest.fail("a refused start reached locate_service")


def refuse_invoke(*_args: Any, **_kwargs: Any) -> str:
    pytest.fail("a refused start reached the launcher")


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        json.dumps(["not", "an", "object", WORKSPACE_ID]),
        json.dumps({"workspace_id": None}),
        json.dumps({"workspace_id": "some-other-workspace"}),
        json.dumps(
            {
                "workspace_id": WORKSPACE_ID,
                "filler": "x" * managed_local._LEGACY_MANIFEST_MAXIMUM_BYTES,
            }
        ),
        "x" * (managed_local._LEGACY_MANIFEST_MAXIMUM_BYTES + 1),
    ],
    ids=[
        "malformed",
        "not-an-object",
        "null-workspace-id",
        "mismatched-workspace-id",
        "well-shaped-but-oversized",
        "malformed-and-oversized",
    ],
)
def test_an_unauthorised_legacy_manifest_never_reaches_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """Only a bounded, valid, identity-matching manifest authorises the legacy
    layout: `<home>/workspace` is the same path for every `workspace_id`, so
    nothing about locating it says which workspace it is."""
    write_legacy_manifest(tmp_path, content)
    connects(monkeypatch, [None])
    monkeypatch.setattr(managed_local, "locate_service", refuse_locate_service)
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))


def test_a_legacy_manifest_without_workspace_id_remains_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original managed client accepted any existing JSON-object manifest."""
    write_legacy_manifest(tmp_path, "{}")
    connects(monkeypatch, [None, client()])
    seen_launches = launcher(monkeypatch)

    connected = connect_managed_local(config(tmp_path), deadline=Deadline.after(30))

    assert connected.status == "started"
    assert len(seen_launches) == 1


def test_a_legacy_workspace_directory_with_no_manifest_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "workspace").mkdir(parents=True)
    connects(monkeypatch, [None])
    monkeypatch.setattr(managed_local, "locate_service", refuse_locate_service)
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))


def test_a_legacy_manifest_naming_a_different_workspace_id_still_launches_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correctly identified manifest still authorises its own workspace_id."""
    other_id = "ws-legacy-alt-02"
    initialise(tmp_path, other_id)
    connects(monkeypatch, [None, client()])
    seen_launches = launcher(monkeypatch)

    connected = connect_managed_local(
        config(tmp_path, other_id), deadline=Deadline.after(30)
    )

    assert connected.status == "started"
    assert len(seen_launches) == 1


def test_a_registered_endpoint_uses_an_owner_private_socket_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket sits inside its own directory, not a bare shared-/tmp name."""
    initialise_registered(tmp_path)
    connects(monkeypatch, [None, client()])
    seen_launches = launcher(monkeypatch)

    connect_managed_local(config(tmp_path), deadline=Deadline.after(30))

    [(argv, _timeout)] = seen_launches
    endpoint = argv[argv.index("--endpoint") + 1]
    directory = registered_socket_directory(tmp_path)
    assert endpoint == f"unix://{directory}/s.sock"
    assert directory.parent == managed_local._POSIX_TEMP_ROOT
    assert len(str(directory / "s.sock")) < 100


def test_the_socket_directory_is_created_owner_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialise_registered(tmp_path)
    directory = registered_socket_directory(tmp_path)
    assert not directory.exists()
    connects(monkeypatch, [None, client()])
    launcher(monkeypatch)

    connect_managed_local(config(tmp_path), deadline=Deadline.after(30))

    assert directory.is_dir()
    assert not directory.is_symlink()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def _plant_symlinked_socket_directory(directory: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere-socket-dir"
    elsewhere.mkdir(mode=0o700)
    directory.symlink_to(elsewhere, target_is_directory=True)


def _plant_world_writable_socket_directory(directory: Path, tmp_path: Path) -> None:
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)


def _plant_socket_directory_as_a_file(directory: Path, tmp_path: Path) -> None:
    directory.write_bytes(b"not a directory")


@pytest.mark.parametrize(
    "plant",
    [
        _plant_symlinked_socket_directory,
        _plant_world_writable_socket_directory,
        _plant_socket_directory_as_a_file,
    ],
    ids=["symlink", "world-writable", "not-a-directory"],
)
def test_a_hostile_preexisting_socket_directory_refuses_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: Any
) -> None:
    initialise_registered(tmp_path)
    directory = registered_socket_directory(tmp_path)
    plant(directory, tmp_path)
    connects(monkeypatch, [None])
    monkeypatch.setattr(
        managed_local, "locate_service", lambda: "/fixed/omnivia-core-service"
    )
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))


def test_a_socket_directory_not_owned_by_this_user_refuses_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The native owner verdict is load-bearing, not just the mode check."""
    initialise_registered(tmp_path)
    directory = registered_socket_directory(tmp_path)
    directory.mkdir(mode=0o700)
    connects(monkeypatch, [None])
    monkeypatch.setattr(
        managed_local, "locate_service", lambda: "/fixed/omnivia-core-service"
    )
    monkeypatch.setattr(managed_local, "owner_private_directory", lambda _path: False)
    monkeypatch.setattr(managed_local, "_invoke", refuse_invoke)

    with pytest.raises(ManagedStartError):
        connect_managed_local(config(tmp_path), deadline=Deadline.after(30))
