"""The installed-MCP authority seam (Gate B, R004 sections 9.1-9.3).

One dedicated principal per supported host, bound to one workspace, holding
exactly the rights its profile implies, and provable from durable state on every
single call. This module is what turns that sentence into code; the catalogue
tables behind it are the only authority, and the live Core service is the only
process that opens them.

**The rights are derived, not transcribed.** A profile here is a list of
`(operation, purpose)` pairs -- the MCP exposure manifest's own allow-list, which
is the one fact this runtime cannot read for itself without importing the MCP
package it must stay independent of. Every *other* right comes off the frozen
operation catalogue: the scopes an operation requires and the capability it needs
at which minimum version are `get_operation_metadata`'s answers, not this
module's opinion. So a renamed scope or a raised capability floor moves here on
the day the catalogue moves, and no copy of it can go stale in between.

**Least privilege is the shape of the data, not a rule about it.** The rights are
stored one row per right and read back the same way; there is no pattern, no
prefix, no "all of namespace x", and the schema refuses a `*` or a `?` in a
granted value outright. A `restricted` principal holds six operations, three
scopes, six capabilities, two purposes and no role at all, and an `authoring` one
holds those plus exactly five operations, two scopes, four capabilities, three
purposes and one role.

**The one role is a grant, not an inference.** R004 section 9.1 requires an
authoring setup to hold "workspace contributor authority sufficient for
`memory:write`", and the mutation coordinator asks for that by role name, so
without it an authoring principal authenticates holding every mutation operation
its profile implies and is refused every one of them. It is therefore stored --
one `McpGrantKind.ROLE` row, `workspace_contributor` and nothing else -- rather
than derived at authentication time from the profile, from the public MCP
configuration file or from anything a caller says about itself. That makes it
durable state this service wrote under an administrator, revoked with every other
right the moment the setup generation advances, and it is why `restricted`
carries no role row and why no path here can produce `knowledge_reviewer` or
`installation_administrator`.

**Nothing a caller says is authority.** Configure mints the principal, the
credential reference and the secret itself, inside the write transaction, after
the store has proved the workspace is one this installation authorised. The
caller chooses a host, a workspace and a profile and nothing else. An
administrator is required, and "administrator" is the same
`INSTALLATION_ADMINISTRATOR_ROLE` the installation service already requires for
`workspace.create` -- held in a server-built `AuthenticatedSession`, which is a
value a request cannot construct.

**The secret exists once.** `configure` hands back an
:class:`InstalledMcpSecret`, whose `repr` and `str` are the word redacted, and
which the caller must ask for explicitly. Nothing else in this module, in the
store, in the schema or in a refusal message carries credential material: what is
persisted is a per-setup salt and `sha256:<salt || secret>`, and what is
persisted is all anybody reading the catalogue file gets.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1 import (
    SCOPE_KIND_WORKSPACE,
    CapabilityRef,
    get_operation_metadata,
)
from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.service.mutation import (
    INSTALLATION_ADMINISTRATOR_ROLE,
    WORKSPACE_CONTRIBUTOR_ROLE,
)
from omnivia_core_runtime.storage.installation_store import (
    InstallationStore,
    InstalledMcpSetup,
    McpGrant,
    McpGrantKind,
    McpHost,
    McpProfile,
    McpSetupStatus,
    NewMcpSetup,
    mcp_credential_digest,
)

__all__ = [
    "AUTHORING_POLICY",
    "RESTRICTED_POLICY",
    "AuthenticatedMcpPrincipal",
    "InstalledMcpAdministrationError",
    "InstalledMcpAuthenticationError",
    "InstalledMcpAuthority",
    "InstalledMcpProvisioning",
    "InstalledMcpSecret",
    "profile_policy",
]


class InstalledMcpAdministrationError(Exception):
    """An installed-MCP administration request was refused."""


class InstalledMcpAuthenticationError(Exception):
    """A presented MCP credential did not resolve to live authority."""


# --- refusal messages ---------------------------------------------------------
#
# Frozen constants rather than strings built at the raise site, for the reason
# `authorization.py` gives at length: a refusal is rendered into logs, audit
# records and wire errors, so anything interpolated into one is republished
# everywhere those go. Here the value that must never reach one is a credential.

_MESSAGE_NOT_ADMINISTRATOR: Final = (
    "installed MCP administration requires a local installation administrator"
)
_MESSAGE_NOT_AUTHENTICATED: Final = (
    "the presented credential does not resolve to live installed MCP authority"
)


# --- the two exact profiles ---------------------------------------------------
#
# The operation and the purpose are the MCP exposure manifest's (`manifest.py`,
# `MANIFEST_VERSION` 2.0) and are restated here because the runtime must not
# import the MCP package: an agent-facing allow-list is a decision that package
# owns, and a dependency in this direction would make the service unable to start
# without it. Everything else about each operation is read from the catalogue.

_RESTRICTED_OPERATIONS: Final[tuple[tuple[str, str], ...]] = (
    ("workspace.inspect", "workspace_inspection"),
    ("evidence.search", "knowledge_retrieval"),
    ("knowledge.search", "knowledge_retrieval"),
    ("memory.search", "knowledge_retrieval"),
    ("graph.traverse", "knowledge_retrieval"),
    ("context_pack.build", "knowledge_retrieval"),
    ("decision.evaluate", "decision_evaluation"),
    ("decision.record.get", "decision_record"),
    ("decision.record.list", "decision_record"),
    ("decision.status", "decision_status"),
)

_AUTHORING_ADDITIONS: Final[tuple[tuple[str, str], ...]] = (
    ("memory.create", "memory_authoring"),
    ("evidence.capture", "content_ingestion"),
    ("import.start", "content_ingestion"),
    ("job.get", "job_observation"),
    ("job.events", "job_observation"),
)


def _derive_policy(entries: tuple[tuple[str, str], ...]) -> tuple[McpGrant, ...]:
    """The exact rights one allow-list implies, in canonical order.

    Every failure below is a failure to import this module, which is the right
    severity: a profile that cannot be derived is a build whose least-privilege
    grant nobody can state, and starting anyway would mean deciding at the first
    call what nothing reviewed.

    An operation that is not workspace-scoped is refused rather than granted. A
    dedicated MCP principal holds authority over one workspace and no installation
    at all, so an installation-scoped operation in a profile would be a right this
    module has no way to bound.
    """
    operations: set[McpGrant] = set()
    scopes: set[McpGrant] = set()
    purposes: set[McpGrant] = set()
    capability_versions: dict[str, str] = {}
    for operation, purpose in entries:
        entry = get_operation_metadata(operation)
        if entry.scope.scope_kind != SCOPE_KIND_WORKSPACE:
            raise ValueError(
                f"{operation}: an installed MCP profile admits workspace-scoped "
                f"operations only, and this one is {entry.scope.scope_kind!r}"
            )
        operations.add(McpGrant(McpGrantKind.OPERATION, operation))
        purposes.add(McpGrant(McpGrantKind.PURPOSE, purpose))
        for scope in entry.scope.required_scopes:
            scopes.add(McpGrant(McpGrantKind.SCOPE, scope))
        required = entry.required_capability
        held = capability_versions.setdefault(required.id, required.minimum_version)
        if held != required.minimum_version:
            raise ValueError(
                f"{required.id}: two operations in one installed MCP profile "
                f"require different minimum versions ({held}, "
                f"{required.minimum_version}); nothing here could say which is "
                "granted"
            )
    capabilities = {
        McpGrant(McpGrantKind.CAPABILITY, identifier, version)
        for identifier, version in capability_versions.items()
    }
    return tuple(sorted(operations | scopes | purposes | capabilities))


#: The one role an authoring setup holds, and the only role any profile here may
#: grant. R004 section 9.1 asks for "workspace contributor authority sufficient for
#: `memory:write`" and for nothing above it, so this is the mutation coordinator's
#: own `workspace_contributor` -- imported rather than spelled, so a rename moves
#: the grant with the requirement -- and never `knowledge_reviewer`, which admits
#: governed transitions no MCP tool exposes, nor
#: `INSTALLATION_ADMINISTRATOR_ROLE`, which administers this catalogue.
_AUTHORING_ROLE: Final = McpGrant(McpGrantKind.ROLE, WORKSPACE_CONTRIBUTOR_ROLE)

#: The read-only grant: exactly the manifest's restricted six and what they need.
#: No role, because a restricted principal holds no operation a role would admit.
RESTRICTED_POLICY: Final[tuple[McpGrant, ...]] = _derive_policy(_RESTRICTED_OPERATIONS)

#: The authoring grant: the restricted rights, exactly the five additions, and the
#: one role those additions need. The role is added here rather than inside
#: `_derive_policy` because it is the one right the frozen operation catalogue does
#: not state -- deriving it would mean inventing a rule the catalogue has no field
#: for, and a reviewer would have no line to read it off.
AUTHORING_POLICY: Final[tuple[McpGrant, ...]] = tuple(
    sorted(
        set(_derive_policy(_RESTRICTED_OPERATIONS + _AUTHORING_ADDITIONS))
        | {_AUTHORING_ROLE}
    )
)

if not set(RESTRICTED_POLICY) < set(AUTHORING_POLICY):  # pragma: no cover
    raise ValueError(
        "the authoring profile must be the restricted profile plus additions; "
        "the two have drifted"
    )

def _roles(policy: tuple[McpGrant, ...]) -> set[McpGrant]:
    return {grant for grant in policy if grant.kind is McpGrantKind.ROLE}


if _roles(RESTRICTED_POLICY) or _roles(AUTHORING_POLICY) != {
    _AUTHORING_ROLE
}:  # pragma: no cover
    raise ValueError(
        "an installed MCP profile grants the one workspace-contributor role to "
        "authoring and no role at all to restricted; the two have drifted"
    )

_POLICIES: Final[dict[McpProfile, tuple[McpGrant, ...]]] = {
    McpProfile.RESTRICTED: RESTRICTED_POLICY,
    McpProfile.AUTHORING: AUTHORING_POLICY,
}


def profile_policy(profile: McpProfile) -> tuple[McpGrant, ...]:
    """The exact rights one profile grants. The only source of them there is."""
    return _POLICIES[profile]


class InstalledMcpSecret:
    """The plaintext bearer for one provisioning, handed over exactly once.

    A plain slots class rather than a dataclass so there is no generated `repr`
    and no generated `__eq__`: the first would print the secret wherever a value
    is logged or a container is rendered, and the second would make the secret
    comparable, which is an oracle nothing needs.

    `reveal()` is deliberately a verb. Reading the secret is a thing a caller
    does on purpose, at one point, to write it into owner-private storage -- and
    it is greppable, so where that happens stays reviewable.
    """

    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def reveal(self) -> str:
        """The bearer secret. Write it somewhere owner-private, then drop it."""
        return self._secret

    def __repr__(self) -> str:
        return "<InstalledMcpSecret redacted>"

    __str__ = __repr__


@dataclass(frozen=True)
class InstalledMcpProvisioning:
    """What one `configure` settled on.

    `secret` is present exactly when `rotated` is true. A configure that found the
    requested state already live returns the durable setup and no secret, because
    it did not mint one: the credential already in the host's private
    configuration is still the credential.
    """

    setup: InstalledMcpSetup
    rotated: bool
    secret: InstalledMcpSecret | None


@dataclass(frozen=True)
class AuthenticatedMcpPrincipal:
    """A resolved dedicated principal and the session its durable rights allow.

    `session` carries no credential, by construction: it is built from the stored
    policy rows, and the secret that produced it never reaches this value.
    """

    session: AuthenticatedSession
    setup: InstalledMcpSetup


class InstalledMcpAuthority:
    """Configure, inspect, revoke and resolve dedicated MCP principals.

    Holds the store rather than an `InstallationAuthority`, and reads
    `store.authority` at the moment of each call, so a fencing generation that
    advanced under a takeover is observed live rather than remembered -- the same
    discipline `InstallationApplicationService` follows for the same reason.

    Nothing is cached. Every status, every authentication and every authoring
    admission re-reads the catalogue, which is what makes a revocation take effect
    on the next call and on the next replay instead of when some session expires.
    """

    def __init__(self, store: InstallationStore) -> None:
        self._store = store

    def configure(
        self,
        administrator: AuthenticatedSession,
        *,
        host: McpHost,
        workspace_id: str,
        profile: McpProfile,
        authoring_intent: bool,
    ) -> InstalledMcpProvisioning:
        """Provision or re-provision `host`, minting everything that is authority.

        `authoring_intent` must be true for the authoring profile and false for
        the restricted one, and it is a separate argument rather than something
        inferred from the profile precisely so that enabling authoring is an
        explicit act recorded as one. R004 section 9.3's rule -- that a public
        `mutation_enabled: true` is a ceiling and never an authorisation -- rests
        on this being a fact only this path can write.

        Returns the secret only when something was actually rotated.
        """
        self._require_administrator(administrator)
        policy = profile_policy(profile)
        secret = ""

        def mint() -> NewMcpSetup:
            # Called by the store only once it holds the write transaction and has
            # established that the requested state is not the live one, so a
            # configure that changes nothing never generates a secret at all.
            nonlocal secret
            secret = secrets.token_urlsafe(32)
            salt = secrets.token_bytes(16).hex()
            return NewMcpSetup(
                audit_ref=f"audit-mcp-{uuid.uuid4()}",
                setup_id=f"mcp-setup-{uuid.uuid4()}",
                principal_id=f"mcp-{host.value}-{uuid.uuid4().hex}",
                credential_reference=f"omcp-{uuid.uuid4().hex}",
                credential_salt=salt,
                credential_digest=mcp_credential_digest(salt, secret),
                grants=policy,
            )

        outcome = self._store.configure_mcp_setup(
            self._store.authority,
            host=host,
            workspace_id=workspace_id,
            profile=profile,
            authoring_intent=authoring_intent,
            grants=policy,
            identity_factory=mint,
        )
        return InstalledMcpProvisioning(
            setup=outcome.setup,
            rotated=outcome.rotated,
            secret=InstalledMcpSecret(secret) if outcome.rotated else None,
        )

    def status(
        self, administrator: AuthenticatedSession, *, host: McpHost | None = None
    ) -> tuple[InstalledMcpSetup, ...]:
        """Every configured host, or one, as redacted durable state.

        `InstalledMcpSetup` has no field that could carry a secret, a salt or a
        digest, so redaction is a property of the type rather than of whoever
        formats it.
        """
        self._require_administrator(administrator)
        if host is None:
            return self._store.mcp_setups()
        found = self._store.mcp_setup(host)
        return () if found is None else (found,)

    def revoke(
        self, administrator: AuthenticatedSession, *, host: McpHost
    ) -> InstalledMcpSetup | None:
        """Revoke `host`'s authority before returning. Idempotent.

        The store overwrites the verifier with fresh material and advances the
        setup generation in the same transaction that marks the row revoked, so
        the previous bearer cannot authenticate and the previous generation's
        rights stop being anybody's policy the moment this commits.

        `None` means the host was never configured, which is already the state a
        revoke was asking for.
        """
        self._require_administrator(administrator)
        salt = secrets.token_bytes(16).hex()
        return self._store.revoke_mcp_setup(
            self._store.authority,
            host=host,
            audit_ref=f"audit-mcp-{uuid.uuid4()}",
            credential_salt=salt,
            credential_digest=mcp_credential_digest(salt, secrets.token_urlsafe(32)),
        )

    def authenticate(self, credential: str) -> AuthenticatedMcpPrincipal:
        """Resolve a presented bearer to live authority, or refuse.

        One refusal for every way this can fail -- absent, malformed, wrong,
        rotated away or revoked. Telling them apart would tell a caller whether a
        credential it holds was ever real, and there is nothing an honest client
        does differently between them.

        The resulting session grants exactly the durable rows: one workspace, the
        stored operations, scopes, purposes, capabilities and roles, and no
        installation authority at all. A restricted principal authenticates
        perfectly well and simply cannot reach a mutation, because no mutation is
        among its operations and no role row is among its rights.

        **Roles come from rows and from nowhere else.** There is no branch here
        that reads the profile, the authoring intent, the MCP configuration file or
        anything a caller presented beyond the bearer itself; a setup whose stored
        rights contain no `ROLE` row authenticates with no role, whatever else it
        says about itself. So a revocation or a rotation drops the role in the same
        statement that drops every other right -- the previous generation's rows
        stop being anybody's policy -- and a role can only ever have got here by
        `configure` writing one profile's frozen policy under an administrator.
        """
        resolved = self._store.resolve_mcp_credential(credential)
        if resolved is None:
            raise InstalledMcpAuthenticationError(_MESSAGE_NOT_AUTHENTICATED)
        setup = resolved.setup
        operations: set[str] = set()
        scopes: set[str] = set()
        purposes: set[str] = set()
        roles: set[str] = set()
        capabilities: list[CapabilityRef] = []
        for grant in resolved.grants:
            if grant.kind is McpGrantKind.OPERATION:
                operations.add(grant.value)
            elif grant.kind is McpGrantKind.SCOPE:
                scopes.add(grant.value)
            elif grant.kind is McpGrantKind.PURPOSE:
                purposes.add(grant.value)
            elif grant.kind is McpGrantKind.ROLE:
                roles.add(grant.value)
            elif grant.version is not None:
                capabilities.append(
                    CapabilityRef(id=grant.value, version=grant.version)
                )
        session = AuthenticatedSession(
            principal_id=setup.principal_id,
            roles=frozenset(roles),
            installations=frozenset(),
            workspaces=frozenset({setup.workspace_id}),
            operations=frozenset(operations),
            scopes=frozenset(scopes),
            purposes=frozenset(purposes),
            capabilities=tuple(capabilities),
        )
        return AuthenticatedMcpPrincipal(session=session, setup=setup)

    def admits_authoring(self, principal_id: str, workspace_id: str) -> bool:
        """Whether this principal may run the authoring surface in this workspace.

        Four conditions, all required and all read fresh: an active setup, this
        exact principal, this exact workspace, and the authoring profile with its
        intent recorded. A restricted principal is not admitted, a revoked one is
        not admitted, and a principal admitted for one workspace is not admitted
        for another.

        A bool, with the argument order `omnivia_core_mcp.configuration`'s
        `AuthoringAdmission` already expects, so the MCP server's startup decision
        can consult this without either side restating the other's shape. False is
        the answer to every question this cannot answer affirmatively: a lookup
        that fails has not admitted anything.
        """
        for setup in self._store.mcp_setups():
            if setup.principal_id != principal_id:
                continue
            return (
                setup.status is McpSetupStatus.ACTIVE
                and setup.workspace_id == workspace_id
                and setup.profile is McpProfile.AUTHORING
                and setup.authoring_intent
            )
        return False

    def _require_administrator(self, administrator: AuthenticatedSession) -> None:
        """Refuse anything but a server-established local installation administrator.

        `AuthenticatedSession` is the seam. It is what the *server* established
        before a request said anything, its grants are copied at construction, and
        no request can widen one -- so requiring the installation administrator
        role in one, over this installation, is an authority check rather than a
        caller's claim about itself. It is the same role
        `InstallationApplicationService` requires to create a workspace, reused
        rather than reinvented: administering this installation is one privilege.
        """
        if (
            not isinstance(administrator, AuthenticatedSession)
            or INSTALLATION_ADMINISTRATOR_ROLE not in administrator.roles
            or self._store.authority.installation_id not in administrator.installations
        ):
            raise InstalledMcpAdministrationError(_MESSAGE_NOT_ADMINISTRATOR)
