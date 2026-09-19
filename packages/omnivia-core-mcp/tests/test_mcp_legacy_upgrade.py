"""Fail-closed migration of pre-credential managed-local configurations.

Startup never creates authority and cannot infer which host launched it. It may
only finish publication of one unambiguous restricted setup whose protected
bearer already exists; every other legacy state requires explicit configure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from omnivia_core_client import (
    AuthoringAdmissionResult,
    ClientError,
    Credential,
    CredentialReference,
    InstalledCredentialStore,
    McpSetupView,
    McpStatusResult,
    write_owner_private,
)
from omnivia_core_client.owner_private import replace_owner_private_if_current
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
STORED_SECRET = "omcp_live_00112233445566778899aabbccddeeff"


@dataclass
class ControlPeer:
    """A redacted local-control peer exposing existing setup state only."""

    setups: dict[str, McpSetupView] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)

    def status(self, *_args: Any, **_kwargs: Any) -> McpStatusResult:
        self.events.append("status")
        return McpStatusResult(setups=tuple(self.setups.values()))

    def admission(
        self, _transport: Any, credential: str, *_args: Any, **_kwargs: Any
    ) -> AuthoringAdmissionResult:
        self.events.append("admission")
        matching = [
            setup
            for setup in self.setups.values()
            if setup.status == "active" and credential == STORED_SECRET
        ]
        if len(matching) != 1:
            raise ClientError("test credential is not active")
        setup = matching[0]
        return AuthoringAdmissionResult(
            admitted=setup.profile == "authoring" and setup.authoring_intent,
            principal_id=setup.principal_id,
            workspace_id=setup.workspace_id,
        )


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
    assert write_owner_private(
        path, (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    )
    return path


def configured_document(path: Path, *, suffix: str = "concurrent") -> bytes:
    """A complete valid document representing a later explicit configure."""
    return (
        json.dumps(
            {
                "allowed_purposes": list(PURPOSES),
                "allowed_workspace_ids": [WORKSPACE],
                "credential_reference": f"omcp-{suffix}-reference",
                "default_workspace_id": WORKSPACE,
                "format": "omnivia.mcp-config.v1",
                "installation_state": str(path.parent / "installation"),
                "mutation_enabled": False,
                "principal_id": f"mcp-{suffix}-principal",
                "service_mode": "managed_local",
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


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
    monkeypatch.setattr(server, "mcp_authoring_admission", peer.admission)


def store_setup(path: Path, setup: McpSetupView) -> CredentialReference:
    reference = CredentialReference(setup.credential_reference)
    InstalledCredentialStore(path.parent / "installation").store(
        reference, Credential(STORED_SECRET)
    )
    return reference


@pytest.mark.parametrize("legacy_mutation", [None, False, True])
def test_legacy_startup_without_prior_setup_refuses_without_creating_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_mutation: bool | None,
) -> None:
    path = legacy_file(tmp_path, mutation_enabled=legacy_mutation)
    peer = ControlPeer()
    attach_control(monkeypatch, peer)

    with pytest.raises(server.StartupError) as refused:
        server.upgrade_legacy_configuration(path, read_configuration(path))

    assert "configure command" in str(refused.value)
    assert peer.events == ["status"]
    assert read_configuration(path).credential_reference is None
    assert peer.setups == {}, "startup must not mint or widen authority"


@pytest.mark.parametrize("host", ["claude-code", "codex"])
def test_one_interrupted_restricted_setup_is_published_without_grant_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    path = legacy_file(tmp_path, mutation_enabled=True)
    existing = setup_view(host)
    reference = store_setup(path, existing)
    peer = ControlPeer(setups={host: existing})
    attach_control(monkeypatch, peer)

    upgraded = server.upgrade_legacy_configuration(path, read_configuration(path))

    assert upgraded == read_configuration(path)
    assert upgraded.principal_id == existing.principal_id
    assert upgraded.allowed_workspace_ids == (WORKSPACE,)
    assert upgraded.default_workspace_id == WORKSPACE
    assert upgraded.allowed_purposes == PURPOSES
    assert upgraded.mutation_enabled is False
    assert upgraded.credential_reference == reference
    assert peer.events == ["status", "admission", "admission"]
    assert peer.setups == {host: existing}

    before = list(peer.events)
    assert server.upgrade_legacy_configuration(path, upgraded) is upgraded
    assert peer.events == before, "a settled document must not touch local control"


@pytest.mark.parametrize(
    "existing",
    [
        setup_view("claude-code", profile="authoring", authoring_intent=True),
        setup_view("claude-code", workspace_id=OTHER_WORKSPACE),
        setup_view("claude-code", status="revoked"),
    ],
    ids=["authoring", "other-workspace", "revoked"],
)
def test_migration_never_reuses_nonmatching_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: McpSetupView
) -> None:
    path = legacy_file(tmp_path)
    store_setup(path, existing)
    peer = ControlPeer(setups={existing.host: existing})
    attach_control(monkeypatch, peer)

    with pytest.raises(server.StartupError):
        server.upgrade_legacy_configuration(path, read_configuration(path))

    assert peer.events == ["status"]
    assert peer.setups == {existing.host: existing}
    assert read_configuration(path).credential_reference is None


def test_two_matching_hosts_are_ambiguous_and_neither_is_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_file(tmp_path)
    claude = setup_view("claude-code")
    codex = setup_view("codex")
    store_setup(path, claude)
    store_setup(path, codex)
    peer = ControlPeer(setups={"claude-code": claude, "codex": codex})
    attach_control(monkeypatch, peer)

    with pytest.raises(server.StartupError):
        server.upgrade_legacy_configuration(path, read_configuration(path))

    assert peer.events == ["status"]
    assert read_configuration(path).credential_reference is None


def test_configuration_publication_failure_preserves_existing_authority_and_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_file(tmp_path)
    existing = setup_view("codex")
    reference = store_setup(path, existing)
    peer = ControlPeer(setups={"codex": existing})
    attach_control(monkeypatch, peer)
    monkeypatch.setattr(
        server,
        "replace_owner_private_if_current",
        lambda *_args, **_kwargs: "unavailable",
    )
    with pytest.raises(server.StartupError) as refused:
        server.upgrade_legacy_configuration(path, read_configuration(path))

    store = InstalledCredentialStore(path.parent / "installation")
    assert peer.setups == {"codex": existing}
    assert peer.events == ["status", "admission"]
    assert store.health(reference) == "present"
    assert read_configuration(path).credential_reference is None
    rendered = " ".join((str(refused.value), repr(refused.value.args)))
    assert refused.value.__context__ is None
    for private in (
        str(path),
        str(path.parent / "installation"),
        existing.credential_reference,
    ):
        assert private not in rendered


def test_a_newer_configuration_is_not_overwritten_at_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publication compare-and-swap preserves an explicit configure winner."""
    path = legacy_file(tmp_path)
    legacy = read_configuration(path)
    existing = setup_view("codex")
    store_setup(path, existing)
    peer = ControlPeer(setups={"codex": existing})
    attach_control(monkeypatch, peer)
    concurrent = configured_document(path)
    actual_replace = replace_owner_private_if_current
    calls = 0

    def configure_before_replace(
        target: Path,
        expected: bytes,
        replacement: bytes,
        *,
        maximum_bytes: int,
    ) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert write_owner_private(path, concurrent)
        return actual_replace(
            target,
            expected,
            replacement,
            maximum_bytes=maximum_bytes,
        )

    monkeypatch.setattr(
        server, "replace_owner_private_if_current", configure_before_replace
    )
    with pytest.raises(server.StartupError):
        server.upgrade_legacy_configuration(path, legacy)

    assert calls == 1
    assert peer.events == ["status", "admission"]
    assert path.read_bytes() == concurrent


def test_exact_original_bytes_must_represent_the_callers_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale parsed generation is refused before a service is contacted."""
    path = legacy_file(tmp_path)
    stale = read_configuration(path)
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["principal_id"] = "legacy-concurrent-user"
    assert write_owner_private(
        path, (json.dumps(changed, sort_keys=True) + "\n").encode("utf-8")
    )
    called = False

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("stale configuration reached the service")

    monkeypatch.setattr(server, "connect_managed_local", unexpected)
    with pytest.raises(server.StartupError):
        server.upgrade_legacy_configuration(path, stale)

    assert called is False
    assert read_configuration(path).principal_id == "legacy-concurrent-user"


def test_a_present_but_wrong_bearer_refuses_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Store readability is not evidence that the service accepts its credential."""
    path = legacy_file(tmp_path)
    original = path.read_bytes()
    existing = setup_view("codex")
    reference = store_setup(path, existing)
    InstalledCredentialStore(path.parent / "installation").store(
        reference, Credential("omcp_live_ffeeddccbbaa99887766554433221100")
    )
    peer = ControlPeer(setups={"codex": existing})
    attach_control(monkeypatch, peer)

    with pytest.raises(server.StartupError):
        server.upgrade_legacy_configuration(path, read_configuration(path))

    assert peer.events == ["status", "admission"]
    assert path.read_bytes() == original
    assert read_configuration(path).credential_reference is None


def test_revocation_after_publication_restores_the_exact_legacy_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second live proof compensates if authority changes at publication."""
    path = legacy_file(tmp_path, mutation_enabled=True)
    original = path.read_bytes()
    existing = setup_view("codex")
    store_setup(path, existing)
    peer = ControlPeer(setups={"codex": existing})
    attach_control(monkeypatch, peer)
    calls = 0

    def revoked_after_first_proof(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            peer.events.append("admission")
            raise ClientError("test credential was revoked")
        return peer.admission(*args, **kwargs)

    monkeypatch.setattr(server, "mcp_authoring_admission", revoked_after_first_proof)

    with pytest.raises(server.StartupError):
        server.upgrade_legacy_configuration(path, read_configuration(path))

    assert peer.events == ["status", "admission", "admission"]
    assert path.read_bytes() == original
    assert read_configuration(path).credential_reference is None


def test_failed_rollback_is_reported_as_unrecovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_file(tmp_path, mutation_enabled=True)
    existing = setup_view("codex")
    store_setup(path, existing)
    peer = ControlPeer(setups={"codex": existing})
    attach_control(monkeypatch, peer)
    admissions = 0

    def revoked_after_first_proof(*args: Any, **kwargs: Any) -> Any:
        nonlocal admissions
        admissions += 1
        if admissions == 2:
            peer.events.append("admission")
            raise ClientError("test credential was revoked")
        return peer.admission(*args, **kwargs)

    actual_replace = replace_owner_private_if_current
    replacements = 0

    def fail_compensation(
        target: Path,
        expected: bytes,
        replacement: bytes,
        *,
        maximum_bytes: int,
    ) -> str:
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            return "unavailable"
        return actual_replace(
            target,
            expected,
            replacement,
            maximum_bytes=maximum_bytes,
        )

    monkeypatch.setattr(server, "mcp_authoring_admission", revoked_after_first_proof)
    monkeypatch.setattr(
        server, "replace_owner_private_if_current", fail_compensation
    )

    with pytest.raises(server.StartupError) as refused:
        server.upgrade_legacy_configuration(path, read_configuration(path))

    assert "could not be restored" in str(refused.value)
    assert replacements == 2
    assert read_configuration(path).credential_reference is not None


def test_rollback_does_not_overwrite_a_concurrent_configure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_file(tmp_path, mutation_enabled=True)
    existing = setup_view("codex")
    store_setup(path, existing)
    peer = ControlPeer(setups={"codex": existing})
    attach_control(monkeypatch, peer)
    concurrent = configured_document(path, suffix="newer")
    admissions = 0

    def configure_then_revoke(*args: Any, **kwargs: Any) -> Any:
        nonlocal admissions
        admissions += 1
        if admissions == 2:
            assert write_owner_private(path, concurrent)
            peer.events.append("admission")
            raise ClientError("test credential was revoked")
        return peer.admission(*args, **kwargs)

    monkeypatch.setattr(server, "mcp_authoring_admission", configure_then_revoke)
    with pytest.raises(server.StartupError) as refused:
        server.upgrade_legacy_configuration(path, read_configuration(path))

    assert "could not be restored" in str(refused.value)
    assert path.read_bytes() == concurrent


def test_readback_must_match_the_whole_published_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-reference replacement cannot widen or narrow another field."""
    path = legacy_file(tmp_path, mutation_enabled=True)
    original = path.read_bytes()
    legacy = read_configuration(path)
    existing = setup_view("claude-code")
    reference = store_setup(path, existing)
    peer = ControlPeer(setups={"claude-code": existing})
    attach_control(monkeypatch, peer)
    actual_read = server.read_configuration

    def tampered_read(target: Path) -> McpConfiguration:
        configured = actual_read(target)
        return replace(configured, allowed_purposes=("workspace_inspection",))

    monkeypatch.setattr(server, "read_configuration", tampered_read)
    with pytest.raises(server.StartupError):
        server.upgrade_legacy_configuration(path, legacy)

    assert peer.setups == {"claude-code": existing}
    assert InstalledCredentialStore(path.parent / "installation").health(reference) == (
        "present"
    )
    assert path.read_bytes() == original


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
