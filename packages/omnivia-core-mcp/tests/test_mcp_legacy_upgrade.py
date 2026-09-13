"""Safe, automatic migration of pre-credential managed-local configurations.

The old document is still trusted configuration, but it cannot authenticate an
MCP application call.  Startup therefore provisions a dedicated *restricted*
principal over the service's protected local control, publishes the bearer and
configuration in that order, and either settles or leaves a retryable state.
Nothing in this module imports the runtime; the control peer below exercises the
same public client result types the real service returns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from omnivia_core_client import (
    CredentialReference,
    InstalledCredentialStore,
    McpConfigureResult,
    McpRevokeResult,
    McpSetupView,
    McpStatusResult,
)
from omnivia_core_mcp import server
from omnivia_core_mcp.configuration import (
    McpConfiguration,
    parse_configuration,
    read_configuration,
)

WORKSPACE = "ws-legacy-upgrade"
OTHER_WORKSPACE = "ws-other"
LEGACY_PRINCIPAL = "legacy-local-user"
PURPOSES = ("workspace_inspection", "knowledge_retrieval", "memory_authoring")


@dataclass
class ControlPeer:
    """A redacted local-control peer with the service's configure semantics."""

    setups: dict[str, McpSetupView] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    minted: int = 0
    revoke_fails: bool = False

    def status(self, *_args: Any, **_kwargs: Any) -> McpStatusResult:
        self.events.append("status")
        return McpStatusResult(setups=tuple(self.setups.values()))

    def configure(
        self,
        *_args: Any,
        host: str,
        workspace_id: str,
        profile: str,
        authoring_intent: bool,
        **_kwargs: Any,
    ) -> McpConfigureResult:
        self.events.append(f"configure:{host}:{profile}:{authoring_intent}")
        current = self.setups.get(host)
        if (
            current is not None
            and current.status == "active"
            and current.workspace_id == workspace_id
            and current.profile == profile
            and current.authoring_intent is authoring_intent
        ):
            return McpConfigureResult(setup=current, rotated=False, _secret=None)

        self.minted += 1
        generation = 1 if current is None else current.setup_generation + 1
        setup = McpSetupView(
            setup_id=f"mcp-setup-{self.minted}",
            host=host,
            workspace_id=workspace_id,
            principal_id=f"mcp-{host}-{self.minted}",
            profile=profile,
            authoring_intent=authoring_intent,
            credential_reference=f"omcp-{self.minted:032x}",
            status="active",
            setup_generation=generation,
        )
        self.setups[host] = setup
        return McpConfigureResult(
            setup=setup,
            rotated=True,
            _secret=f"omcp_live_{self.minted:032x}",
        )

    def revoke(self, *_args: Any, host: str, **_kwargs: Any) -> McpRevokeResult:
        self.events.append(f"revoke:{host}")
        if self.revoke_fails:
            raise OSError("peer failure containing private installation details")
        current = self.setups.get(host)
        if current is None:
            return McpRevokeResult(setup=None)
        revoked = replace(
            current,
            status="revoked",
            setup_generation=current.setup_generation + 1,
        )
        self.setups[host] = revoked
        return McpRevokeResult(setup=revoked)


def setup_view(
    host: str,
    *,
    workspace_id: str = WORKSPACE,
    profile: str = "restricted",
    authoring_intent: bool = False,
    status: str = "active",
    generation: int = 1,
) -> McpSetupView:
    suffix = 1 if host == "claude-code" else 2
    return McpSetupView(
        setup_id=f"existing-{suffix}",
        host=host,
        workspace_id=workspace_id,
        principal_id=f"existing-principal-{suffix}",
        profile=profile,
        authoring_intent=authoring_intent,
        credential_reference=f"omcp-existing-{suffix}",
        status=status,
        setup_generation=generation,
    )


def legacy_file(
    tmp_path: Path,
    *,
    mutation_enabled: bool | None = None,
    workspaces: tuple[str, ...] = (WORKSPACE, OTHER_WORKSPACE),
    default_workspace: str | None = WORKSPACE,
) -> Path:
    state = tmp_path / "installation"
    state.mkdir()
    document: dict[str, Any] = {
        "format": "omnivia.mcp-config.v1",
        "principal_id": LEGACY_PRINCIPAL,
        "allowed_workspace_ids": list(workspaces),
        "allowed_purposes": list(PURPOSES),
        "service_mode": "managed_local",
        "installation_state": str(state),
    }
    if default_workspace is not None:
        document["default_workspace_id"] = default_workspace
    if mutation_enabled is not None:
        document["mutation_enabled"] = mutation_enabled
    path = tmp_path / "legacy-mcp.json"
    assert server.write_owner_private(
        path, (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    )
    return path


def attach_control(monkeypatch: pytest.MonkeyPatch, peer: ControlPeer) -> None:
    client = object()
    monkeypatch.setattr(
        server,
        "connect_managed_local",
        lambda *_args, **_kwargs: SimpleNamespace(client=client, status="attached"),
    )
    monkeypatch.setattr(
        server,
        "local_control_transport",
        lambda connected: peer if connected is client else pytest.fail("wrong client"),
    )
    monkeypatch.setattr(server, "mcp_status", peer.status)
    monkeypatch.setattr(server, "mcp_configure", peer.configure)
    monkeypatch.setattr(server, "mcp_revoke", peer.revoke)


@pytest.mark.parametrize("legacy_mutation", [None, False, True])
def test_legacy_startup_mints_only_restricted_authority_and_settles_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_mutation: bool | None,
) -> None:
    path = legacy_file(tmp_path, mutation_enabled=legacy_mutation)
    legacy = read_configuration(path)
    peer = ControlPeer()
    attach_control(monkeypatch, peer)

    upgraded = server.upgrade_legacy_configuration(path, legacy)

    assert upgraded == read_configuration(path)
    assert upgraded.principal_id == "mcp-claude-code-1"
    assert upgraded.allowed_workspace_ids == (WORKSPACE,)
    assert upgraded.default_workspace_id == WORKSPACE
    assert upgraded.allowed_purposes == PURPOSES
    assert upgraded.mutation_enabled is False
    assert upgraded.credential_reference is not None
    assert upgraded.installation_state is not None
    store = InstalledCredentialStore(upgraded.installation_state)
    assert store.health(upgraded.credential_reference) == "present"
    assert peer.events == ["status", "configure:claude-code:restricted:False"]

    before = list(peer.events)
    assert server.upgrade_legacy_configuration(path, upgraded) is upgraded
    assert peer.events == before, "a settled document must not touch local control"


def test_migration_never_displaces_live_authoring_or_another_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_file(tmp_path)
    authoring = setup_view("claude-code", profile="authoring", authoring_intent=True)
    peer = ControlPeer(setups={"claude-code": authoring})
    attach_control(monkeypatch, peer)

    upgraded = server.upgrade_legacy_configuration(path, read_configuration(path))

    assert peer.setups["claude-code"] == authoring
    assert peer.setups["codex"].profile == "restricted"
    assert upgraded.credential_reference == CredentialReference(
        peer.setups["codex"].credential_reference
    )
    assert peer.events == ["status", "configure:codex:restricted:False"]


def test_migration_refuses_when_every_bounded_host_slot_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_file(tmp_path)
    peer = ControlPeer(
        setups={
            "claude-code": setup_view(
                "claude-code", profile="authoring", authoring_intent=True
            ),
            "codex": setup_view("codex", workspace_id=OTHER_WORKSPACE),
        }
    )
    attach_control(monkeypatch, peer)

    with pytest.raises(server.StartupError) as refused:
        server.upgrade_legacy_configuration(path, read_configuration(path))

    assert "upgraded safely" in str(refused.value)
    assert peer.events == ["status"]
    assert read_configuration(path).credential_reference is None


def test_configuration_publication_failure_rolls_back_then_retries_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_file(tmp_path)
    peer = ControlPeer()
    attach_control(monkeypatch, peer)
    write = server.write_owner_private
    attempts = 0

    def fail_once(target: Path, content: bytes) -> bool:
        nonlocal attempts
        attempts += 1
        return False if attempts == 1 else write(target, content)

    monkeypatch.setattr(server, "write_owner_private", fail_once)
    with pytest.raises(server.StartupError) as refused:
        server.upgrade_legacy_configuration(path, read_configuration(path))

    first = peer.setups["claude-code"]
    first_reference = CredentialReference(first.credential_reference)
    store = InstalledCredentialStore(path.parent / "installation")
    assert first.status == "revoked"
    assert store.health(first_reference) == "absent"
    assert read_configuration(path).credential_reference is None
    rendered = " ".join((str(refused.value), repr(refused.value.args)))
    assert refused.value.__context__ is None
    for private in (
        str(path),
        str(path.parent / "installation"),
        first.credential_reference,
    ):
        assert private not in rendered

    upgraded = server.upgrade_legacy_configuration(path, read_configuration(path))
    assert upgraded.credential_reference is not None
    assert upgraded.credential_reference != first_reference
    assert store.health(upgraded.credential_reference) == "present"
    assert peer.minted == 2


def test_uncertain_revocation_retains_a_resumable_pair_and_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_file(tmp_path)
    peer = ControlPeer(revoke_fails=True)
    attach_control(monkeypatch, peer)
    write = server.write_owner_private
    attempts = 0

    def fail_once(target: Path, content: bytes) -> bool:
        nonlocal attempts
        attempts += 1
        return False if attempts == 1 else write(target, content)

    monkeypatch.setattr(server, "write_owner_private", fail_once)
    with pytest.raises(server.StartupError) as refused:
        server.upgrade_legacy_configuration(path, read_configuration(path))

    retained = peer.setups["claude-code"]
    reference = CredentialReference(retained.credential_reference)
    store = InstalledCredentialStore(path.parent / "installation")
    assert retained.status == "active"
    assert store.health(reference) == "present"
    assert read_configuration(path).credential_reference is None
    assert refused.value.__context__ is None

    peer.revoke_fails = False
    upgraded = server.upgrade_legacy_configuration(path, read_configuration(path))
    assert upgraded.credential_reference == reference
    assert peer.minted == 1, "the retained grant must be reused rather than rotated"
    assert peer.events == [
        "status",
        "configure:claude-code:restricted:False",
        "revoke:claude-code",
        "status",
        "configure:claude-code:restricted:False",
    ]


def test_readback_must_match_the_whole_published_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-reference replacement cannot widen or narrow another field."""
    path = legacy_file(tmp_path, mutation_enabled=True)
    legacy = read_configuration(path)
    peer = ControlPeer()
    attach_control(monkeypatch, peer)
    actual_read = server.read_configuration

    def tampered_read(target: Path) -> McpConfiguration:
        configured = actual_read(target)
        return replace(configured, allowed_purposes=("workspace_inspection",))

    monkeypatch.setattr(server, "read_configuration", tampered_read)
    with pytest.raises(server.StartupError):
        server.upgrade_legacy_configuration(path, legacy)

    setup = peer.setups["claude-code"]
    reference = CredentialReference(setup.credential_reference)
    assert setup.status == "revoked"
    assert InstalledCredentialStore(path.parent / "installation").health(reference) == (
        "absent"
    )
    restored = read_configuration(path)
    assert restored.credential_reference is None
    assert restored.allowed_purposes == PURPOSES
    assert restored.mutation_enabled is False


def test_an_ambiguous_legacy_workspace_refuses_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_file(tmp_path, default_workspace=None)
    called = False

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("ambiguous legacy configuration reached the service")

    monkeypatch.setattr(server, "connect_managed_local", unexpected)
    with pytest.raises(server.StartupError):
        server.upgrade_legacy_configuration(path, read_configuration(path))
    assert called is False


def test_main_upgrades_before_it_builds_admission_or_connects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = legacy_file(tmp_path, workspaces=(WORKSPACE,))
    legacy = read_configuration(path)
    upgraded = parse_configuration(
        {
            "format": "omnivia.mcp-config.v1",
            "principal_id": "mcp-upgraded-principal",
            "allowed_workspace_ids": [WORKSPACE],
            "default_workspace_id": WORKSPACE,
            "allowed_purposes": list(PURPOSES),
            "mutation_enabled": False,
            "service_mode": "managed_local",
            "installation_state": str(path.parent / "installation"),
            "credential_reference": "omcp-upgraded-reference",
        }
    )
    events: list[str] = []
    admission = object()

    class Session:
        status = "attached"
        workspace_id = WORKSPACE

        def clear_credentials(self) -> None:
            events.append("clear")

    monkeypatch.setattr(
        server,
        "read_configuration",
        lambda target: (
            (events.append("read"), legacy)[1]
            if target == path
            else pytest.fail("wrong path")
        ),
    )
    monkeypatch.setattr(
        server,
        "upgrade_legacy_configuration",
        lambda target, configuration: (
            events.append("upgrade"),
            upgraded if target == path and configuration is legacy else pytest.fail(),
        )[1],
    )
    monkeypatch.setattr(
        server,
        "_installed_admission",
        lambda configuration: (
            events.append("admission"),
            admission if configuration is upgraded else pytest.fail(),
        )[1],
    )

    def connect(configuration: McpConfiguration, **kwargs: Any) -> Session:
        events.append("connect")
        assert configuration is upgraded
        assert kwargs["authoring_admission"] is admission
        return Session()

    async def serve(*, session: Session) -> None:
        events.append("serve")
        assert isinstance(session, Session)

    monkeypatch.setattr(server, "connect", connect)
    monkeypatch.setattr(server, "serve", serve)

    assert server.main(["--config", str(path)]) == 0
    assert events == ["read", "upgrade", "admission", "connect", "serve", "clear"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"omnivia-core: attached {WORKSPACE}\n"
