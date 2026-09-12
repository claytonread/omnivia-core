"""Windows ACL establishment for a freshly created installation-state root.

`InstallationLayout.create` is the one place a fresh installation-state root
comes into being in the whole of `omnivia-core-service --init`
(`service/workspace_init.py`), and on Windows a brand new directory inherits
whatever DACL its parent's inheritance supplies rather than the mode `mkdir`
was given. `omnivia_core_client.installed_credentials`'s pathname walk proves
this exact root with the parent policy -- owned by this user, writable by
nobody else -- before it creates anything beneath it, so a root this call left
unrestricted refused the very first `mcp configure` a hosted Windows runner's
temp-nested installation-state root ever saw.

`_windows_restrict_root` is exercised below with `subprocess.run` doubled, the
same way `packages/omnivia-core-client/tests/test_owner_private.py` exercises
the identical, independently hosted mechanism -- see that module's docstring
for why `icacls` rather than `ctypes`. The one real-Windows case at the bottom
is unmocked and drives the installed stores through `InstallationLayout.create`
itself -- the same production method `workspace_init._bootstrap` calls to
establish this root, though calling it directly here is not a run of
`_bootstrap` or the `--init` CLI, which the hosted Standard journey proves
end-to-end -- rather than a hand-restricted root.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnivia_core_runtime.storage import backup
from omnivia_core_runtime.storage.backup import BackupError, InstallationLayout

WORKSPACE_ID = "ws-test"


# --- the Windows icacls mechanism, doubled -----------------------------------


def _windows_commands(
    monkeypatch: pytest.MonkeyPatch, *results: tuple[int, str]
) -> list[list[str]]:
    """Run `_windows_restrict_root` with `subprocess.run` doubled, in order.

    `results` is one `(returncode, stdout)` pair per expected call.
    """
    commands: list[list[str]] = []
    pending = list(results)

    def fake_run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(arguments))
        code, stdout = pending.pop(0)
        return subprocess.CompletedProcess(list(arguments), code, stdout, "")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    monkeypatch.setenv("SystemRoot", "D:\\Windows")
    return commands


def _expected_tool(program: str) -> str:
    return str(Path("D:\\Windows", "System32", program))


def _whoami_row() -> str:
    return '"host\\user","S-1-5-21-1111111111-2222222222-3333333333-1001"\n'


_EXPECTED_SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"


def test_windows_restrict_root_issues_the_established_icacls_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = _windows_commands(
        monkeypatch, (0, _whoami_row()), (0, ""), (0, ""), (0, "")
    )
    target = Path("/some/installation-state")

    assert backup._windows_restrict_root(target) is True

    icacls = _expected_tool("icacls.exe")
    assert commands == [
        [_expected_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        [icacls, str(target), "/setowner", f"*{_EXPECTED_SID}", "/q"],
        [icacls, str(target), "/reset", "/q"],
        [
            icacls,
            str(target),
            "/inheritance:r",
            "/grant:r",
            f"*{_EXPECTED_SID}:(OI)(CI)F",
            "/q",
        ],
    ]


def test_windows_restrict_root_fails_closed_when_the_sid_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = _windows_commands(monkeypatch, (1, _whoami_row()))
    assert backup._windows_restrict_root(Path("/x")) is False
    assert len(commands) == 1


def test_windows_restrict_root_fails_closed_when_the_sid_is_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = _windows_commands(monkeypatch, (0, "no sid on this line"))
    assert backup._windows_restrict_root(Path("/x")) is False
    assert len(commands) == 1


@pytest.mark.parametrize("failing_step", [0, 1, 2])
def test_windows_restrict_root_short_circuits_on_the_first_failing_step(
    failing_step: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = [(0, ""), (0, ""), (0, "")]
    steps[failing_step] = (5, "")
    commands = _windows_commands(monkeypatch, (0, _whoami_row()), *steps)

    assert backup._windows_restrict_root(Path("/x")) is False
    # The whoami lookup, plus every icacls step up to and including the one
    # that failed -- nothing queued after it was ever run.
    assert len(commands) == failing_step + 2


def test_windows_restrict_root_fails_closed_when_a_command_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def leaking_run(
        arguments: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise OSError(f"{arguments!r} failed for /secret/installation-state")

    monkeypatch.setattr(backup.subprocess, "run", leaking_run)
    assert backup._windows_restrict_root(Path("/secret/installation-state")) is False


# --- the platform gate --------------------------------------------------------


def test_restrict_root_to_owner_is_a_no_op_success_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_path: Path) -> bool:
        raise AssertionError("must not run the Windows mechanism off Windows")

    monkeypatch.setattr(backup, "_IS_WINDOWS", False)
    monkeypatch.setattr(backup, "_windows_restrict_root", fail)
    assert backup._restrict_root_to_owner(Path("/x")) is True


def test_restrict_root_to_owner_delegates_to_the_windows_mechanism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Path] = []
    monkeypatch.setattr(backup, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        backup, "_windows_restrict_root", lambda p: (seen.append(p), False)[1]
    )
    assert backup._restrict_root_to_owner(Path("/x")) is False
    assert seen == [Path("/x")]


# --- InstallationLayout.create establishes the root, once and only once -----


def test_ensure_root_creates_a_freshly_made_root_at_an_explicit_owner_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX has no later DACL step the way Windows does: the mode has to be
    right at the `mkdir` syscall itself, passed explicitly rather than left to
    `mkdir`'s own 0o777 default, because a permissive umask filters a default
    the same way it would filter any other mode -- there is nothing here that
    narrows it afterwards.
    """
    root = tmp_path / "installation-state"
    monkeypatch.setattr(backup, "_restrict_root_to_owner", lambda _path: True)
    real_mkdir = Path.mkdir
    calls: list[dict[str, object]] = []

    def spy(self: Path, *args: object, **kwargs: object) -> None:
        calls.append(kwargs)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", spy)

    InstallationLayout(root=root)._ensure_root()

    assert calls == [{"parents": True, "mode": 0o700}]
    assert root.is_dir()


def test_create_restricts_a_freshly_made_root_and_only_then_populates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order that matters: restricted before a single child exists."""
    root = tmp_path / "installation-state"
    calls: list[Path] = []

    def fake_restrict(path: Path) -> bool:
        calls.append(path)
        assert list(path.iterdir()) == []
        return True

    monkeypatch.setattr(backup, "_restrict_root_to_owner", fake_restrict)

    InstallationLayout(root=root).create(WORKSPACE_ID)

    assert calls == [root]
    for name in ("backups", "attempts", "runtime"):
        assert (root / name / WORKSPACE_ID).is_dir()


def test_create_never_touches_a_pre_existing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root this call finds already there is not this call's to re-permission."""
    root = tmp_path / "installation-state"
    root.mkdir()

    def fail(_path: Path) -> bool:
        raise AssertionError("a pre-existing root must not be restricted")

    monkeypatch.setattr(backup, "_restrict_root_to_owner", fail)

    InstallationLayout(root=root).create(WORKSPACE_ID)

    for name in ("backups", "attempts", "runtime"):
        assert (root / name / WORKSPACE_ID).is_dir()


def test_a_repeat_create_does_not_restrict_the_root_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "installation-state"
    InstallationLayout(root=root).create(WORKSPACE_ID)

    calls: list[Path] = []
    monkeypatch.setattr(
        backup, "_restrict_root_to_owner", lambda p: (calls.append(p), True)[1]
    )
    InstallationLayout(root=root).create(WORKSPACE_ID)

    assert calls == []


def test_create_raises_and_rolls_back_a_root_it_cannot_restrict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed, and leave nothing behind for a retry to misread.

    A bare, unrestricted root left in place is not merely inert: the next
    `_ensure_root` finds it with `mkdir`, takes the `FileExistsError` branch
    that means "somebody else's pre-existing directory", and skips
    restriction entirely -- so a retry could populate an unrestricted root
    and report success while the installed stores still refuse it. Rolling
    the bare root back keeps the path absent, which is what makes a retry
    re-create and re-restrict it instead.

    The message is checked for what it must not carry as much as for what it
    must: no path, so it is safe wherever `BackupError` surfaces, including
    through `workspace_init`'s public refusal reason.
    """
    root = tmp_path / "installation-state"
    monkeypatch.setattr(backup, "_restrict_root_to_owner", lambda _p: False)

    with pytest.raises(BackupError) as excinfo:
        InstallationLayout(root=root).create(WORKSPACE_ID)

    assert str(excinfo.value) == backup._ROOT_RESTRICTION_FAILURE
    assert str(root) not in str(excinfo.value)
    assert not root.exists()


def test_create_still_raises_when_the_bare_root_cannot_be_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback is best-effort: a second failure must not hide the first.

    Simulated the same way a slower concurrent writer could produce it: a
    child appears under the root before `_ensure_root` attempts its plain,
    non-recursive `rmdir` -- the one condition that makes that call fail --
    so the root survives, honestly, exactly where the ordinary failure would
    have left it if this repair did not exist. The error raised is the same
    fixed, path-free one either way.
    """
    root = tmp_path / "installation-state"

    def unrestrictable(path: Path) -> bool:
        (path / "unexpected").mkdir()
        return False

    monkeypatch.setattr(backup, "_restrict_root_to_owner", unrestrictable)

    with pytest.raises(BackupError) as excinfo:
        InstallationLayout(root=root).create(WORKSPACE_ID)

    assert str(excinfo.value) == backup._ROOT_RESTRICTION_FAILURE
    assert root.is_dir()
    assert [entry.name for entry in root.iterdir()] == ["unexpected"]


def test_a_pre_existing_installation_state_root_that_is_a_file_still_raises(
    tmp_path: Path,
) -> None:
    """The regular-file case `workspace_init.py`'s `_bootstrap` docstring names,
    pinned at this layer rather than only through the whole `--init` sequence."""
    root = tmp_path / "installation-state"
    root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(FileExistsError):
        InstallationLayout(root=root).create(WORKSPACE_ID)


# --- real Windows: the actual root/layout creation path, unmocked -----------


@pytest.mark.skipif(
    os.name != "nt", reason="exercises the real Windows ACL writer and reader"
)
def test_the_real_init_created_root_satisfies_the_installed_stores_on_real_windows(
    tmp_path: Path,
) -> None:
    """`InstallationLayout.create`, not a hand-restricted root, is what has to hold.

    `packages/omnivia-core-client/tests/test_installed_credentials.py`'s
    `test_the_installed_stores_agree_with_the_native_reader_on_real_windows`
    proves `restrict_to_owner`'s writer and `owner_private`'s reader agree once
    a root is already owner-private -- a dedicated child directory it restricts
    by hand, deliberately, since that package may depend on neither this one
    nor `scripts/`. What that leaves unproved is whether the root the Standard
    journey's own `--init` produces is one either store can ever use, and on a
    hosted Windows runner it was not: `InstallationLayout.create` left the
    installation-state root exactly as `mkdir` made it, inheriting whatever
    DACL its temp-directory parent's inheritance supplied. This test calls
    `InstallationLayout.create` itself -- the same production method
    `_bootstrap` calls to bring this exact root into being, not `_bootstrap`
    or the `--init` CLI path -- against a fresh child of `tmp_path` that was
    never touched by hand; nothing here restricts the root before handing it
    to the stores. The hosted Standard journey is what proves `_bootstrap`
    and `--init` do this end-to-end.
    """
    from omnivia_core_client import (
        Credential,
        CredentialReference,
        InstalledConfigStore,
        InstalledCredentialStore,
        owner_private,
    )

    root = tmp_path / "installation-state"
    InstallationLayout(root=root).create(WORKSPACE_ID)

    # The invariant every installed store's parent-policy proof assumes of
    # this exact root, demonstrated directly rather than only inferred from
    # the stores succeeding below.
    assert owner_private.owner_private_directory(root) is True

    reference = CredentialReference("omcp-0123456789abcdef")
    secret = "omcp_live_9f3a2b1c4d5e6f708192a3b4c5d6e7f8"
    credentials = InstalledCredentialStore(root)
    credentials.store(reference, Credential(secret))
    assert credentials.resolve(reference).reveal() == secret
    assert credentials.health(reference) == "present"

    host = "claude-code"
    document = b'{"format": "omnivia.mcp-config.v1"}\n'
    config = InstalledConfigStore(root)
    assert config.write(host, document) is True
    assert config.read(host) == document
    assert config.health(host) == "present"
