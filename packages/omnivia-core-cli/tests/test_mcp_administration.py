"""`omnivia mcp configure|status|revoke`: the installed administration family.

Focused, and deliberately offline. The seam this module fakes is the *transport*,
not the client: every control below goes through the real
:func:`~omnivia_core_client.mcp_configure`, :func:`~omnivia_core_client.mcp_status`
and :func:`~omnivia_core_client.mcp_revoke`, with their real answer validation,
against a fake installation service that models the durable state the runtime
holds -- one row per host, a generation that advances, a bearer minted exactly
once and never retrievable again. So a change that broke the wire contract fails
here rather than passing against a stub of the CLI's own shape.

Everything else is real: both protected stores --
:class:`~omnivia_core_client.InstalledCredentialStore` for the bearer and
:class:`~omnivia_core_client.InstalledConfigStore` for the document -- are rooted
at a temporary installation state, and the file on disk is what is read back and
asserted on.

The second seam is the MCP handshake check, which is faked because the MCP
distribution is not a dependency of this one -- that is the boundary the seam
exists for, and the check itself is unit-tested in the MCP package.

`SECRET` is the sentinel. Every command is checked for it in both streams and in
every file this module writes: a bearer reaches owner-private storage and
nowhere else.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_cli import mcp_admin
from omnivia_core_cli.main import build_parser, main
from omnivia_core_cli.mcp_admin import Seams
from omnivia_core_cli.surface import (
    APPLICATION_COMMANDS,
    LIFECYCLE_COMMANDS,
    MCP_COMMANDS,
    PROBE_COMMANDS,
)
from omnivia_core_client import (
    LOCAL_CONTROL_VERSION,
    Credential,
    CredentialReference,
    CredentialUnavailableError,
    InstalledConfigStore,
    InstalledCredentialStore,
)

#: Distinctive enough that finding it anywhere is proof it came from a bearer.
SECRET = "bearer-s3cr3t-9f2a-do-not-print"

WORKSPACE = "ws-1"


# --- the fake installation service -------------------------------------------


@dataclass
class FakeService:
    """One installation's durable MCP state, answered over the control wire.

    Models exactly what `omnivia_core_runtime.service.installed_mcp` guarantees
    and nothing more: a configure that finds the requested state live rotates
    nothing and returns no secret; one that does not mints a fresh principal,
    reference and bearer inside the same step; a revoke advances the generation
    and marks the row revoked; and the bearer is handed over once and never
    stored here in a form anything can read back.
    """

    setups: dict[str, dict[str, Any]] = field(default_factory=dict)
    minted: int = 0
    refusals: dict[str, str] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)
    #: What the durable state looked like when each control was answered, in
    #: order, so a test can assert compensation ordering rather than its effect.
    seen: list[tuple[str, bool]] = field(default_factory=list)
    #: Set by a test to observe the local half at the moment a control lands.
    watch: Path | None = None

    def exchange(
        self,
        document: dict[str, Any],
        *,
        deadline: Any,
        cancellation: Any = None,
        operation: str = "",
    ) -> dict[str, Any]:
        kind = document["kind"]
        self.seen.append((kind, self.watch is not None and self.watch.exists()))
        if kind in self.unavailable:
            raise OSError("the fake endpoint is down")
        if kind in self.refusals:
            return {
                "local_control_result": LOCAL_CONTROL_VERSION,
                "kind": kind,
                "error": {"code": self.refusals[kind], "message": "refused"},
            }
        arguments = document.get("arguments", {})
        handler = {
            "mcp.configure": self._configure,
            "mcp.status": self._status,
            "mcp.revoke": self._revoke,
        }[kind]
        return {
            "local_control_result": LOCAL_CONTROL_VERSION,
            "kind": kind,
            "result": handler(arguments),
        }

    def _configure(self, arguments: dict[str, Any]) -> dict[str, Any]:
        host = arguments["host"]
        live = self.setups.get(host)
        matches = live is not None and (
            live["status"] == "active"
            and live["workspace_id"] == arguments["workspace_id"]
            and live["profile"] == arguments["profile"]
            and live["authoring_intent"] == arguments["authoring_intent"]
        )
        if matches:
            assert live is not None
            return {"setup": dict(live), "rotated": False}
        self.minted += 1
        mint = self.minted
        generation = 1 if live is None else live["setup_generation"] + 1
        setup = {
            "setup_id": f"mcp-setup-{mint}",
            "host": host,
            "workspace_id": arguments["workspace_id"],
            "principal_id": f"mcp-{host}-{mint}",
            "profile": arguments["profile"],
            "authoring_intent": arguments["authoring_intent"],
            "credential_reference": f"omcp-{mint:032x}",
            "status": "active",
            "setup_generation": generation,
        }
        self.setups[host] = setup
        return {"setup": dict(setup), "rotated": True, "secret": f"{SECRET}-{mint}"}

    def _status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        host = arguments.get("host")
        return {
            "setups": [
                dict(setup)
                for setup in self.setups.values()
                if host is None or setup["host"] == host
            ]
        }

    def _revoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        host = arguments["host"]
        live = self.setups.get(host)
        if live is None:
            return {"setup": None}
        live["status"] = "revoked"
        live["setup_generation"] += 1
        return {"setup": dict(live)}


@dataclass
class FakeVerification:
    """The MCP handshake seam: records what it was asked, answers or refuses."""

    count: int = 11
    failure: BaseException | None = None
    paths: list[Path] = field(default_factory=list)

    def __call__(self, path: Path) -> int:
        self.paths.append(path)
        if self.failure is not None:
            raise self.failure
        return self.count


@dataclass
class Harness:
    """One temporary installation, a fake service, and the seams wired to them."""

    state: Path
    service: FakeService
    verification: FakeVerification
    reachable: bool = True

    @property
    def seams(self) -> Seams:
        def control(
            state: Path, workspace: str | None, *, start: bool, deadline: Any
        ) -> Any:
            assert state == self.state
            return self.service if self.reachable else None

        return Seams(control=control, verify=self.verification)

    def run(self, *argv: str) -> int:
        return main(
            ["--installation-state", str(self.state), "mcp", *argv],
            mcp_seams=self.seams,
        )

    def configuration(self, host: str = "claude-code") -> Path:
        return mcp_admin.configuration_path(self.state, host)

    def stored(self, host: str = "claude-code") -> dict[str, Any] | None:
        path = self.configuration(host)
        if not path.is_file():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict)
        return document

    def credential(self, host: str = "claude-code") -> str:
        document = self.stored(host)
        assert document is not None
        store = InstalledCredentialStore(self.state)
        return store.resolve(
            CredentialReference(document["credential_reference"])
        ).reveal()

    def health(self, reference: str) -> str:
        return InstalledCredentialStore(self.state).health(
            CredentialReference(reference)
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    state = tmp_path / "installation"
    (state / "runtime").mkdir(parents=True)
    return Harness(state=state, service=FakeService(), verification=FakeVerification())


def configure(
    harness: Harness, *extra: str, host: str = "claude-code", profile: str = "authoring"
) -> int:
    return harness.run(
        "configure",
        "--host",
        host,
        "--workspace",
        WORKSPACE,
        "--profile",
        profile,
        *extra,
    )


def assert_nothing_disclosed(
    harness: Harness, captured: pytest.CaptureResult[str], *references: str
) -> None:
    """Nothing reached a stream but this module's own fixed vocabulary.

    Applied to every compensation branch, because a compensation is where the
    material to disclose is densest: a bearer in hand, two credential
    references, a path a store just refused and the text of whatever refused it.
    """
    assert captured.out == ""
    for forbidden in (
        SECRET,
        str(harness.state),
        str(harness.configuration()),
        "runtime",
        ".installed",
        "endpoint",  # the fake service's own `OSError` says this
        "startup refused",  # the fake handshake's own `RuntimeError` says this
        "Traceback",
        *references,
    ):
        assert forbidden not in captured.err


# --- the parser is the surface ------------------------------------------------


BASE = ["--installation-state", "/absolute/installation"]


@pytest.mark.parametrize("command", MCP_COMMANDS, ids=lambda c: ".".join(c.path))
def test_each_mcp_command_parses_from_its_exact_two_segments(command: Any) -> None:
    argv = [*BASE, *command.path]
    if command.action == "configure":
        argv += ["--host", "codex", "--workspace", WORKSPACE, "--profile", "restricted"]
    arguments = build_parser().parse_args(argv)
    assert arguments.command is command
    assert (arguments.group, arguments.leaf) == command.path
    assert len(command.path) == 2


def test_the_three_paths_are_the_whole_family() -> None:
    """R004 section 9.2's required surface, and no fourth path beside it."""
    assert [command.path for command in MCP_COMMANDS] == [
        ("mcp", "configure"),
        ("mcp", "status"),
        ("mcp", "revoke"),
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["mcp"],
        ["mcp", "conf"],
        ["mcp", "rotate"],
        ["mcp", "grant"],
        ["mcp", "list"],
        ["mcp", "configure", "extra"],
        ["mcp", "configure"],
        ["mcp", "configure", "--host", "claude-code"],
        ["mcp", "configure", "--host", "claude-code", "--workspace", WORKSPACE],
        ["mcp", "configure", "--workspace", WORKSPACE, "--profile", "restricted"],
        ["mcp", "status", "--workspace", WORKSPACE],
        ["mcp", "revoke", "--profile", "authoring"],
    ],
    ids=lambda a: " ".join(a),
)
def test_nothing_outside_the_declared_family_parses(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*BASE, *argv]) == 2
    assert capsys.readouterr().out == ""


#: Every authority-shaped flag this family must not have. R004 section 9.2: the
#: caller chooses a host, a workspace and a profile, and the rights a profile
#: implies are the service's to derive.
AUTHORITY_FLAGS = [
    "--scope",
    "--scopes",
    "--capability",
    "--purpose",
    "--purposes",
    "--principal",
    "--principal-id",
    "--credential",
    "--credential-reference",
    "--secret",
    "--config",
    "--config-path",
    "--endpoint",
    "--grant",
    "--operation",
]


@pytest.mark.parametrize("flag", AUTHORITY_FLAGS)
@pytest.mark.parametrize("action", ["configure", "status", "revoke"])
def test_no_mcp_command_accepts_an_authority_shaped_flag(
    flag: str, action: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [*BASE, "mcp", action]
    if action == "configure":
        argv += ["--host", "codex", "--workspace", WORKSPACE, "--profile", "restricted"]
    assert main([*argv, flag, SECRET]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SECRET not in captured.err


@pytest.mark.parametrize(
    "argv",
    [
        [
            "mcp",
            "configure",
            "--host",
            SECRET,
            "--workspace",
            WORKSPACE,
            "--profile",
            "restricted",
        ],
        [
            "mcp",
            "configure",
            "--host",
            "claude-code",
            "--workspace",
            WORKSPACE,
            "--profile",
            SECRET,
        ],
        [
            "mcp",
            "configure",
            "--host",
            "claude-code",
            "--workspace",
            f"/{SECRET}",
            "--profile",
            "restricted",
        ],
        [
            "mcp",
            "configure",
            "--host",
            "claude-code",
            "--workspace",
            f"../{SECRET}",
            "--profile",
            "restricted",
        ],
        ["mcp", "status", "--host", SECRET],
        ["mcp", "revoke", "--host", SECRET],
    ],
    ids=[
        "host",
        "profile",
        "workspace-path",
        "workspace-traversal",
        "status-host",
        "revoke-host",
    ],
)
def test_a_value_outside_the_closed_vocabulary_is_refused_without_an_echo(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*BASE, *argv]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SECRET not in captured.err


# --- the old root form still works --------------------------------------------


@pytest.mark.parametrize(
    "command",
    [*APPLICATION_COMMANDS, *PROBE_COMMANDS, *LIFECYCLE_COMMANDS],
    ids=lambda c: ".".join(c.path),
)
def test_the_old_root_form_still_parses_every_pre_existing_command(
    command: Any,
) -> None:
    """`--workspace-id` stopped being argparse-required; nothing else moved."""
    arguments = build_parser().parse_args(
        [*BASE, "--workspace-id", WORKSPACE, *command.path]
    )
    assert arguments.command is command
    assert arguments.workspace_id == WORKSPACE


@pytest.mark.parametrize(
    "command",
    [*APPLICATION_COMMANDS, *PROBE_COMMANDS, *LIFECYCLE_COMMANDS],
    ids=lambda c: ".".join(c.path),
)
def test_every_pre_existing_command_still_requires_the_root_workspace_id(
    command: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Still a usage error, still exit 2, still before a socket is opened."""
    assert main([*BASE, *command.path]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", MCP_COMMANDS, ids=lambda c: ".".join(c.path))
def test_the_mcp_family_needs_no_root_workspace_id(command: Any) -> None:
    argv = [*BASE, *command.path]
    if command.action == "configure":
        argv += ["--host", "codex", "--workspace", WORKSPACE, "--profile", "restricted"]
    arguments = build_parser().parse_args(argv)
    assert arguments.workspace_id is None


# --- configure ----------------------------------------------------------------


def test_a_first_configure_publishes_both_halves_and_prints_a_snippet(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    captured = capsys.readouterr()

    setup = harness.service.setups["claude-code"]
    assert setup["profile"] == "authoring"
    assert setup["authoring_intent"] is True
    assert setup["workspace_id"] == WORKSPACE
    assert harness.stored() == {
        "allowed_purposes": [
            "content_ingestion",
            "job_observation",
            "knowledge_retrieval",
            "memory_authoring",
            "workspace_inspection",
        ],
        "allowed_workspace_ids": [WORKSPACE],
        "credential_reference": setup["credential_reference"],
        "default_workspace_id": WORKSPACE,
        "format": "omnivia.mcp-config.v1",
        "installation_state": str(harness.state),
        "mutation_enabled": True,
        "principal_id": setup["principal_id"],
        "service_mode": "managed_local",
    }
    assert harness.credential() == f"{SECRET}-1"
    assert harness.verification.paths == [harness.configuration()]
    assert json.loads(captured.out) == {
        "mcpServers": {
            "omnivia-core": {
                "command": "omnivia-core-mcp",
                "args": ["--config", str(harness.configuration())],
            }
        }
    }
    assert captured.err == ""


def test_a_restricted_configure_writes_the_restricted_ceiling_and_purposes(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness, profile="restricted") == 0
    capsys.readouterr()
    document = harness.stored()
    assert document is not None
    assert document["mutation_enabled"] is False
    assert document["allowed_purposes"] == [
        "knowledge_retrieval",
        "workspace_inspection",
    ]
    assert harness.service.setups["claude-code"]["authoring_intent"] is False


def test_the_codex_snippet_is_toml_and_names_only_the_command_and_the_path(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness, host="codex") == 0
    printed = capsys.readouterr().out
    assert printed == (
        "[mcp_servers.omnivia-core]\n"
        'command = "omnivia-core-mcp"\n'
        f'args = ["--config", "{harness.configuration("codex")}"]\n'
    )


@pytest.mark.parametrize(
    "path",
    [
        Path("/absolute/config.json"),
        Path(r"C:\Users\Example\AppData\Local\OmniVia\codex.json"),
        Path('/absolute/a "quoted" configuration.json'),
        Path("/absolute/control\ncharacter.json"),
        Path("/absolute/del-\x7f.json"),
    ],
)
def test_every_codex_path_round_trips_through_a_toml_parser(path: Path) -> None:
    document = tomllib.loads(mcp_admin.host_snippet("codex", path))
    assert document == {
        "mcp_servers": {
            "omnivia-core": {
                "command": "omnivia-core-mcp",
                "args": ["--config", str(path)],
            }
        }
    }


@pytest.mark.parametrize("host", ["claude-code", "codex"])
def test_no_snippet_can_carry_a_credential(host: str) -> None:
    """There is no parameter here a secret could travel through."""
    snippet = mcp_admin.host_snippet(host, Path("/absolute/config.json"))
    assert "credential" not in snippet
    assert "token" not in snippet
    assert snippet.count("--config") == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_the_published_configuration_is_owner_private_in_a_private_directory(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    path = harness.configuration()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not path.is_symlink()


def test_nothing_is_left_behind_beside_the_published_configuration(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """An atomic replacement removes its temporary on every path it takes."""
    assert configure(harness) == 0
    capsys.readouterr()
    directory = harness.configuration().parent
    assert sorted(entry.name for entry in directory.iterdir()) == ["claude-code.json"]


def test_a_symlink_standing_at_the_configuration_path_is_replaced_not_followed(
    harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    victim = tmp_path / "victim.json"
    victim.write_text("untouched", encoding="utf-8")
    path = harness.configuration()
    path.parent.mkdir(parents=True, mode=0o700)
    path.symlink_to(victim)

    assert configure(harness) == 0
    capsys.readouterr()
    assert victim.read_text(encoding="utf-8") == "untouched"
    assert not path.is_symlink()
    assert harness.stored() is not None


def test_a_symlinked_configuration_directory_refuses_and_compensates(
    harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    directory = harness.configuration().parent
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.symlink_to(elsewhere, target_is_directory=True)

    assert configure(harness) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SECRET not in captured.err
    assert sorted(elsewhere.iterdir()) == []
    assert harness.service.setups["claude-code"]["status"] == "revoked"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_group_readable_configuration_is_not_settled_and_is_rotated_away(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    first = harness.service.setups["claude-code"]["credential_reference"]
    harness.configuration().chmod(0o644)

    assert configure(harness) == 0
    capsys.readouterr()
    second = harness.service.setups["claude-code"]["credential_reference"]
    assert second != first
    assert harness.health(first) == "absent"
    assert stat.S_IMODE(harness.configuration().stat().st_mode) == 0o600


# --- idempotency, repair and rotation ----------------------------------------


def test_a_repeated_configure_over_a_healthy_setup_changes_nothing(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    first = capsys.readouterr().out
    before = harness.configuration().read_bytes()

    assert configure(harness) == 0
    assert capsys.readouterr().out == first
    assert harness.configuration().read_bytes() == before
    assert harness.service.minted == 1
    assert harness.credential() == f"{SECRET}-1"
    assert [kind for kind, _ in harness.service.seen] == [
        "mcp.configure",
        "mcp.configure",
    ]


def test_a_missing_local_configuration_is_repaired_by_rotating(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    first = harness.service.setups["claude-code"]["credential_reference"]
    harness.configuration().unlink()

    assert configure(harness) == 0
    capsys.readouterr()
    second = harness.service.setups["claude-code"]["credential_reference"]
    assert harness.service.minted == 2
    assert second != first
    assert harness.credential() == f"{SECRET}-2"
    # Superseded material is gone rather than merely unreferenced.
    assert harness.health(first) == "absent"
    # Invalidate first, then re-provision: never a live grant with no local half.
    assert [kind for kind, _ in harness.service.seen] == [
        "mcp.configure",
        "mcp.configure",
        "mcp.revoke",
        "mcp.configure",
    ]


def test_a_missing_local_credential_is_repaired_by_rotating(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    document = harness.stored()
    assert document is not None
    InstalledCredentialStore(harness.state).remove(
        CredentialReference(document["credential_reference"])
    )

    assert configure(harness) == 0
    capsys.readouterr()
    assert harness.service.minted == 2
    assert harness.credential() == f"{SECRET}-2"


def test_a_hand_edited_configuration_is_not_reported_as_settled(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    document = harness.stored()
    assert document is not None
    document["allowed_purposes"] = ["knowledge_retrieval"]
    harness.configuration().write_text(json.dumps(document), encoding="utf-8")

    assert configure(harness) == 0
    capsys.readouterr()
    assert harness.service.minted == 2
    assert harness.stored() != document


def test_a_profile_change_rotates_and_invalidates_the_superseded_material(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness, profile="restricted") == 0
    capsys.readouterr()
    first = harness.service.setups["claude-code"]["credential_reference"]

    assert configure(harness, profile="authoring") == 0
    capsys.readouterr()
    setup = harness.service.setups["claude-code"]
    assert setup["profile"] == "authoring"
    assert setup["authoring_intent"] is True
    assert setup["credential_reference"] != first
    assert harness.health(first) == "absent"
    document = harness.stored()
    assert document is not None
    assert document["mutation_enabled"] is True


def test_a_workspace_change_rotates(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    assert (
        harness.run(
            "configure",
            "--host",
            "claude-code",
            "--workspace",
            "ws-2",
            "--profile",
            "authoring",
        )
        == 0
    )
    capsys.readouterr()
    assert harness.service.minted == 2
    document = harness.stored()
    assert document is not None
    assert document["allowed_workspace_ids"] == ["ws-2"]


def test_each_host_is_configured_independently(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness, host="claude-code") == 0
    assert configure(harness, host="codex", profile="restricted") == 0
    capsys.readouterr()
    assert set(harness.service.setups) == {"claude-code", "codex"}
    assert harness.credential("claude-code") != harness.credential("codex")
    assert harness.stored("codex") is not None
    assert harness.stored("codex")["mutation_enabled"] is False  # type: ignore[index]


# --- refusals -----------------------------------------------------------------


def test_a_caller_who_is_not_an_administrator_is_refused_with_nothing_written(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.service.refusals["mcp.configure"] = "unauthorized"
    assert configure(harness) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SECRET not in captured.err
    assert harness.stored() is None
    assert harness.service.setups == {}


@pytest.mark.parametrize("code", ["refused", "malformed", "unsupported"])
def test_a_nonexistent_or_mismatched_workspace_is_refused_with_nothing_written(
    harness: Harness, code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.service.refusals["mcp.configure"] = code
    assert configure(harness) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert harness.stored() is None
    assert harness.verification.paths == []


def test_an_unreachable_service_refuses_before_anything_is_written(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.reachable = False
    assert configure(harness) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert harness.stored() is None
    assert harness.service.seen == []


# --- compensation -------------------------------------------------------------


def test_a_failed_handshake_revokes_the_authority_and_removes_the_local_half(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.verification.failure = RuntimeError(f"startup refused {SECRET}")
    harness.service.watch = mcp_admin.configuration_path(harness.state, "claude-code")

    assert configure(harness) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SECRET not in captured.err

    reference = harness.service.setups["claude-code"]["credential_reference"]
    assert harness.service.setups["claude-code"]["status"] == "revoked"
    assert harness.health(reference) == "absent"
    assert not mcp_admin.configuration_path(harness.state, "claude-code").exists()
    # Ordering: the revocation was answered while the local half was still there.
    assert harness.service.seen == [("mcp.configure", False), ("mcp.revoke", True)]


def test_a_failed_rotation_leaves_no_grant_and_no_superseded_material(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """The previous setup cannot be restored, so it is revoked rather than kept.

    Its bearer was handed over once and this installation kept only what it
    filed, so there is nothing to put back. What is left is the choice between an
    active grant nobody holds a usable credential for and no grant at all.
    """
    assert configure(harness, profile="restricted") == 0
    capsys.readouterr()
    first = harness.service.setups["claude-code"]["credential_reference"]
    harness.verification.failure = RuntimeError("startup refused")

    assert configure(harness, profile="authoring") == 1
    captured = capsys.readouterr()
    assert captured.out == ""

    second = harness.service.setups["claude-code"]["credential_reference"]
    assert second != first
    assert harness.service.setups["claude-code"]["status"] == "revoked"
    assert harness.health(first) == "absent"
    assert harness.health(second) == "absent"
    assert harness.stored() is None


def test_a_failed_handshake_whose_revocation_also_fails_reports_a_recoverable_state(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """The grant may still be live, so the local half that presents it stays.

    R004 section 9.2 forbids an active grant without recoverable configuration
    just as firmly as it forbids configuration pointing at a nonexistent
    principal. A compensation that deleted here would create the first while
    trying to avoid the second, and would leave `revoke` with no reference to
    invalidate. What is reported instead is the resumable state and the order
    that settles it.
    """
    harness.verification.failure = RuntimeError("startup refused")
    harness.service.unavailable.add("mcp.revoke")
    before = harness.configuration().parent

    assert configure(harness) == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        mcp_admin._NOT_VERIFIED,
        mcp_admin._RECOVERABLE,
    ]
    assert_nothing_disclosed(harness, captured)

    reference = harness.service.setups["claude-code"]["credential_reference"]
    assert harness.service.setups["claude-code"]["status"] == "active"
    assert harness.stored() is not None
    assert harness.health(reference) == "present"
    assert harness.credential() == f"{SECRET}-1"
    # Nothing was half-removed either: the store's own temporaries are gone.
    assert sorted(entry.name for entry in before.iterdir()) == ["claude-code.json"]


def test_status_after_an_unrevoked_compensation_reports_the_live_grant(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """The preserved local half is exactly what makes the state legible."""
    harness.verification.failure = RuntimeError("startup refused")
    harness.service.unavailable.add("mcp.revoke")
    assert configure(harness) == 1
    capsys.readouterr()

    assert harness.run("status", "--json") == 0
    captured = capsys.readouterr()
    row = json.loads(captured.out)["hosts"][0]
    assert row["grant"] == "active"
    assert row["configuration"] == "present"
    assert row["credential"] == "present"
    assert row["profile"] == "authoring"
    # The handshake is still failing, so nothing is claimed about the inventory.
    assert row["advertised_tool_count"] is None
    for forbidden in (SECRET, str(harness.state), str(harness.configuration())):
        assert forbidden not in captured.out


def test_a_bare_revoke_after_an_unrevoked_compensation_settles_both_halves(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """The remedy the recoverable sentence names, in the order it names it."""
    harness.verification.failure = RuntimeError("startup refused")
    harness.service.unavailable.add("mcp.revoke")
    assert configure(harness) == 1
    capsys.readouterr()
    reference = harness.service.setups["claude-code"]["credential_reference"]

    harness.service.unavailable.clear()
    harness.service.watch = harness.configuration()
    assert harness.run("revoke", "--host", "claude-code") == 0
    assert capsys.readouterr().out == "revoked claude-code\n"

    assert harness.service.setups["claude-code"]["status"] == "revoked"
    # Invalidated first, and only then removed -- the same invariant, from the
    # command the failed configure handed the state to.
    assert harness.service.seen[-1] == ("mcp.revoke", True)
    assert harness.stored() is None
    assert harness.health(reference) == "absent"


def test_a_configure_re_run_after_an_unrevoked_compensation_repairs_in_place(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """The preserved halves are the whole requested state, so nothing rotates.

    A compensation that had deleted them would have forced a fresh principal and
    a fresh bearer here for no reason but its own tidying.
    """
    harness.verification.failure = RuntimeError("startup refused")
    harness.service.unavailable.add("mcp.revoke")
    assert configure(harness) == 1
    capsys.readouterr()
    before = harness.configuration().read_bytes()

    harness.verification.failure = None
    harness.service.unavailable.clear()
    assert configure(harness) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["mcpServers"]["omnivia-core"]["args"] == [
        "--config",
        str(harness.configuration()),
    ]
    assert captured.err == ""
    assert harness.service.minted == 1
    assert harness.configuration().read_bytes() == before
    assert harness.credential() == f"{SECRET}-1"


# --- the same invariant at the two earlier compensation points ----------------
#
# A handshake that fails is the easy case: both halves are on disk, and both are
# the ones this configure wrote. The two failures before it are the ones where
# "roll back the local half" is ambiguous -- there may be no new local half at
# all, and what is on disk may be the *previous* setup's. The rule is the same
# one: nothing local is touched until the service has confirmed the authority is
# gone, and a superseded bearer is never restored, because it was handed over
# once and this installation kept only what it filed.


def refuse_credential_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The protected credential store refusing to file the new bearer."""

    def refuse(
        self: InstalledCredentialStore,
        reference: CredentialReference,
        credential: Credential,
    ) -> None:
        raise CredentialUnavailableError("the credential store could not be reached")

    monkeypatch.setattr(InstalledCredentialStore, "store", refuse)


def refuse_configuration_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The protected configuration store refusing to publish the document."""
    monkeypatch.setattr(
        InstalledConfigStore, "write", lambda self, host, content: False
    )


def test_a_credential_write_failure_revokes_before_it_discards(
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert configure(harness, profile="restricted") == 0
    capsys.readouterr()
    superseded = harness.service.setups["claude-code"]["credential_reference"]
    refuse_credential_write(monkeypatch)
    harness.service.watch = harness.configuration()

    assert configure(harness, profile="authoring") == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        mcp_admin._NOT_PUBLISHED,
        mcp_admin._ROLLED_BACK,
    ]
    assert_nothing_disclosed(harness, captured, superseded)

    minted = harness.service.setups["claude-code"]["credential_reference"]
    assert harness.service.setups["claude-code"]["status"] == "revoked"
    # The revocation was answered while the superseded configuration was still
    # there, and everything this host had material for went afterwards.
    assert harness.service.seen[-1] == ("mcp.revoke", True)
    assert harness.stored() is None
    assert harness.health(superseded) == "absent"
    assert harness.health(minted) == "absent"


def test_a_credential_write_failure_whose_revocation_fails_keeps_the_earlier_half(
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No usable new local half exists, and the old one is not deleted for it.

    The new authority may be live, and this installation cannot present it -- the
    bearer never reached the store. Deleting the previous setup's material on top
    of that would remove the only reference a later `revoke` can name. It is kept
    exactly as it stands, and it is not *restored* either: nothing was undone,
    because nothing about it was touched.
    """
    assert configure(harness, profile="restricted") == 0
    capsys.readouterr()
    superseded = harness.service.setups["claude-code"]["credential_reference"]
    before = harness.configuration().read_bytes()
    refuse_credential_write(monkeypatch)
    harness.service.unavailable.add("mcp.revoke")

    assert configure(harness, profile="authoring") == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        mcp_admin._NOT_PUBLISHED,
        mcp_admin._RECOVERABLE,
    ]
    assert_nothing_disclosed(harness, captured, superseded)

    assert harness.service.setups["claude-code"]["status"] == "active"
    assert harness.configuration().read_bytes() == before
    assert harness.health(superseded) == "present"

    # And the recoverable state is one a bare revoke settles.
    harness.service.unavailable.clear()
    assert harness.run("revoke", "--host", "claude-code") == 0
    capsys.readouterr()
    assert harness.service.setups["claude-code"]["status"] == "revoked"
    assert harness.stored() is None
    assert harness.health(superseded) == "absent"


def test_a_configuration_write_failure_revokes_before_it_discards(
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refuse_configuration_write(monkeypatch)

    assert configure(harness) == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        mcp_admin._NOT_PUBLISHED,
        mcp_admin._ROLLED_BACK,
    ]
    assert_nothing_disclosed(harness, captured)

    reference = harness.service.setups["claude-code"]["credential_reference"]
    assert harness.service.setups["claude-code"]["status"] == "revoked"
    assert [kind for kind, _ in harness.service.seen] == ["mcp.configure", "mcp.revoke"]
    assert harness.health(reference) == "absent"
    assert harness.stored() is None
    assert harness.verification.paths == []


def test_a_configuration_write_failure_whose_revocation_fails_keeps_the_new_bearer(
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bearer is filed and the grant may be live: the pair is what revoke needs."""
    refuse_configuration_write(monkeypatch)
    harness.service.unavailable.add("mcp.revoke")

    assert configure(harness) == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        mcp_admin._NOT_PUBLISHED,
        mcp_admin._RECOVERABLE,
    ]
    reference = harness.service.setups["claude-code"]["credential_reference"]
    assert_nothing_disclosed(harness, captured, reference)

    assert harness.service.setups["claude-code"]["status"] == "active"
    assert harness.health(reference) == "present"

    # Status still reports it, without a configuration it does not have.
    assert harness.run("status", "--json") == 0
    row = json.loads(capsys.readouterr().out)["hosts"][0]
    assert (row["grant"], row["configuration"], row["credential"]) == (
        "active",
        "absent",
        "present",
    )

    # And a bare revoke reaches the orphaned bearer through the service's row.
    harness.service.unavailable.clear()
    assert harness.run("revoke", "--host", "claude-code") == 0
    capsys.readouterr()
    assert harness.service.setups["claude-code"]["status"] == "revoked"
    assert harness.health(reference) == "absent"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_cleanup_a_protected_store_refuses_is_not_reported_as_a_rollback(
    harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Revocation confirmed, removal refused: say so rather than claim a rollback.

    A substituted credential store is the case where both halves of the
    compensation land differently. The authority is gone -- that is what makes
    this safe -- but unusable local material is still there, and the store was
    right to refuse: unlinking down a substituted directory would delete whatever
    stands at the end of it.
    """
    planted = tmp_path / "elsewhere" / ".installed-credentials"
    planted.mkdir(parents=True, mode=0o700)
    planted.parent.chmod(0o700)
    (harness.state / "runtime" / ".installed-credentials").symlink_to(
        planted, target_is_directory=True
    )

    assert configure(harness) == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        mcp_admin._NOT_PUBLISHED,
        mcp_admin._ROLLED_BACK_PARTLY,
    ]
    assert_nothing_disclosed(harness, captured)

    assert harness.service.setups["claude-code"]["status"] == "revoked"
    assert sorted(planted.iterdir()) == []

    # Nothing is stuck: status answers, and a bare revoke is still idempotent.
    assert harness.run("status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["hosts"][0]["grant"] == "revoked"
    assert harness.run("revoke", "--host", "claude-code") == 0
    capsys.readouterr()


@pytest.mark.parametrize("point", ["credential", "configuration", "handshake"])
@pytest.mark.parametrize("revocation", ["confirmed", "unreachable"])
def test_no_compensation_point_discloses_anything_it_touched(
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    revocation: str,
) -> None:
    """Six compensations, and the same two fixed sentences out of all of them.

    Nothing a store read, nothing a peer wrote and nothing this module composed:
    not the bearer, not either credential reference, not the configuration path,
    not the installation root, not the layout under it, and not the text of the
    exception that started the compensation.
    """
    assert configure(harness, profile="restricted") == 0
    capsys.readouterr()
    superseded = harness.service.setups["claude-code"]["credential_reference"]
    if point == "credential":
        refuse_credential_write(monkeypatch)
    elif point == "configuration":
        refuse_configuration_write(monkeypatch)
    else:
        harness.verification.failure = RuntimeError(f"startup refused {SECRET}")
    if revocation == "unreachable":
        harness.service.unavailable.add("mcp.revoke")

    assert configure(harness, profile="authoring") == 1
    captured = capsys.readouterr()
    minted = harness.service.setups["claude-code"]["credential_reference"]
    assert_nothing_disclosed(harness, captured, superseded, minted)
    assert len(captured.err.splitlines()) == 2


def test_an_absent_mcp_distribution_is_reported_and_compensated(
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default seam looks the MCP server up by name and fails closed.

    The one test that exercises the real seam rather than the fake: an
    installation without the MCP distribution gets one fixed sentence and a
    compensated setup, not a traceback out of an import.
    """
    monkeypatch.setattr(mcp_admin, "_MCP_SERVER_MODULE", "omnivia_core_mcp.absent")
    assert (
        main(
            [
                "--installation-state",
                str(harness.state),
                "mcp",
                "configure",
                "--host",
                "claude-code",
                "--workspace",
                WORKSPACE,
                "--profile",
                "restricted",
            ],
            mcp_seams=Seams(
                control=harness.seams.control, verify=mcp_admin._default_verification
            ),
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not available" in captured.err
    assert harness.service.setups["claude-code"]["status"] == "revoked"
    assert harness.stored() is None


# --- status -------------------------------------------------------------------


def test_status_reports_every_host_redacted(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()

    assert harness.run("status", "--json") == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    document = json.loads(captured.out)
    assert document["mcp_status_version"] == 1
    configured, unconfigured = document["hosts"]
    setup = harness.service.setups["claude-code"]
    assert configured == {
        "advertised_tool_count": 11,
        "authoring_intent": True,
        "configuration": "present",
        "credential": "present",
        "grant": "active",
        "host": "claude-code",
        "principal_id": setup["principal_id"],
        "profile": "authoring",
        "service": "reachable",
        "workspace_id": WORKSPACE,
    }
    assert unconfigured == {
        "advertised_tool_count": None,
        "authoring_intent": None,
        "configuration": "absent",
        "credential": "absent",
        "grant": "absent",
        "host": "codex",
        "principal_id": None,
        "profile": None,
        "service": "reachable",
        "workspace_id": None,
    }


def test_status_discloses_no_path_endpoint_or_secret(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    assert harness.run("status", "--json") == 0
    printed = capsys.readouterr().out
    for forbidden in (
        SECRET,
        str(harness.state),
        str(harness.configuration()),
        "runtime",
        "salt",
        "digest",
        "socket",
    ):
        assert forbidden not in printed


def test_status_narrows_to_one_host(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    assert harness.run("status", "--host", "codex", "--json") == 0
    document = json.loads(capsys.readouterr().out)
    assert [row["host"] for row in document["hosts"]] == ["codex"]


def test_status_reports_an_unreachable_service_without_guessing(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    harness.reachable = False

    assert harness.run("status", "--json") == 1
    document = json.loads(capsys.readouterr().out)
    row = document["hosts"][0]
    assert row["service"] == "unreachable"
    assert row["grant"] == "unknown"
    assert row["advertised_tool_count"] is None
    # What the local half alone can say, it still says.
    assert row["profile"] == "authoring"
    assert row["credential"] == "present"


def test_status_reports_a_mismatched_configuration(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    document = harness.stored()
    assert document is not None
    document["default_workspace_id"] = "ws-other"
    harness.configuration().write_text(json.dumps(document), encoding="utf-8")

    assert harness.run("status", "--json") == 0
    row = json.loads(capsys.readouterr().out)["hosts"][0]
    assert row["configuration"] == "mismatched"
    assert row["advertised_tool_count"] is None


def test_status_reports_an_unusable_configuration(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    harness.configuration().write_bytes(b"\xff\xfe not json")

    assert harness.run("status", "--json") == 0
    row = json.loads(capsys.readouterr().out)["hosts"][0]
    assert row["configuration"] == "unusable"


def test_the_human_status_view_is_the_same_redacted_values(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    assert harness.run("status") == 0
    printed = capsys.readouterr().out
    assert SECRET not in printed
    assert printed.splitlines()[0].startswith("advertised_tool_count=11 ")
    assert "host=claude-code" in printed
    assert "host=codex" in printed


# --- revoke -------------------------------------------------------------------


def test_revoke_defaults_to_every_configured_host(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness, host="claude-code") == 0
    assert configure(harness, host="codex") == 0
    capsys.readouterr()
    references = [
        setup["credential_reference"] for setup in harness.service.setups.values()
    ]

    assert harness.run("revoke") == 0
    captured = capsys.readouterr()
    assert captured.out == "revoked claude-code\nrevoked codex\n"
    assert SECRET not in captured.out
    for host in ("claude-code", "codex"):
        assert harness.service.setups[host]["status"] == "revoked"
        assert not mcp_admin.configuration_path(harness.state, host).exists()
    for reference in references:
        assert harness.health(reference) == "absent"


def test_revoke_invalidates_authority_before_removing_the_local_half(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness, host="claude-code") == 0
    capsys.readouterr()
    harness.service.watch = mcp_admin.configuration_path(harness.state, "claude-code")

    assert harness.run("revoke", "--host", "claude-code") == 0
    capsys.readouterr()
    assert harness.service.seen[-1] == ("mcp.revoke", True)


def test_revoke_is_idempotent_and_never_touches_workspace_state(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    assert harness.run("revoke") == 0
    capsys.readouterr()
    assert harness.run("revoke") == 0
    assert capsys.readouterr().out == "revoked claude-code\nrevoked codex\n"
    # A revoke over a host that was never configured is already in the state it
    # asked for, and nothing about the installation's workspaces was named.
    assert harness.service.minted == 1


def test_a_revoke_that_cannot_reach_the_service_keeps_the_local_half(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    harness.service.unavailable.add("mcp.revoke")

    assert harness.run("revoke") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    # Removing the local half under a live grant is the one state this must not
    # create, so the file stays exactly where it was.
    assert harness.stored() is not None
    assert harness.service.setups["claude-code"]["status"] == "active"


def test_an_unreachable_service_refuses_a_revoke_rather_than_reporting_one(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    harness.reachable = False

    assert harness.run("revoke") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert harness.stored() is not None


# --- the bearer never leaves owner-private storage ----------------------------


def test_no_command_ever_puts_the_bearer_in_a_stream_or_a_configuration(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    printed: list[str] = []
    for argv in (
        [
            "configure",
            "--host",
            "claude-code",
            "--workspace",
            WORKSPACE,
            "--profile",
            "authoring",
        ],
        [
            "configure",
            "--host",
            "codex",
            "--workspace",
            WORKSPACE,
            "--profile",
            "restricted",
        ],
        ["status", "--json"],
        ["status"],
        ["revoke"],
    ):
        harness.run(*argv)
        captured = capsys.readouterr()
        printed.extend((captured.out, captured.err))
    assert SECRET not in "".join(printed)
    for path in harness.state.rglob("*"):
        if path.is_file() and mcp_admin.CONFIGURATION_DIRECTORY[1] in path.parts:
            assert SECRET not in path.read_text(encoding="utf-8")


def test_the_bearer_reaches_the_credential_store_and_only_it(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    holders = [
        path
        for path in harness.state.rglob("*")
        if path.is_file() and SECRET in path.read_bytes().decode("utf-8", "replace")
    ]
    assert [path.parent.name for path in holders] == [".installed-credentials"]
    assert stat.S_IMODE(holders[0].stat().st_mode) == 0o600 or os.name == "nt"


# --- the local half is mutated only through the protected store ----------------
#
# `write_owner_private` proves the directory a document lands in and nothing
# above it, and `Path.unlink` proves nothing at all. Both were how this module
# published and removed a configuration; neither is now. What replaced them is
# `InstalledConfigStore`, which walks the installation root, `runtime/` and its
# own directory before it touches anything -- so a substituted component ends the
# operation instead of redirecting it.


def substituted_store(harness: Harness, tmp_path: Path) -> Path:
    """Move the real configuration directory aside and stand a decoy in its place.

    The decoy is laid out as a *working* store holding this host's document, so a
    module that resolved the configuration path as a pathname would read it,
    overwrite it or delete it rather than refusing.
    """
    real = harness.configuration().parent
    planted = tmp_path / "elsewhere" / mcp_admin.CONFIGURATION_DIRECTORY[1]
    planted.mkdir(parents=True, mode=0o700)
    planted.parent.chmod(0o700)
    document = planted / "claude-code.json"
    document.write_bytes(b'{"format": "attacker"}\n')
    document.chmod(0o600)
    for entry in real.iterdir():
        entry.unlink()
    real.rmdir()
    real.symlink_to(planted, target_is_directory=True)
    return document


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_revoke_will_not_delete_through_a_substituted_configuration_directory(
    harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one operation that cannot be taken back is the one proved hardest.

    A revoke that unlinked the configuration by pathname would follow the
    substituted directory and delete a file somebody else chose. The authority is
    still invalidated -- that is what a revoke is for, and it is what leaves no
    live grant behind -- and the local removal that cannot be proved simply does
    not happen.
    """
    assert configure(harness) == 0
    capsys.readouterr()
    planted = substituted_store(harness, tmp_path)

    assert harness.run("revoke", "--host", "claude-code") == 0
    capsys.readouterr()

    assert harness.service.setups["claude-code"]["status"] == "revoked"
    assert planted.read_bytes() == b'{"format": "attacker"}\n'
    assert [path.name for path in planted.parent.iterdir()] == ["claude-code.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_status_reports_a_configuration_behind_a_substituted_store_as_unusable(
    harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not `absent`: something local is wrong, and saying "unconfigured" would
    describe a host that has a live grant as one that has nothing."""
    assert configure(harness) == 0
    capsys.readouterr()
    substituted_store(harness, tmp_path)

    assert harness.run("status", "--json") == 0
    row = json.loads(capsys.readouterr().out)["hosts"][0]
    assert row["configuration"] == "unusable"
    assert row["advertised_tool_count"] is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_configure_over_a_substituted_store_refuses_and_writes_nothing_into_it(
    harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert configure(harness) == 0
    capsys.readouterr()
    planted = substituted_store(harness, tmp_path)

    assert configure(harness) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SECRET not in captured.err
    assert planted.read_bytes() == b'{"format": "attacker"}\n'
    assert [path.name for path in planted.parent.iterdir()] == ["claude-code.json"]
    assert harness.service.setups["claude-code"]["status"] == "revoked"


def test_this_module_publishes_and_removes_through_no_pathname_of_its_own() -> None:
    """Asserted over the source, because the defect it guards is an *absence*.

    A behavioural test can show that the store refuses a substituted directory; it
    cannot show that a future edit did not reintroduce a second, unproved way to
    write or delete beside it. Reading the source can. `read_owner_private` and
    `write_owner_private` remain the right tools for a standalone trusted file --
    the MCP package's configuration reader is one -- and are simply not how
    installed administration mutates its own state.
    """
    source = Path(mcp_admin.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "write_owner_private(",
        "read_owner_private(",
        ".unlink(",
        ".mkdir(",
        "os.replace(",
    ):
        assert forbidden not in source, forbidden
