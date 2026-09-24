"""Gate B: the durable, service-owned registry of dedicated MCP principals."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.service.installed_mcp import (
    AUTHORING_POLICY,
    RESTRICTED_POLICY,
    InstalledMcpAdministrationError,
    InstalledMcpAuthenticationError,
    InstalledMcpAuthority,
    InstalledMcpSecret,
    profile_policy,
)
from omnivia_core_runtime.service.mutation import INSTALLATION_ADMINISTRATOR_ROLE
from omnivia_core_runtime.storage.installation_store import (
    InstallationAuthority,
    InstallationAuthorityError,
    InstallationBusy,
    InstallationStore,
    InstallationStoreError,
    InstalledMcpSetup,
    McpGrant,
    McpGrantKind,
    McpHost,
    McpProfile,
    McpSetupStatus,
    NewInstallationAllocation,
    NewMcpSetup,
    mcp_credential_digest,
    open_installation_store,
)

INSTALLATION_ID = "inst-installed-mcp"


class TickClock:
    def __init__(self, start: int = 1_800_000_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1
        return self.value


def administrator(
    *, roles: frozenset[str] = frozenset({INSTALLATION_ADMINISTRATOR_ROLE})
) -> AuthenticatedSession:
    return AuthenticatedSession(
        principal_id="local-owner",
        roles=roles,
        installations=frozenset({INSTALLATION_ID}),
    )


def register_workspace(store: InstallationStore, suffix: str) -> str:
    """Put one workspace in the authorised inventory the ordinary way.

    Through the real allocation and settlement path rather than by inserting a row:
    the inventory is evidence that this installation authorised a workspace, and a
    test that forged one would be testing a state the service cannot reach.
    """
    workspace_id = f"ws-{suffix}"
    minted = NewInstallationAllocation(
        audit_ref=f"audit-{suffix}",
        claim_id=f"claim-{suffix}",
        allocation_id=f"allocation-{suffix}",
        target_workspace_id=workspace_id,
        target_path=(store.installation_root / "workspaces" / workspace_id).resolve(),
    )
    store.claim_allocation(
        store.authority,
        principal_id="local-owner",
        operation="workspace.create",
        purpose="workspace_provisioning",
        idempotency_key=f"key-{suffix}",
        request_digest="sha256:" + hashlib.sha256(suffix.encode()).hexdigest(),
        identity_factory=lambda: minted,
    )
    store.settle_allocation_success(
        store.authority,
        allocation_id=minted.allocation_id,
        workspace_label=None,
        outcome_id=f"outcome-{suffix}",
        outcome_json="{}",
        outcome_digest="sha256:" + hashlib.sha256(b"outcome").hexdigest(),
        execution_id=f"execution-{suffix}",
        grant_id=f"grant-{suffix}",
        required_role=INSTALLATION_ADMINISTRATOR_ROLE,
        settlement_guard=lambda: None,
    )
    return workspace_id


@contextmanager
def installation(
    tmp_path: Path, *, workspaces: Sequence[str] = ("one",)
) -> Iterator[tuple[InstallationStore, InstalledMcpAuthority]]:
    store = open_installation_store(
        (tmp_path / "installation").resolve(),
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: INSTALLATION_ID,
    )
    try:
        for suffix in workspaces:
            register_workspace(store, suffix)
        yield store, InstalledMcpAuthority(store)
    finally:
        store.close()


def kinds(policy: Sequence[McpGrant], kind: McpGrantKind) -> set[str]:
    return {grant.value for grant in policy if grant.kind is kind}


# --- the exact policies -------------------------------------------------------


def test_restricted_policy_is_exactly_the_manifest_read_surface() -> None:
    assert kinds(RESTRICTED_POLICY, McpGrantKind.OPERATION) == {
        "workspace.inspect",
        "evidence.search",
        "knowledge.search",
        "memory.search",
        "graph.traverse",
        "context_pack.build",
        "decision.evaluate",
        "decision.record.get",
        "decision.record.list",
        "decision.status",
    }
    assert kinds(RESTRICTED_POLICY, McpGrantKind.SCOPE) == {
        "workspace:read",
        "memory:read",
        "graph:read",
        "decision:read",
        "decision:invoke",
    }
    assert kinds(RESTRICTED_POLICY, McpGrantKind.PURPOSE) == {
        "workspace_inspection",
        "knowledge_retrieval",
        "decision_evaluation",
        "decision_record",
        "decision_status",
    }
    assert {
        (grant.value, grant.version)
        for grant in RESTRICTED_POLICY
        if grant.kind is McpGrantKind.CAPABILITY
    } == {
        ("workspace.read", "1.0"),
        ("evidence.read", "1.0"),
        ("knowledge.read", "1.0"),
        ("memory.read", "1.0"),
        ("graph.read", "1.0"),
        ("context_pack.build", "1.0"),
        ("decision.read", "1.0"),
        ("decision.invoke", "1.0"),
    }
    # No role at all. A restricted principal holds no operation a role admits, and
    # the one this file's authoring profile grants is what lets a mutation through.
    assert kinds(RESTRICTED_POLICY, McpGrantKind.ROLE) == set()


def test_authoring_policy_is_the_read_surface_plus_exactly_the_five() -> None:
    added = set(AUTHORING_POLICY) - set(RESTRICTED_POLICY)
    assert set(RESTRICTED_POLICY) < set(AUTHORING_POLICY)
    # R004 section 9.1's "workspace contributor authority sufficient for
    # `memory:write`", and exactly that: never the reviewer role that admits
    # governed transitions, never the administrator role that administers this
    # installation.
    assert kinds(added, McpGrantKind.ROLE) == {"workspace_contributor"}
    assert kinds(added, McpGrantKind.OPERATION) == {
        "memory.create",
        "evidence.capture",
        "import.start",
        "job.get",
        "job.events",
    }
    assert kinds(added, McpGrantKind.SCOPE) == {"memory:write", "job:read"}
    assert kinds(added, McpGrantKind.PURPOSE) == {
        "memory_authoring",
        "content_ingestion",
        "job_observation",
    }
    assert {
        (grant.value, grant.version)
        for grant in added
        if grant.kind is McpGrantKind.CAPABILITY
    } == {
        ("memory.write", "1.0"),
        ("evidence.write", "1.0"),
        ("ingestion.import", "1.0"),
        ("job.read", "1.0"),
    }


def test_no_policy_states_a_wildcard() -> None:
    for grant in (*RESTRICTED_POLICY, *AUTHORING_POLICY):
        for stated in (grant.value, grant.version or ""):
            assert "*" not in stated
            assert "?" not in stated
            assert stated.strip() == stated


# --- provisioning -------------------------------------------------------------


def test_configure_mints_a_dedicated_principal_and_a_redacted_status(
    tmp_path: Path,
) -> None:
    with installation(tmp_path) as (store, authority):
        provisioning = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        setup = provisioning.setup
        assert provisioning.rotated is True
        assert setup.host is McpHost.CLAUDE_CODE
        assert setup.workspace_id == "ws-one"
        assert setup.status is McpSetupStatus.ACTIVE
        assert setup.authoring_intent is False
        assert setup.setup_generation == 1
        assert setup.revoked_at_us is None

        # Dedicated, and provably not one of the identities R004 9.1 forbids.
        assert setup.principal_id.startswith("mcp-")
        assert setup.principal_id not in {
            "local-owner",
            "installation-service",
            store.authority.owner_instance_id,
        }
        assert setup.credential_reference.startswith("omcp-")

        # The status value has no field that could carry credential material.
        rendered = repr(setup)
        assert provisioning.secret is not None
        assert provisioning.secret.reveal() not in rendered
        assert "salt" not in rendered and "digest" not in rendered


def test_the_secret_is_redacted_in_repr_and_str() -> None:
    secret = InstalledMcpSecret("s3cr3t-material")
    assert "s3cr3t-material" not in repr(secret)
    assert "s3cr3t-material" not in str(secret)
    assert "s3cr3t-material" not in f"{secret}"
    assert secret.reveal() == "s3cr3t-material"


def test_configure_refuses_a_workspace_this_installation_never_authorised(
    tmp_path: Path,
) -> None:
    with (
        installation(tmp_path) as (_, authority),
        pytest.raises(InstallationStoreError) as refusal,
    ):
        authority.configure(
            administrator(),
            host=McpHost.CODEX,
            workspace_id="ws-elsewhere",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
    assert "authorised" in str(refusal.value)


def test_configure_refuses_authoring_without_matching_intent(tmp_path: Path) -> None:
    with installation(tmp_path) as (_, authority):
        for profile, intent in (
            (McpProfile.AUTHORING, False),
            (McpProfile.RESTRICTED, True),
        ):
            with pytest.raises(InstallationStoreError):
                authority.configure(
                    administrator(),
                    host=McpHost.CODEX,
                    workspace_id="ws-one",
                    profile=profile,
                    authoring_intent=intent,
                )
        assert authority.status(administrator()) == ()


def test_repeating_the_live_configuration_does_not_rotate(tmp_path: Path) -> None:
    with installation(tmp_path) as (_, authority):
        first = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.AUTHORING,
            authoring_intent=True,
        )
        assert first.secret is not None
        again = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.AUTHORING,
            authoring_intent=True,
        )
        assert again.rotated is False
        assert again.secret is None
        assert again.setup == first.setup
        # The credential already in the host's private configuration still works.
        assert (
            authority.authenticate(first.secret.reveal()).setup.principal_id
            == first.setup.principal_id
        )


@pytest.mark.parametrize(
    ("workspace_id", "profile", "intent"),
    [
        ("ws-one", McpProfile.AUTHORING, True),
        ("ws-two", McpProfile.RESTRICTED, False),
    ],
)
def test_changed_configuration_rotates_and_invalidates_the_old_credential(
    tmp_path: Path, workspace_id: str, profile: McpProfile, intent: bool
) -> None:
    with installation(tmp_path, workspaces=("one", "two")) as (_, authority):
        first = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        assert first.secret is not None
        rotated = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id=workspace_id,
            profile=profile,
            authoring_intent=intent,
        )
        assert rotated.rotated is True
        assert rotated.secret is not None
        assert rotated.setup.setup_generation == 2
        assert rotated.setup.setup_id == first.setup.setup_id
        assert rotated.setup.principal_id != first.setup.principal_id
        assert rotated.setup.credential_reference != first.setup.credential_reference

        with pytest.raises(InstalledMcpAuthenticationError):
            authority.authenticate(first.secret.reveal())
        assert (
            authority.authenticate(rotated.secret.reveal()).setup.principal_id
            == rotated.setup.principal_id
        )


def test_each_host_gets_its_own_principal_and_credential(tmp_path: Path) -> None:
    with installation(tmp_path, workspaces=("one", "two")) as (_, authority):
        claude = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        codex = authority.configure(
            administrator(),
            host=McpHost.CODEX,
            workspace_id="ws-two",
            profile=McpProfile.AUTHORING,
            authoring_intent=True,
        )
        assert claude.setup.principal_id != codex.setup.principal_id
        assert claude.secret is not None and codex.secret is not None
        assert claude.secret.reveal() != codex.secret.reveal()
        assert authority.authenticate(claude.secret.reveal()).setup.host is (
            McpHost.CLAUDE_CODE
        )
        assert authority.authenticate(codex.secret.reveal()).setup.host is McpHost.CODEX
        assert {setup.host for setup in authority.status(administrator())} == {
            McpHost.CLAUDE_CODE,
            McpHost.CODEX,
        }


# --- what the durable rows actually say ---------------------------------------


def test_stored_rights_are_exactly_the_profile_and_never_a_wildcard(
    tmp_path: Path,
) -> None:
    with installation(tmp_path) as (store, authority):
        provisioning = authority.configure(
            administrator(),
            host=McpHost.CODEX,
            workspace_id="ws-one",
            profile=McpProfile.AUTHORING,
            authoring_intent=True,
        )
        stored = store.mcp_grants(
            provisioning.setup.setup_id, provisioning.setup.setup_generation
        )
        assert stored == profile_policy(McpProfile.AUTHORING)
        assert all("*" not in grant.value for grant in stored)


def test_rotation_leaves_the_previous_generation_behind_as_evidence(
    tmp_path: Path,
) -> None:
    with installation(tmp_path) as (store, authority):
        first = authority.configure(
            administrator(),
            host=McpHost.CODEX,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        authority.configure(
            administrator(),
            host=McpHost.CODEX,
            workspace_id="ws-one",
            profile=McpProfile.AUTHORING,
            authoring_intent=True,
        )
        setup_id = first.setup.setup_id
        assert store.mcp_grants(setup_id, 1) == profile_policy(McpProfile.RESTRICTED)
        assert store.mcp_grants(setup_id, 2) == profile_policy(McpProfile.AUTHORING)


def test_lifecycle_evidence_is_recorded_without_credential_material(
    tmp_path: Path,
) -> None:
    with installation(tmp_path) as (store, authority):
        provisioning = authority.configure(
            administrator(),
            host=McpHost.CODEX,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        authority.revoke(administrator(), host=McpHost.CODEX)
        database = store.database_path
        secret = provisioning.secret
        assert secret is not None
    connection = sqlite3.connect(database)
    try:
        events = connection.execute(
            "SELECT operation, purpose, principal_id, outcome_class "
            "FROM omnivia_installation_audit_events "
            "WHERE operation LIKE 'mcp.setup.%' ORDER BY recorded_at_us"
        ).fetchall()
        blob = database.read_bytes()
    finally:
        connection.close()
    assert [(row[0], row[3]) for row in events] == [
        ("mcp.setup.configure", "succeeded"),
        ("mcp.setup.revoke", "succeeded"),
    ]
    assert {row[1] for row in events} == {"installed_mcp_administration"}
    assert {row[2] for row in events} == {provisioning.setup.principal_id}
    # The catalogue file itself holds no plaintext bearer, anywhere.
    assert secret.reveal().encode() not in blob


# --- authentication and admission ---------------------------------------------


def test_authentication_yields_exactly_the_durable_policy(tmp_path: Path) -> None:
    with installation(tmp_path) as (_, authority):
        provisioning = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.AUTHORING,
            authoring_intent=True,
        )
        assert provisioning.secret is not None
        principal = authority.authenticate(provisioning.secret.reveal())
        session = principal.session
        assert session.principal_id == provisioning.setup.principal_id
        assert session.workspaces == frozenset({"ws-one"})
        assert session.operations == kinds(AUTHORING_POLICY, McpGrantKind.OPERATION)
        assert session.scopes == kinds(AUTHORING_POLICY, McpGrantKind.SCOPE)
        assert session.purposes == kinds(AUTHORING_POLICY, McpGrantKind.PURPOSE)
        assert {(ref.id, ref.version) for ref in session.capabilities} == {
            (grant.value, grant.version)
            for grant in AUTHORING_POLICY
            if grant.kind is McpGrantKind.CAPABILITY
        }
        # Exactly the stored role rows, which for authoring is the one bounded
        # contributor role, and no installation authority at all: a dedicated MCP
        # principal administers nothing and reaches no installation-scoped
        # operation.
        assert session.roles == kinds(AUTHORING_POLICY, McpGrantKind.ROLE)
        assert session.roles == frozenset({"workspace_contributor"})
        assert session.installations == frozenset()


@pytest.mark.parametrize("presented", ["", "not-the-secret", "x" * 43])
def test_a_wrong_credential_is_refused_with_one_fixed_message(
    tmp_path: Path, presented: str
) -> None:
    with installation(tmp_path) as (_, authority):
        provisioning = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        assert provisioning.secret is not None
        with pytest.raises(InstalledMcpAuthenticationError) as refusal:
            authority.authenticate(presented)
    # One frozen sentence, whatever was presented: it names no credential, no
    # principal, no reference, and not which of the ways to be wrong this was.
    assert str(refusal.value) == (
        "the presented credential does not resolve to live installed MCP authority"
    )
    assert provisioning.setup.principal_id not in str(refusal.value)


def test_authenticating_against_an_unconfigured_installation_is_refused(
    tmp_path: Path,
) -> None:
    with (
        installation(tmp_path) as (_, authority),
        pytest.raises(InstalledMcpAuthenticationError),
    ):
        authority.authenticate("anything-at-all")


def test_a_restricted_principal_reads_but_is_not_admitted_to_authoring(
    tmp_path: Path,
) -> None:
    with installation(tmp_path) as (_, authority):
        provisioning = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        assert provisioning.secret is not None
        principal = authority.authenticate(provisioning.secret.reveal())
        assert "memory.search" in principal.session.operations
        assert "memory.create" not in principal.session.operations
        # And no role, so the mutation coordinator refuses it a second way: there
        # is no stored row a contributor grant could be reconstructed from.
        assert principal.session.roles == frozenset()
        assert (
            authority.admits_authoring(provisioning.setup.principal_id, "ws-one")
            is False
        )


def test_a_role_survives_exactly_as_long_as_the_grant_that_states_it(
    tmp_path: Path,
) -> None:
    """Rotation and revocation drop the role with every other right, immediately.

    Three resolutions of three credentials against one setup. The role is not a
    property of the principal, of the host or of anything the caller keeps; it is
    the generation's stored rows, so narrowing the profile takes it away and
    revoking takes the whole session away.
    """
    with installation(tmp_path) as (_, authority):
        authoring = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.AUTHORING,
            authoring_intent=True,
        )
        assert authoring.secret is not None
        assert authority.authenticate(authoring.secret.reveal()).session.roles == (
            frozenset({"workspace_contributor"})
        )

        narrowed = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        assert narrowed.secret is not None
        assert authority.authenticate(narrowed.secret.reveal()).session.roles == (
            frozenset()
        )
        # The credential the role was issued against no longer resolves at all.
        with pytest.raises(InstalledMcpAuthenticationError):
            authority.authenticate(authoring.secret.reveal())

        authority.revoke(administrator(), host=McpHost.CLAUDE_CODE)
        with pytest.raises(InstalledMcpAuthenticationError):
            authority.authenticate(narrowed.secret.reveal())


def test_authoring_admission_requires_this_principal_and_this_workspace(
    tmp_path: Path,
) -> None:
    with installation(tmp_path, workspaces=("one", "two")) as (_, authority):
        provisioning = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.AUTHORING,
            authoring_intent=True,
        )
        principal_id = provisioning.setup.principal_id
        assert authority.admits_authoring(principal_id, "ws-one") is True
        assert authority.admits_authoring(principal_id, "ws-two") is False
        assert authority.admits_authoring("mcp-somebody-else", "ws-one") is False
        assert authority.admits_authoring("local-owner", "ws-one") is False


# --- revocation ---------------------------------------------------------------


def test_revoke_invalidates_immediately_and_is_idempotent(tmp_path: Path) -> None:
    with installation(tmp_path) as (store, authority):
        provisioning = authority.configure(
            administrator(),
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.AUTHORING,
            authoring_intent=True,
        )
        assert provisioning.secret is not None
        secret = provisioning.secret.reveal()
        assert authority.authenticate(secret).setup.setup_generation == 1

        revoked = authority.revoke(administrator(), host=McpHost.CLAUDE_CODE)
        assert revoked is not None
        assert revoked.status is McpSetupStatus.REVOKED
        assert revoked.revoked_at_us is not None
        assert revoked.setup_generation == 2

        with pytest.raises(InstalledMcpAuthenticationError):
            authority.authenticate(secret)
        assert (
            authority.admits_authoring(provisioning.setup.principal_id, "ws-one")
            is False
        )
        # The revoked generation holds no rights at all, so nothing can resolve to
        # a policy even if a later reader forgot the status.
        assert store.mcp_grants(revoked.setup_id, revoked.setup_generation) == ()

        again = authority.revoke(administrator(), host=McpHost.CLAUDE_CODE)
        assert again is not None
        assert again.setup_generation == 2
        assert again.revoked_at_us == revoked.revoked_at_us


def test_revoking_an_unconfigured_host_changes_nothing(tmp_path: Path) -> None:
    with installation(tmp_path) as (_, authority):
        assert authority.revoke(administrator(), host=McpHost.CODEX) is None


def test_a_revoked_host_can_be_configured_again(tmp_path: Path) -> None:
    with installation(tmp_path) as (_, authority):
        authority.configure(
            administrator(),
            host=McpHost.CODEX,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        authority.revoke(administrator(), host=McpHost.CODEX)
        restored = authority.configure(
            administrator(),
            host=McpHost.CODEX,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        assert restored.rotated is True
        assert restored.secret is not None
        assert restored.setup.status is McpSetupStatus.ACTIVE
        assert restored.setup.revoked_at_us is None
        assert restored.setup.setup_generation == 3
        assert (
            authority.authenticate(restored.secret.reveal()).setup.setup_generation == 3
        )


# --- who may administer -------------------------------------------------------


@pytest.mark.parametrize(
    "session",
    [
        AuthenticatedSession(principal_id="local-owner"),
        AuthenticatedSession(
            principal_id="local-owner", installations=frozenset({INSTALLATION_ID})
        ),
        AuthenticatedSession(
            principal_id="local-owner",
            roles=frozenset({INSTALLATION_ADMINISTRATOR_ROLE}),
        ),
        AuthenticatedSession(
            principal_id="local-owner",
            roles=frozenset({INSTALLATION_ADMINISTRATOR_ROLE}),
            installations=frozenset({"inst-somewhere-else"}),
        ),
    ],
)
def test_administration_requires_the_installation_administrator_role(
    tmp_path: Path, session: AuthenticatedSession
) -> None:
    with installation(tmp_path) as (_, authority):
        for call in (
            lambda: authority.configure(
                session,
                host=McpHost.CODEX,
                workspace_id="ws-one",
                profile=McpProfile.RESTRICTED,
                authoring_intent=False,
            ),
            lambda: authority.revoke(session, host=McpHost.CODEX),
            lambda: authority.status(session),
        ):
            with pytest.raises(InstalledMcpAdministrationError):
                call()
        assert authority.status(administrator()) == ()


# --- fencing, faults and exclusivity ------------------------------------------


def test_a_stale_installation_authority_cannot_write(tmp_path: Path) -> None:
    with installation(tmp_path) as (store, _):
        current = store.authority
        stale = InstallationAuthority(
            installation_id=current.installation_id,
            owner_instance_id=current.owner_instance_id,
            fencing_generation=current.fencing_generation - 1,
        )
        with pytest.raises(InstallationAuthorityError):
            store.configure_mcp_setup(
                stale,
                host=McpHost.CODEX,
                workspace_id="ws-one",
                profile=McpProfile.RESTRICTED,
                authoring_intent=False,
                grants=RESTRICTED_POLICY,
                identity_factory=lambda: minted_setup("stale"),
            )
        assert store.mcp_setups() == ()


def test_only_one_process_can_own_the_catalogue(tmp_path: Path) -> None:
    with installation(tmp_path) as (store, _), pytest.raises(InstallationBusy):
        open_installation_store(
            store.installation_root,
            owner_instance_id="a-second-service",
            clock_us=TickClock(),
        )


def minted_setup(
    suffix: str, *, grants: Sequence[McpGrant] = RESTRICTED_POLICY
) -> NewMcpSetup:
    salt = "ab" * 16
    return NewMcpSetup(
        audit_ref=f"audit-mcp-{suffix}",
        setup_id=f"mcp-setup-{suffix}",
        principal_id=f"mcp-codex-{suffix}",
        credential_reference=f"omcp-{suffix}",
        credential_salt=salt,
        credential_digest=mcp_credential_digest(salt, f"secret-{suffix}"),
        grants=tuple(grants),
    )


def test_a_fault_at_a_write_boundary_persists_nothing(tmp_path: Path) -> None:
    """A colliding audit reference rolls the whole configure back, not part of it."""
    with installation(tmp_path, workspaces=("one", "two")) as (store, _):
        store.configure_mcp_setup(
            store.authority,
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
            grants=RESTRICTED_POLICY,
            identity_factory=lambda: minted_setup("first"),
        )
        with pytest.raises(InstallationStoreError):
            store.configure_mcp_setup(
                store.authority,
                host=McpHost.CODEX,
                workspace_id="ws-two",
                profile=McpProfile.RESTRICTED,
                authoring_intent=False,
                grants=RESTRICTED_POLICY,
                identity_factory=lambda: minted_setup("first"),
            )
        setups = store.mcp_setups()
        assert [setup.host for setup in setups] == [McpHost.CLAUDE_CODE]
        assert setups[0].workspace_id == "ws-one"
        assert store.mcp_grants("mcp-setup-first", 1) == profile_policy(
            McpProfile.RESTRICTED
        )


@pytest.mark.parametrize(
    "grants",
    [
        (),
        (McpGrant(McpGrantKind.OPERATION, "memory.*"),),
        (McpGrant(McpGrantKind.SCOPE, "memory:?ead"),),
        (McpGrant(McpGrantKind.OPERATION, "memory.search", "1.0"),),
        (McpGrant(McpGrantKind.CAPABILITY, "memory.read"),),
        (McpGrant(McpGrantKind.CAPABILITY, "memory.read", "*"),),
        (
            McpGrant(McpGrantKind.OPERATION, "memory.search"),
            McpGrant(McpGrantKind.OPERATION, "memory.search"),
        ),
    ],
)
def test_the_store_refuses_rights_that_are_not_exact(
    tmp_path: Path, grants: Sequence[McpGrant]
) -> None:
    with installation(tmp_path) as (store, _):
        with pytest.raises(InstallationStoreError):
            store.configure_mcp_setup(
                store.authority,
                host=McpHost.CODEX,
                workspace_id="ws-one",
                profile=McpProfile.RESTRICTED,
                authoring_intent=False,
                grants=grants,
                identity_factory=lambda: minted_setup("inexact", grants=grants),
            )
        assert store.mcp_setups() == ()


def test_the_schema_refuses_a_principal_that_is_not_dedicated(tmp_path: Path) -> None:
    """The `mcp-` prefix is a constraint, not a convention the service remembers."""
    with installation(tmp_path) as (store, _):
        borrowed = NewMcpSetup(
            audit_ref="audit-mcp-borrowed",
            setup_id="mcp-setup-borrowed",
            principal_id="installation-service",
            credential_reference="omcp-borrowed",
            credential_salt="cd" * 16,
            credential_digest=mcp_credential_digest("cd" * 16, "secret"),
            grants=RESTRICTED_POLICY,
        )
        with pytest.raises(InstallationStoreError):
            store.configure_mcp_setup(
                store.authority,
                host=McpHost.CODEX,
                workspace_id="ws-one",
                profile=McpProfile.RESTRICTED,
                authoring_intent=False,
                grants=RESTRICTED_POLICY,
                identity_factory=lambda: borrowed,
            )
        assert store.mcp_setups() == ()


def test_status_is_the_durable_state_and_only_the_durable_state(
    tmp_path: Path,
) -> None:
    with installation(tmp_path) as (store, authority):
        assert authority.status(administrator()) == ()
        provisioning = authority.configure(
            administrator(),
            host=McpHost.CODEX,
            workspace_id="ws-one",
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        assert authority.status(administrator(), host=McpHost.CODEX) == (
            provisioning.setup,
        )
        assert authority.status(administrator(), host=McpHost.CLAUDE_CODE) == ()
        assert store.mcp_setup(McpHost.CODEX) == provisioning.setup
        assert isinstance(provisioning.setup, InstalledMcpSetup)
