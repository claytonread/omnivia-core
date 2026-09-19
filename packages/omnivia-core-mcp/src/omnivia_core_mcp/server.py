"""The stdio MCP server for OmniVia Core (R004-05, R004-06, R004-07).

Built on the official Model Context Protocol Python SDK v2. There is no
JSON-RPC, framing, session or capability code in this package and there must not
be: R004-05 makes the official SDK the sole MCP framework dependency, and rules
out both a bespoke stack and FastMCP.

**stdout is protocol-only, and the SDK is what makes it so.** `stdio_server()`
claims fd 0 and 1, serves the wire from private duplicates and points fd 1 at
stderr for the duration, so a stray `print` in any handler -- or in a child
process that inherits the descriptors -- misses the wire instead of tearing a
frame. A service the shared client starts on this server's behalf has its output
captured there as well, so it is contained twice over. No guard of this package's
own would be better than the transport's own claim on the descriptor, and a
second one would be a second thing to get wrong.

**Everything is decided before the transport opens.** The configuration is read,
the service is connected, and the workspace is agreed before `stdio_server()` is
entered, so a refusal is a startup failure on stderr with not one byte of
protocol written -- R004-07's protocol-safe diagnostic in its strongest form --
and tools are never advertised by a server that cannot serve them.

**The call path is composed by `omnivia-core-client`, not here.**
:class:`~omnivia_core_client.ServiceClient` reads the published descriptor,
picks the transport, negotiates versions and proves the endpoint is live; this
package holds no dial loop, no descriptor read, no transport choice and no
credential source. What was a `TransportFactory` seam is gone with the direct
transport construction it existed for: a connected :class:`ConnectedSession` is
what a test substitutes now, and it substitutes the same object production uses.

**An installed managed-local server calls as its own dedicated principal.** A
configuration the installed setup path wrote carries the *name* of a credential
this installation filed in its own protected store; this package asks the shared
client's :class:`~omnivia_core_client.InstalledCredentialStore` for it by that
name, at a location the store derives from the installation root and nothing here
chooses, and wraps the connected client with
:func:`~omnivia_core_client.authenticated_client` so every application request
travels the local endpoint's authenticated control. The bearer is never held: it
is read from the store for each call, so revoking or rotating it changes the very
next call. Nothing above this sees any of it -- `ConnectedSession` and every
handler still go through `ServiceClient.call`.

**There is no unauthenticated managed-local session, and no way to ask for one.**
A managed-local configuration that names no credential is checked at the
console boundary before :func:`connect`. Migration may finish publication of
exactly one already-authorized restricted setup whose bearer is recoverable; it
never chooses a host or creates authority. Every other legacy state refuses and
requires the explicit installed configure command. A configuration naming a
credential this installation cannot produce also refuses at :func:`connect`. The local endpoint would accept
the plain application path, so neither case may fall back to a session running as
the service's own identity.

**Authority is the configuration's, never the model's.** The principal, the
workspace, the allowed purposes, the endpoint and the credential *reference* all
come from the trusted `omnivia.mcp-config.v1` document, which is read from an
explicit owner-private path before anything else happens. A tool call cannot
name any of them: :data:`RESERVED_ARGUMENTS` refuses the attempt by name and the
advertised closed schema refuses it again as an undeclared key, both before a
request exists, let alone a call.

**Which surface is exposed is settled once, before a tool is advertised.**
:func:`connect` asks
:func:`~omnivia_core_mcp.configuration.effective_profile` for the profile and
freezes it on the session, and both `tools/list` and the call path read that one
value -- so the advertised inventory and the callable inventory are the same
inventory, and neither varies with a prompt, an argument or an allowed purpose.
It asks *after* the service is connected and its descriptor agreed, and hands the
protected admission seam that connected client, so the implementation reads its
record through the authority this session already established rather than through
an installation database or a second connection of its own. The console entry
point first migrates a legacy configuration that names no credential, then
injects that seam for the resulting installed configuration -- see
:func:`upgrade_legacy_configuration` and :func:`_installed_admission`. The
admission requires the protected answer to name exactly the configured principal
and workspace. A direct call to :func:`connect` with no reference never reaches a
profile at all, and a remote one gets no admission and is `restricted` whatever
its `mutation_enabled` byte says.

**An authoring call is checked against the canonical contract before it is
sent.** The advertised wrapper is a call shape and the schema projection is a
key list; neither says what a value may be. So every operation the `authoring`
profile adds has its unwrapped input put through the *public* decoder
`omnivia_core.contracts.v1` publishes for it, and a mutation's key through
`is_idempotency_key`, before `ServiceClient.call` is reached. Nothing about
those constraints is transcribed here -- a copied bound is a bound that goes
stale -- and a refusal is fixed text that quotes none of what was sent.

**MCP does not own the lease and does not stop what it started.** Neither
appears below, and their absence is the implementation: there is no lease call,
no stop call and no shutdown hook. A service started here is an independent Core
service and outlives the stdio session, exactly as R004-07 requires. The one
thing this process does drop at shutdown is its own credential cache.

**The one process this module does own is a copy of itself.**
:func:`verify_installed_setup` qualifies a protected configuration by running
this entry point as a child and speaking MCP to it with the official SDK's own
client, because that is the only way to answer the question an installed setup
actually asks: *would a host that launched this command get this server, with
this inventory?* An in-process rehearsal cannot answer it -- it proves a session
object was built, not that a subprocess speaks the protocol -- and the child is
stopped, on success and on every failure, before the answer is returned. It is
still no launcher: the child is this interpreter running this module, its whole
command line is the configuration path, and nothing about a Core *service*
process is decided here.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TextIO

import anyio
import mcp_types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from omnivia_core_client import (
    CLIENT_API_VERSION,
    ClientError,
    CredentialCache,
    CredentialReference,
    CredentialResolver,
    Deadline,
    HttpServiceConfig,
    InstallationServiceConfig,
    InstalledCredentialStore,
    ManagedStartError,
    ServiceClient,
    authenticated_client,
    connect_managed_local,
    local_control_transport,
    mcp_authoring_admission,
    mcp_status,
    read_owner_private,
)
from omnivia_core_client.owner_private import replace_owner_private_if_current

from omnivia_core.contracts.v1 import (
    EVIDENCE_CAPTURE_MAX_CONTENT_BYTES,
    CapabilityRequirement,
    ClientIdentity,
    ContractDecodeError,
    ContractSemanticError,
    EvidenceCaptureSizeLimitError,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    codec,
    decode_evidence_capture_input,
    decode_import_start_input,
    decode_job_events_input,
    decode_job_get_input,
    decode_memory_create_input,
    get_operation_metadata,
    is_idempotency_key,
)
from omnivia_core_mcp import __version__
from omnivia_core_mcp.configuration import (
    MAXIMUM_CONFIGURATION_BYTES,
    AuthoringAdmission,
    McpConfiguration,
    McpConfigurationError,
    _parse_configuration_bytes,
    effective_profile,
    read_configuration,
)
from omnivia_core_mcp.manifest import (
    ADMITTED_MUTATIONS,
    AUTHORING_PROFILE,
    RESTRICTED_PROFILE,
    ExposedOperation,
    exposed_by_tool_name,
    exposure_manifest,
    input_schema,
    tools,
)

__all__ = [
    "CALL_TIMEOUT_SECONDS",
    "CLIENT_NAME",
    "CONNECT_TIMEOUT_SECONDS",
    "EXPECTED_TOOL_COUNT",
    "RESERVED_ARGUMENTS",
    "SERVER_NAME",
    "ConnectedSession",
    "StartupError",
    "build_server",
    "connect",
    "main",
    "serve",
    "upgrade_legacy_configuration",
    "verify_installed_setup",
]

#: The name this server advertises to a host. Stable MCP-facing vocabulary.
SERVER_NAME: Final = "omnivia-core"

#: This adapter's self-declared identity on every request. Diagnostic only, never
#: an authorization input, and distinct from the CLI's so a service log can tell
#: an agent's call from a human's.
CLIENT_NAME: Final = "omnivia-core-mcp"

#: Budget for one application call. A call budget, not a startup budget: the
#: service is already ready by the time any of these are made.
CALL_TIMEOUT_SECONDS: Final = 30.0

#: Budget for one connect, including the live probe behind it.
CONNECT_TIMEOUT_SECONDS: Final = 30.0

#: One strict-base64 spelling just beyond the largest encoded capture.  Oversized
#: caller material is replaced with this bounded sentinel before OVC1 serialization,
#: so Core still owns the canonical ``size_limit_exceeded`` response without this
#: adapter allocating in proportion to an attacker-controlled input.
_CAPTURE_MAX_BASE64_LENGTH: Final = 4 * (
    (EVIDENCE_CAPTURE_MAX_CONTENT_BYTES + 2) // 3
)
_CAPTURE_OVERSIZE_SENTINEL_LENGTH: Final = _CAPTURE_MAX_BASE64_LENGTH + 4

#: Budget for reaching a managed-local service, which is a different question
#: from reaching one that is already up: it covers the probe, a service being
#: started, whatever recovery or migration that service does before it reports
#: ready, and the probe again. Thirty seconds is a connect budget and would
#: expire in the middle of a cold start, leaving a service running that this
#: process has just refused to talk to.
MANAGED_START_TIMEOUT_SECONDS: Final = 180.0

#: Argument names a tool call may never carry, whatever a schema says.
#:
#: Every one of these is authority the trusted configuration fixes: who the
#: caller is, which workspace it reaches, what it may claim to be doing, what it
#: has been granted, whether it may write, where the service is and which
#: credential is presented there. The advertised schemas are closed and declare
#: none of them, so this is the second of two refusals rather than the only one
#: -- and it is the one that stays true if a canonical contract ever grows a
#: field with one of these names.
RESERVED_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {
        "principal_id",
        "principal_claim",
        "claimed_principal_id",
        "claimed_roles",
        "workspace_id",
        "allowed_workspace_ids",
        "default_workspace_id",
        "purpose",
        "purposes",
        "allowed_purposes",
        "scopes",
        "grants",
        "granted_authority",
        "required_capabilities",
        "mutation_enabled",
        "endpoint",
        "endpoint_uri",
        "credential_reference",
        "credentials",
    }
)


#: The public canonical decoder for each operation the `authoring` profile adds.
#:
#: `omnivia_core.contracts.v1`'s own `decode_<operation>_input`, which parses the
#: wire document into the contract's type *and* runs its semantic validator --
#: the same pair the service runs on the way in. Named here rather than
#: reimplemented, and reached through the public package rather than through any
#: module inside it, so this adapter carries no bound, no pattern, no allowlist
#: and no cross-field rule of its own: a contract that tightens one tightens this
#: call path in the same commit, and one that relaxes one does not leave a stale
#: copy refusing valid input.
#:
#: Only the five the `authoring` profile adds. The restricted six are unchanged
#: accepted behaviour and are validated where they always were -- at the service,
#: which answers with its own typed refusal.
_CANONICAL_INPUT: Final[dict[str, Callable[[object], object]]] = {
    "memory.create": decode_memory_create_input,
    "evidence.capture": decode_evidence_capture_input,
    "import.start": decode_import_start_input,
    "job.get": decode_job_get_input,
    "job.events": decode_job_events_input,
}


class StartupError(Exception):
    """A fixed-text refusal raised before MCP initialization.

    Same contract as :class:`~omnivia_core_mcp.configuration.McpConfigurationError`
    and for the same reason: every one of these is decided before the stdio
    transport opens, reaches the host on stderr, and quotes no endpoint, path,
    workspace identifier or credential reference.
    """


_AMBIGUOUS_WORKSPACE: Final = (
    "the MCP configuration does not select one unambiguous workspace"
)
_WORKSPACE_MISMATCH: Final = (
    "the connected service does not serve the selected workspace"
)
#: The one thing a failed managed start is told, whatever failed. The shared
#: client collapses every cause -- an unrecognised installation layout, no
#: workspace, no service program, a launcher that would not answer or answered
#: with something unreadable, a start that never became reachable -- into one
#: payload-free refusal, and this is that refusal in this server's vocabulary
#: plus the instruction only an adapter can give. Naming which of the causes it
#: was would mean reporting a path, a launcher field or a child's output.
_MANAGED_START_UNREACHABLE: Final = (
    "the managed service could not be started for this configuration. If this "
    "installation has no workspace yet, run `omnivia init` to create one and "
    "start this server again: this server starts an existing workspace and "
    "creates none"
)
_NO_CREDENTIAL_RESOLVER: Final = (
    "remote service mode requires an injected trusted credential resolver"
)
#: What a managed-local configuration with no usable dedicated principal is told.
#: One sentence for every reason -- no reference in the document at all, nothing
#: filed under the one it carries, a stored file that is not owner-private, one
#: that is not a credential, a store that could not be read -- because the
#: reference, the store and the bytes are all things this refusal reaches a
#: host's stderr carrying, and the answer to every one of them is the same:
#: re-run the installed setup for this host. It is the *only* outcome besides a
#: resolved bearer: there is no branch below that reaches the service unauthenticated.
_NO_INSTALLED_CREDENTIAL: Final = (
    "this installation holds no usable credential for the configured reference. "
    "Re-run the installed OmniVia MCP setup for this host: the server presents a "
    "dedicated principal's credential on every call and will not fall back to "
    "the service's own authority"
)
_SERVICE_UNAVAILABLE: Final = "the configured service could not be connected"
_LEGACY_UPGRADE_REFUSED: Final = (
    "the legacy managed-local MCP configuration could not be upgraded safely. "
    "Run the installed OmniVia MCP configure command for this host with the "
    "restricted profile and start the server again"
)
_LEGACY_UPGRADE_UNRECOVERED: Final = (
    "the legacy managed-local MCP configuration could not be restored after a "
    "failed upgrade. Run the installed OmniVia MCP configure command for this host "
    "with the restricted profile before starting the server again"
)


@dataclass(frozen=True, slots=True)
class ConnectedSession:
    """One trusted configuration, one live service, and how it was reached.

    Built only by :func:`connect`, which is what makes the fields mean something
    together: `workspace_id` is the configuration's unambiguous selection *and*
    the descriptor the connected service answered with, checked equal before
    this exists. Frozen, because nothing serving a session may swap the service
    or the authority under it.

    `profile` is the exposure profile this session advertises and dispatches
    against, decided once by
    :func:`~omnivia_core_mcp.configuration.effective_profile` before any tool is
    advertised. It is here rather than recomputed per request so that the
    listing and the call path cannot disagree, and frozen with the rest so a
    running session cannot be widened: `restricted` by default, for a caller
    that builds a session without going through :func:`connect` at all.

    `credentials` is the cache this process created for a remote endpoint, held
    for exactly one reason -- :meth:`clear_credentials` at shutdown and on every
    failed startup path. Local mode has none, and `None` is that fact rather
    than an empty one.
    """

    configuration: McpConfiguration
    client: ServiceClient
    workspace_id: str
    status: str
    credentials: CredentialCache | None = None
    profile: str = RESTRICTED_PROFILE

    def clear_credentials(self) -> None:
        """Drop any credential this process resolved. Safe to call twice."""
        if self.credentials is not None:
            self.credentials.clear()


def connect(
    configuration: McpConfiguration,
    *,
    credential_resolver: CredentialResolver | None = None,
    authoring_admission: AuthoringAdmission | None = None,
) -> ConnectedSession:
    """Reach the service this configuration names, or refuse before MCP starts.

    The workspace is settled first and settled once. A configuration that
    allow-lists several workspaces without naming a default selects none of
    them, and this server takes no argument that would choose between them --
    so it refuses rather than picking, because picking is the decision R004-06
    keeps away from the model.

    `credential_resolver` is the host's, injected. There is no default, no
    environment lookup, no argv secret and no file beside the configuration: a
    remote endpoint with no resolver fails closed here.

    `authoring_admission` is injected on exactly the same terms and for the same
    reason: the protected seam described on
    :data:`~omnivia_core_mcp.configuration.AuthoringAdmission`, which Phase 6
    must implement and nothing in this repository implements yet.

    **The profile is settled last, and that ordering is the seam's contract.**
    The admission is asked only after the service is connected *and* after its
    descriptor is proved to name the selected workspace, because it is handed
    that connected client and must be able to read the protected record through
    it rather than opening an installation database or dialling a second
    connection of its own. It is still settled before `stdio_server()` is
    entered and before one tool is advertised, so `tools/list` and the call path
    read one frozen decision and neither can be reached by a prompt or an
    argument. A connect that fails and a descriptor that disagrees both raise
    here, so neither reaches the admission and neither yields a session at all.
    """
    workspace_id = configuration.selected_workspace_id
    if workspace_id is None:
        raise StartupError(_AMBIGUOUS_WORKSPACE)

    credentials: CredentialCache | None = None
    if configuration.service_mode == "managed_local":
        client, status = _connect_managed_local(configuration, workspace_id)
    else:
        client, status, credentials = _connect_service_client(
            configuration, credential_resolver
        )

    if client.descriptor.workspace_id != workspace_id:
        # The one check both modes need and neither transport can make: a
        # service may be reachable, compatible and live, and still be serving a
        # workspace this configuration never allow-listed.
        if credentials is not None:
            credentials.clear()
        raise StartupError(_WORKSPACE_MISMATCH)
    return ConnectedSession(
        configuration=configuration,
        client=client,
        workspace_id=workspace_id,
        status=status,
        credentials=credentials,
        profile=effective_profile(
            configuration,
            client,
            workspace_id,
            authoring_admission=authoring_admission,
        ),
    )


def _connect_managed_local(
    configuration: McpConfiguration, workspace_id: str
) -> tuple[ServiceClient, str]:
    """Hand the whole of managed-local startup to the shared client.

    Two lines of this adapter's own: the configuration says which installation
    and which workspace, and a budget says how long the whole of it may take.
    Everything else -- the descriptor read, the transport choice, the liveness
    probe, whether the layout authorises a start at all, locating and running the
    service program, reading its bounded result, and reconnecting afterwards --
    is :func:`~omnivia_core_client.connect_managed_local`'s, in the one package
    that owns it. There is no launcher, no path convention, no argv and no
    process control in this package, which is what
    `packages/omnivia-core-runtime/tests/phase2/test_service_and_adapters.py`
    asserts by reading this file.

    Its refusal is already a fixed payload-free sentence, and it is still
    translated rather than re-raised: what reaches a host on stderr is this
    server's own vocabulary about its own startup, with one instruction added
    that this adapter can give and the shared client deliberately cannot -- the
    client does not know that `omnivia init` is the command, and must not carry
    a CLI's name.
    """
    state = configuration.installation_state
    if state is None:  # pragma: no cover - the configuration model forbids it
        raise StartupError(_SERVICE_UNAVAILABLE)
    # Before anything is started, because a configuration that cannot present
    # its own bearer has no session to reach whatever happens next, and starting
    # a service this process is about to refuse to talk to is a cold start and a
    # running service bought for a refusal.
    store, reference = _installed_credential(configuration)
    service_config = InstallationServiceConfig(
        installation_state=state, workspace_id=workspace_id
    )
    deadline = Deadline.after(MANAGED_START_TIMEOUT_SECONDS)
    connected = None
    try:
        connected = connect_managed_local(service_config, deadline=deadline)
    except ManagedStartError:
        connected = None
    if connected is None:
        raise StartupError(_MANAGED_START_UNREACHABLE)
    return (
        authenticated_client(connected.client, lambda: store.resolve(reference)),
        connected.status,
    )


def _installed_credential(
    configuration: McpConfiguration,
) -> tuple[InstalledCredentialStore, CredentialReference]:
    """The store and the name this managed-local session presents, or a refusal.

    **There is no other outcome, and that is the whole of the rule.** A
    managed-local configuration that names no credential, and one naming a
    credential this installation cannot produce, are the same thing to this
    server: a configuration with no dedicated principal to call as. Both refuse
    here, before a service is started and long before MCP initialization, rather
    than reaching the installation-local endpoint as whatever the service itself
    runs as. A local endpoint admits an unauthenticated application call, so
    falling back would be a working session dispatching under the service's own
    administrator identity -- silently, with `authoring` the only thing it could
    not do, which is the wrong half to be stopped by.

    One sentence covers both, because the difference between them is which
    reference a document carries and which file a store could not read, and a
    refusal that reaches a host's stderr carries neither. The instruction is the
    same either way: re-run the installed setup for this host.

    The bearer is resolved **once here and then not kept**: the resolution is a
    startup check rather than a cached value, so a credential this installation
    does not hold, or holds in a file that is not owner-private, fails before one
    tool is advertised instead of at the first call a model makes. What the
    session carries is the store and the name, asked again on every call -- so a
    revoked or rotated credential takes effect on the next call rather than at
    the next restart, and nothing this process holds outlives the authority
    behind it.
    """
    reference = configuration.credential_reference
    store = _installed_store(configuration)
    if reference is None or store is None:
        raise StartupError(_NO_INSTALLED_CREDENTIAL)
    resolvable = True
    try:
        store.resolve(reference)
    except ClientError:
        resolvable = False
    if not resolvable:
        # Outside the handler: a `ClientError` reachable through `__context__`
        # is the store's own refusal, and this one must quote nothing at all.
        raise StartupError(_NO_INSTALLED_CREDENTIAL)
    return store, reference


def _resumable_legacy_setup(
    configuration: McpConfiguration,
    store: InstalledCredentialStore,
    setups: tuple[Any, ...],
) -> Any | None:
    """Return one unambiguous, already-authorized restricted setup to resume.

    Legacy configuration carries no trusted host identity. Migration therefore
    cannot choose a free host slot, mint authority, or guess between two matching
    slots. It may only finish publication for exactly one active restricted
    setup whose credential is already present in this installation's protected
    store -- the recoverable state an interrupted explicit configure can leave.
    """
    workspace_id = configuration.selected_workspace_id
    if workspace_id is None:
        return None
    matching: list[Any] = []
    for setup in setups:
        if (
            setup.status == "active"
            and setup.workspace_id == workspace_id
            and setup.profile == RESTRICTED_PROFILE
            and setup.authoring_intent is False
        ):
            reference = None
            try:
                reference = CredentialReference(setup.credential_reference)
            except ClientError:
                reference = None
            healthy = False
            if reference is not None:
                try:
                    healthy = store.health(reference) == "present"
                except ClientError:
                    healthy = False
            if healthy:
                matching.append(setup)
    return matching[0] if len(matching) == 1 else None


def _legacy_configuration_document(
    configuration: McpConfiguration,
    *,
    principal_id: str | None = None,
    credential_reference: str | None = None,
) -> bytes:
    """Return a narrowed legacy document, optionally with dedicated authority."""
    workspace_id = configuration.selected_workspace_id
    state = configuration.installation_state
    if workspace_id is None or state is None:  # pragma: no cover - caller proves both
        raise ValueError("legacy configuration is not selectable")
    document: dict[str, Any] = {
        "allowed_purposes": list(configuration.allowed_purposes),
        "allowed_workspace_ids": [workspace_id],
        "default_workspace_id": workspace_id,
        "format": configuration.format,
        "installation_state": str(state),
        # A legacy true value is not informed consent for the expanded surface.
        "mutation_enabled": False,
        "principal_id": (
            configuration.principal_id if principal_id is None else principal_id
        ),
        "service_mode": "managed_local",
    }
    if credential_reference is not None:
        document["credential_reference"] = credential_reference
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _legacy_setup_authenticates(
    control: Any,
    store: InstalledCredentialStore,
    reference: CredentialReference,
    setup: Any,
    *,
    deadline: Deadline,
) -> bool:
    """Whether this stored bearer still resolves to this restricted setup now."""
    admission = None
    try:
        admission = mcp_authoring_admission(
            control,
            store.resolve(reference).reveal(),
            deadline=deadline,
        )
    except (ClientError, OSError):
        admission = None
    return bool(
        admission is not None
        and admission.admitted is False
        and admission.principal_id == setup.principal_id
        and admission.workspace_id == setup.workspace_id
    )


def upgrade_legacy_configuration(
    path: Path, configuration: McpConfiguration
) -> McpConfiguration:
    """Finish one interrupted explicit restricted setup, or fail closed.

    Migration itself never creates or widens a grant. A credential-free legacy
    document has no host identity, so ordinary startup cannot safely choose a
    host slot or call ``mcp.configure``. The sole automatic case is an
    unambiguous active restricted setup for the selected workspace whose bearer
    is already recoverable from protected storage. That is an interrupted
    publication, not new authority; this function only publishes its narrowed
    configuration. Every other legacy document is refused with the instruction
    to run the explicit installed configure command for the intended host.
    """
    if (
        configuration.service_mode != "managed_local"
        or configuration.credential_reference is not None
    ):
        return configuration
    workspace_id = configuration.selected_workspace_id
    state = configuration.installation_state
    if workspace_id is None or state is None:
        raise StartupError(_LEGACY_UPGRADE_REFUSED)

    original_document = read_owner_private(
        path, maximum_bytes=MAXIMUM_CONFIGURATION_BYTES + 1
    )
    if original_document is None:
        raise StartupError(_LEGACY_UPGRADE_REFUSED)
    original_configuration = None
    try:
        original_configuration = _parse_configuration_bytes(original_document)
    except McpConfigurationError:
        original_configuration = None
    if original_configuration != configuration:
        # The caller parsed one generation and this function read another. Only
        # the exact generation represented by ``configuration`` may be migrated.
        raise StartupError(_LEGACY_UPGRADE_REFUSED)

    deadline = Deadline.after(MANAGED_START_TIMEOUT_SECONDS)
    connected = None
    try:
        connected = connect_managed_local(
            InstallationServiceConfig(
                installation_state=state, workspace_id=workspace_id
            ),
            deadline=deadline,
        )
    except (ClientError, ManagedStartError, OSError):
        connected = None
    if connected is None:
        raise StartupError(_LEGACY_UPGRADE_REFUSED)

    control = None
    setups: tuple[Any, ...] | None = None
    try:
        control = local_control_transport(connected.client)
        setups = mcp_status(control, deadline=deadline).setups
    except (ClientError, OSError):
        setups = None
    if control is None or setups is None:
        raise StartupError(_LEGACY_UPGRADE_REFUSED)

    store = InstalledCredentialStore(state)
    setup = _resumable_legacy_setup(configuration, store, setups)
    if setup is None:
        raise StartupError(_LEGACY_UPGRADE_REFUSED)
    reference = None
    try:
        reference = CredentialReference(setup.credential_reference)
    except ClientError:
        reference = None
    if reference is None:
        raise StartupError(_LEGACY_UPGRADE_REFUSED)
    valid_setup = (
        setup.workspace_id == workspace_id
        and setup.profile == RESTRICTED_PROFILE
        and setup.authoring_intent is False
        and setup.status == "active"
    )
    if not valid_setup:
        raise StartupError(_LEGACY_UPGRADE_REFUSED)

    if not _legacy_setup_authenticates(
        control, store, reference, setup, deadline=deadline
    ):
        raise StartupError(_LEGACY_UPGRADE_REFUSED)

    document = _legacy_configuration_document(
        configuration,
        principal_id=setup.principal_id,
        credential_reference=setup.credential_reference,
    )
    if (
        replace_owner_private_if_current(
            path,
            original_document,
            document,
            maximum_bytes=MAXIMUM_CONFIGURATION_BYTES,
        )
        != "replaced"
    ):
        raise StartupError(_LEGACY_UPGRADE_REFUSED)

    # Publication never outruns the authority it names. A revocation, rotation,
    # profile change, or identity mismatch between the pre-publication proof and
    # this immediate second proof restores the exact trusted legacy document.
    if not _legacy_setup_authenticates(
        control, store, reference, setup, deadline=deadline
    ):
        if (
            replace_owner_private_if_current(
                path,
                document,
                original_document,
                maximum_bytes=MAXIMUM_CONFIGURATION_BYTES,
            )
            != "replaced"
        ):
            raise StartupError(_LEGACY_UPGRADE_UNRECOVERED)
        raise StartupError(_LEGACY_UPGRADE_REFUSED)

    upgraded = None
    try:
        upgraded = read_configuration(path)
    except McpConfigurationError:
        upgraded = None
    expected = McpConfiguration(
        format=configuration.format,
        principal_id=setup.principal_id,
        allowed_workspace_ids=(workspace_id,),
        default_workspace_id=workspace_id,
        allowed_purposes=configuration.allowed_purposes,
        mutation_enabled=False,
        service_mode="managed_local",
        installation_state=state,
        endpoint=None,
        credential_reference=reference,
    )
    if upgraded != expected:
        if (
            replace_owner_private_if_current(
                path,
                document,
                original_document,
                maximum_bytes=MAXIMUM_CONFIGURATION_BYTES,
            )
            != "replaced"
        ):
            raise StartupError(_LEGACY_UPGRADE_UNRECOVERED)
        raise StartupError(_LEGACY_UPGRADE_REFUSED)
    return upgraded


def _installed_store(
    configuration: McpConfiguration,
) -> InstalledCredentialStore | None:
    """This installation's protected store, for a configuration that names one.

    The store is rooted at the configuration's own ``installation_state`` and
    chooses everything below it: this adapter passes a trusted root and never a
    path, a filename or a directory, so there is no configuration value and no
    argument that could point credential resolution anywhere else.
    """
    state = configuration.installation_state
    if state is None or configuration.credential_reference is None:
        return None
    return InstalledCredentialStore(state)


def _installed_admission(configuration: McpConfiguration) -> AuthoringAdmission | None:
    """The production authoring admission, for an installation that can answer one.

    ``None`` whenever there is no bearer to present: a remote configuration, whose
    credential is the injecting host's rather than this installation's. Such a
    server has no way to ask the protected authority anything, so
    :func:`~omnivia_core_mcp.configuration.effective_profile` is given nothing and
    `restricted` is the only profile it can reach, whatever `mutation_enabled`
    says. A managed-local configuration with no reference also answers ``None``
    here, but nothing reaches a profile on that path any more --
    :func:`_installed_credential` has already refused it -- so this is a total
    function rather than a second gate.

    With a bearer, the answer comes from
    :func:`~omnivia_core_client.mcp_authoring_admission`: the service reads
    durable protected state fresh on that call and says whether the principal
    that bearer resolves to is admitted to author, and for whom. **Both
    identifiers are then compared, and that comparison is the admission.** A true
    ``admitted`` for some other principal or some other workspace is an answer
    about a different session, and accepting it would let a credential filed for
    one workspace author in another; the identifiers the comparison uses are the
    trusted configuration's, which no prompt, argument or tool call can reach.

    The credential is resolved inside the call, not captured: an admission asked
    after a revocation asks with whatever the store holds then, which is nothing.
    """
    reference = configuration.credential_reference
    store = _installed_store(configuration)
    if reference is None or store is None:
        return None

    def admission(client: ServiceClient, principal_id: str, workspace_id: str) -> bool:
        answer = mcp_authoring_admission(
            local_control_transport(client),
            store.resolve(reference).reveal(),
            deadline=Deadline.after(CALL_TIMEOUT_SECONDS),
        )
        return (
            answer.admitted
            and answer.principal_id == principal_id
            and answer.workspace_id == workspace_id
        )

    return admission


def _connect_service_client(
    configuration: McpConfiguration,
    credential_resolver: CredentialResolver | None,
) -> tuple[ServiceClient, str, CredentialCache]:
    """Connect to the configured HTTP endpoint with the host's own resolver.

    The configuration carries the *name* of a credential and the normalized
    origin it may be presented to; the secret exists only inside the cache, only
    for as long as its TTL, and only because a resolver this process was handed
    answered for that pair. A startup that does not complete drops the cache on
    the way out, so a refused start leaves nothing resolved behind it.
    """
    if credential_resolver is None:
        raise StartupError(_NO_CREDENTIAL_RESOLVER)
    endpoint = configuration.endpoint
    reference = configuration.credential_reference
    if endpoint is None or reference is None:  # pragma: no cover - model forbids it
        raise StartupError(_SERVICE_UNAVAILABLE)

    credentials = CredentialCache(credential_resolver)
    connected: ServiceClient | None = None
    try:
        connected = ServiceClient.connect(
            HttpServiceConfig(
                endpoint_uri=endpoint,
                credential_reference=reference,
                credentials=credentials,
            ),
            deadline=Deadline.after(CONNECT_TIMEOUT_SECONDS),
        )
    finally:
        if connected is None:
            credentials.clear()
    if connected is None:
        raise StartupError(_SERVICE_UNAVAILABLE)
    return connected, "connected", credentials


def build_server(*, session: ConnectedSession) -> Server[object]:
    """The MCP server for one connected Core service.

    Everything authority-shaped is already settled in `session`, so there is no
    factory, no endpoint and no credential argument here any more: a test that
    wants a different service connects a different session, which is the same
    object this takes in production.
    """

    async def on_list_tools(
        _context: object, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        """Every allow-listed tool of this session's profile, in manifest order.

        R004-06 requires this to be deterministic for a given package version and
        configuration. Both inventories are built once at import in
        :mod:`omnivia_core_mcp.manifest`, and the profile was settled once in
        :func:`connect`, so the listing is a lookup: nothing is filtered, sorted,
        or read from the environment here, and nothing about a prompt or an
        argument can reach it.

        In particular the allowed-purpose set does *not* filter the listing -- a
        listing that varied with the authority granted to one host would not be
        deterministic, and the purpose is enforced on call instead, which is
        where refusing it is a decision rather than a disappearance.
        """
        return types.ListToolsResult(tools=list(tools(session.profile)))

    async def on_call_tool(
        _context: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return _call_tool(params, session=session)

    return Server(
        SERVER_NAME,
        version=__version__,
        title="OmniVia Core",
        instructions=(
            "Read and authoring access to a local OmniVia Core workspace. Every "
            "tool is explicitly allow-listed; service lifecycle, workspace "
            "creation, governance decisions and every other mutation are "
            "deliberately absent and cannot be called. A writing tool takes the "
            "operation input under `input` and a caller-chosen `idempotency_key`."
            if session.profile == AUTHORING_PROFILE
            else "Read-only access to a local OmniVia Core workspace. Every tool "
            "is explicitly allow-listed; service lifecycle, workspace creation "
            "and every mutation are deliberately absent and cannot be called."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _call_tool(
    params: types.CallToolRequestParams, *, session: ConnectedSession
) -> types.CallToolResult:
    """Dispatch one tool call against the allow-list and the configured authority.

    Four refusals come before anything is sent, in this order and for four
    different reasons. The allow-list is the *only* lookup, so an operation
    absent from the manifest is not callable rather than merely unadvertised.
    The manifest's purpose must be one the configuration allows, so a host
    granted `workspace_inspection` alone cannot retrieve knowledge with a tool
    it can see. The payload must be one the advertised schema declares, with no
    authority-shaped key anywhere in it. And for the five operations the
    `authoring` profile adds, the canonical contract must accept the values too
    -- its own public decoder decides that, and a mutation's idempotency key is
    put through the envelope's own predicate beside it.

    None of the four reaches the client, so none of them costs a dial, a
    credential resolution or a service round trip.

    The lookup is the *session's profile's* allow-list, which is the same
    inventory `tools/list` returned, so the two cannot disagree: a restricted
    server does not merely omit `memory_create` from its listing, it has no way
    to resolve that name to an operation at all.
    """
    exposed = exposed_by_tool_name(params.name, session.profile)
    if exposed is None:
        return _failure(
            f"{params.name!r} is not a tool this server exposes. "
            f"Available: {', '.join(tool.name for tool in tools(session.profile))}."
        )

    if exposed.purpose not in session.configuration.allowed_purposes:
        return _failure(
            f"{params.name} states a purpose this server's configuration does "
            "not allow, so it was not called."
        )

    try:
        request = _request(
            exposed,
            configuration=session.configuration,
            workspace_id=session.workspace_id,
            arguments=params.arguments,
        )
    except ValueError as refusal:
        return _failure(f"{params.name}: {refusal}")

    try:
        response = session.client.call(
            request, deadline=Deadline.after(CALL_TIMEOUT_SECONDS)
        )
    except ClientError as failure:
        # Every way a client call is documented to fail: it could not carry the
        # call, what came back was not a frame, a credential did not resolve,
        # the deadline passed, or the caller cancelled. All of them are answers
        # to give a model, not tracebacks. The client's diagnostics are
        # payload-free by construction, so passing one through to a model quotes
        # no workspace content, no endpoint and no local path.
        return _failure(f"{params.name} could not be called: {failure}")

    if not _correlates(request, response):
        # Before either branch below publishes anything: an answer that does not
        # carry this request's correlation identifier is not this request's
        # answer, and publishing it as `structuredContent` would attribute one
        # call's result to another.
        return _failure(
            f"{params.name} was answered by a response that does not correlate "
            "with the request, so the answer was not published."
        )

    if not isinstance(response, SuccessResponseEnvelope):
        return _failure(
            f"{params.name} was refused by the service: "
            f"{codec.to_canonical_json(codec.encode_response(response))}"
        )
    # Through the codec's own encoder, not `dict(response.result)`. `dict()`
    # converts the top level only, and a decoded envelope carries read-only
    # mappings further down -- `to_canonical_json` is `json.dumps`, which refuses
    # a nested `mappingproxy` outright. `encode_response` is the accepted way to
    # get a wire-shaped document, and is what the CLI has always used.
    encoded = codec.encode_response(response)["result"]
    if not isinstance(encoded, dict):
        # Refused rather than substituted. This read `encoded if isinstance(...)
        # else {}`, which published an empty success document for a shape the
        # advertised output schema does not describe -- inert while no schema was
        # advertised, and a lie the moment one is.
        return _failure(
            f"{params.name} returned a result this server cannot publish: the "
            f"encoded result is {type(encoded).__name__}, not a JSON object"
        )
    return types.CallToolResult(
        # The contract-encoded result itself. The advertised `output_schema`
        # describes exactly this document, and the official client validates it
        # against that schema on every successful call.
        structured_content=encoded,
        # Exactly one text item, and it is the same document: a host that predates
        # structured content still gets the whole answer, and one that has it can
        # check the two agree. Serialised through the codec's canonical encoder, so
        # the mirror is byte-stable rather than dict-order-dependent.
        content=[types.TextContent(type="text", text=codec.to_canonical_json(encoded))],
    )


def _correlates(request: RequestEnvelope, response: ResponseEnvelope) -> bool:
    """Whether this response is an answer to this request.

    Both identifiers are checked because both are this process's own: the
    request id and the correlation id are minted together below, and a peer that
    echoes neither -- or echoes one from an earlier call -- has not answered the
    call being published.
    """
    return (
        response.metadata.correlation_id == request.metadata.correlation_id
        and response.metadata.request_id == request.metadata.request_id
    )


def _failure(message: str) -> types.CallToolResult:
    """A refusal the model can read, as a tool error rather than an exception.

    `is_error` rather than a raise: an exception out of a handler is a protocol
    error the model never sees the text of, and every refusal here is information
    it should act on -- a wrong tool name, a purpose it was not granted, an
    unavailable call, a service that said no.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


def _request(
    exposed: ExposedOperation,
    *,
    configuration: McpConfiguration,
    workspace_id: str,
    arguments: Mapping[str, Any] | None,
) -> RequestEnvelope:
    """A contract-valid envelope for one allow-listed operation.

    **Scopes and the capability requirement are read off the frozen catalogue
    entry, never transcribed.** The catalogue validator refuses a request that
    declares no capability, or the wrong one, and the service builds its own
    session from that same entry -- so both ends of the call read one source and
    a renamed capability fails as a rename rather than as a mystery refusal two
    files away.

    **The principal and the workspace come from the trusted configuration.** The
    principal claim is exactly `principal_id` from the configuration document and
    is a *claim*: the service decides from its own grant, and a claim that is not
    granted is refused there. The workspace is the one unambiguous allow-listed
    selection the session connected to and proved the service serves.

    **A model supplies neither, and cannot.** :data:`RESERVED_ARGUMENTS` refuses
    an authority-shaped key by name -- at the outer object *and* inside a
    mutation's nested input, so unwrapping cannot become a way to smuggle one in
    -- and the advertised schema's own `properties` refuses every key it does not
    declare, read off the projection rather than from a literal list, so what
    `tools/list` says and what this accepts stay one document. Every advertised
    payload declares `unevaluatedProperties: false`, so a key outside that set is
    one the contract refuses anyway; refusing it here costs the model a round
    trip to find that out.

    **Values are checked too, and by the contract itself.** A key list is not a
    schema, so the five operations the `authoring` profile added go through
    :func:`_refuse_uncanonical`, which runs the public
    `omnivia_core.contracts.v1` decoder for the operation and nothing of its
    own. The service still validates what it receives -- it must, because MCP is
    not its only caller -- so this is the same judgment reached earlier, not a
    substitute for it.

    **A mutation's key travels in the envelope, not in the payload.** The
    advertised wrapper is unwrapped here and nowhere else: `input` becomes the
    request's canonical operation input (with capture text represented by its
    equivalent compact base64 form), and `idempotency_key` becomes
    `RequestMetadata.idempotency_key`, which is where the contract puts it and
    where the service's own durable mutation coordinator looks for it. A read
    carries no key at all. Nothing about a repeat is decided here: this builds a
    fresh envelope for every call, so a replay is a real call that Core settles
    against its stored outcome -- and re-checks its authority for -- rather than
    an answer this process remembered.
    """
    supplied = dict(arguments or {})
    _refuse_reserved(exposed, supplied)
    entry = get_operation_metadata(exposed.operation)
    schema = input_schema(entry)
    if exposed.operation in ADMITTED_MUTATIONS:
        payload, key = _unwrapped(exposed, supplied)
        _refuse_reserved(exposed, payload)
        advertised = set(schema["properties"]["input"]["properties"])
    else:
        payload, key = supplied, None
        advertised = set(schema["properties"])
    unknown = sorted(set(payload) - advertised)
    if unknown:
        raise ValueError(
            f"{exposed.tool_name} accepts no argument named {unknown[0]!r}"
            + (f" (or {len(unknown) - 1} other(s))" if len(unknown) > 1 else "")
            + f"; its advertised schema declares {sorted(advertised)} and is closed"
        )
    oversized_capture = _refuse_uncanonical(exposed, payload)
    payload = _transport_payload(
        exposed, payload, oversized_capture=oversized_capture
    )
    required = entry.required_capability
    request_id = f"mcp-{uuid.uuid4()}"
    return RequestEnvelope(
        operation=exposed.operation,
        metadata=RequestMetadata(
            request_id=request_id,
            correlation_id=request_id,
            trace_id=request_id,
            api_version=CLIENT_API_VERSION,
            client=ClientIdentity(id=CLIENT_NAME, version=__version__),
            workspace_id=workspace_id,
            scopes=tuple(entry.scope.required_scopes),
            # A claim, not authority: the service decides from its own grant.
            purpose=exposed.purpose,
            idempotency_key=key,
            required_capabilities=(
                CapabilityRequirement(
                    id=required.id,
                    minimum_version=required.minimum_version,
                    required=required.required,
                ),
            ),
            principal_claim=PrincipalClaim(
                claimed_principal_id=configuration.principal_id
            ),
        ),
        input=payload,
    )


def _transport_payload(
    exposed: ExposedOperation,
    payload: Mapping[str, Any],
    *,
    oversized_capture: bool = False,
) -> dict[str, Any]:
    """Choose the compact equivalent capture representation for Core's wire.

    OVC1 v1's 4 MiB ceiling is frozen. A valid 1 MiB text value can expand to
    more than that when canonical JSON escapes C0 controls, while the same UTF-8
    bytes encoded as base64 remain below the ceiling. The evidence contract
    declares ``text`` and ``content_base64`` as equivalent alternatives, so MCP
    always sends the deterministic base64 form after validating the caller's
    original document. This also makes text/base64 retries settle against one
    canonical input rather than representation-dependent idempotency material.
    """
    compact = dict(payload)
    if exposed.operation != "evidence.capture":
        return compact
    if oversized_capture:
        compact.pop("text", None)
        compact.pop("content_base64", None)
        compact["content_base64"] = "A" * _CAPTURE_OVERSIZE_SENTINEL_LENGTH
        return compact
    value = compact.pop("text", None)
    if isinstance(value, str):
        compact["content_base64"] = base64.b64encode(value.encode("utf-8")).decode(
            "ascii"
        )
    return compact


def _refuse_reserved(exposed: ExposedOperation, supplied: Mapping[str, Any]) -> None:
    """Refuse an authority-shaped key by name, wherever in the call it appears."""
    reserved = sorted(set(supplied) & RESERVED_ARGUMENTS)
    if reserved:
        raise ValueError(
            f"{exposed.tool_name} does not take {reserved[0]!r}: the principal, "
            "the workspace, the purpose, the granted authority, the endpoint and "
            "the credential are fixed by this server's trusted configuration and "
            "cannot be set by a caller"
        )


def _refuse_uncanonical(
    exposed: ExposedOperation, payload: Mapping[str, Any]
) -> bool:
    """Refuse an input the operation's own canonical contract does not accept.

    The advertised wrapper proves a call is the right *shape* and the projected
    schema's `properties` proves its keys are declared; neither says a thing
    about a value. `omnivia_core.contracts.v1`'s public decoder does, and it is
    the same decode-then-validate pair the service runs -- so a missing required
    field, a media type outside the allowlist, a malformed timestamp or a
    cross-field contradiction is refused here, once, rather than becoming a round
    trip whose only outcome is the service's refusal.

    **The refusal is fixed and quotes nothing.** A contract error names the path
    it failed at and frequently the value, and this server's refusals go to a
    model over a channel that is not the caller's own: the same rule every other
    refusal in this module follows. The advertised input schema already carries
    every constraint, so a caller reading it has what it needs to correct the
    call.

    Only the five operations the `authoring` profile adds are checked, because
    they are the ones this phase added. Nothing here is a second opinion about
    them: an operation absent from :data:`_CANONICAL_INPUT` is sent exactly as it
    always was.
    """
    decode = _CANONICAL_INPUT.get(exposed.operation)
    if decode is None:
        return False
    try:
        if exposed.operation == "evidence.capture" and _capture_exceeds_limit(payload):
            return True
        decode(dict(payload))
    except EvidenceCaptureSizeLimitError:
        # The Application Contract gives an over-limit capture its own canonical
        # error code.  Let the service classify it so MCP relays the same typed
        # response as in-process and local IPC callers instead of replacing it with
        # this adapter's uncoded preflight refusal.
        return True
    except (ContractDecodeError, ContractSemanticError) as refusal:
        raise ValueError(
            "the input this call carries is not a valid document for "
            f"{exposed.operation}, so it was not sent. Its advertised input "
            "schema states every field, type and bound the operation requires; "
            "this refusal deliberately repeats none of what was supplied"
        ) from refusal
    return False


def _capture_exceeds_limit(payload: Mapping[str, Any]) -> bool:
    """Recognize an over-limit content form with bounded work and no copy.

    The canonical decoder is still authoritative for every structural and semantic
    decision.  This is only its cheap size-ordering rule moved ahead of ``str.encode``:
    make one allocation-free pass over text (also preserving the decoder's surrogate
    refusal), or compare the encoded string's length, then let Core classify a fixed
    bounded surrogate document.
    """
    text = payload.get("text")
    encoded = payload.get("content_base64")
    if (text is None) == (encoded is None):
        return False
    if isinstance(encoded, str):
        return len(encoded) > _CAPTURE_MAX_BASE64_LENGTH
    if not isinstance(text, str):
        return False

    length = 0
    oversized = False
    for character in text:
        scalar = ord(character)
        if 0xD800 <= scalar <= 0xDFFF:
            raise ContractSemanticError("text is not valid Unicode text")
        if oversized:
            continue
        if scalar <= 0x7F:
            length += 1
        elif scalar <= 0x7FF:
            length += 2
        elif scalar <= 0xFFFF:
            length += 3
        else:
            length += 4
        if length > EVIDENCE_CAPTURE_MAX_CONTENT_BYTES:
            oversized = True
    return oversized


def _unwrapped(
    exposed: ExposedOperation, supplied: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    """The canonical input and the idempotency key out of a mutation's wrapper.

    The closed two-field object the tool already advertises, enforced rather than
    described: both halves present, an object and a string, and nothing else
    alongside them. An extra outer key is refused rather than dropped -- it is a
    caller saying something this seam does not accept, and silently ignoring it
    would make the mutation look like it had honoured a constraint it never saw.

    What a key may *spell* is the canonical contract's business, and it is asked
    rather than restated: :func:`~omnivia_core.contracts.v1.is_idempotency_key`
    is the envelope's own primitive for exactly this, applying the pattern and
    the length bounds the advertised wrapper already published. A key that fails
    it would be refused by `RequestMetadata` anyway, as an exception out of a
    handler rather than an answer -- and refusing it here means a write whose key
    cannot settle a replay is never sent at all, which is the one shape where a
    round trip is worse than a refusal.
    """
    payload = supplied.get("input")
    key = supplied.get("idempotency_key")
    if (
        set(supplied) != {"input", "idempotency_key"}
        or not isinstance(payload, dict)
        or not isinstance(key, str)
    ):
        raise ValueError(
            f"{exposed.tool_name} writes, so it takes exactly the advertised "
            "wrapper: an `input` object holding the operation's own arguments, "
            "and an `idempotency_key` string that makes a repeat answer from the "
            "settled outcome instead of writing twice. No other property is "
            "accepted, and this server never chooses or retries a key itself"
        )
    if not is_idempotency_key(key):
        raise ValueError(
            f"{exposed.tool_name} takes an `idempotency_key` the canonical "
            "request envelope accepts, and this one is not one. The advertised "
            "wrapper carries that definition's own pattern and length bounds; "
            "this server never chooses a key on a caller's behalf"
        )
    return dict(payload), key


async def serve(*, session: ConnectedSession) -> None:
    """Serve one stdio session against an already-connected service.

    The `redirect_stdout` closes a real gap the SDK's descriptor claim cannot.
    `stdio_server()` has by this point taken its private duplicate of fd 1 for the
    wire and pointed fd 1 itself at stderr, so a *flushed* write from a handler
    already misses the protocol stream. An unflushed one does not: `print()` to a
    pipe is block-buffered, the bytes sit in `sys.stdout`'s buffer for the life of
    the session, and the interpreter flushes them at shutdown -- after the claim
    is released and fd 1 is back on the wire. They then land on the real stdout as
    trailing garbage behind a closed session. Rebinding the object here means
    those bytes never enter that buffer at all. Verified by
    `test_every_byte_the_server_writes_to_stdout_is_valid_protocol`, which caught
    exactly this leak.
    """
    server = build_server(session=session)
    async with stdio_server() as (read_stream, write_stream):
        with redirect_stdout(sys.stderr):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=SERVER_NAME,
                    server_version=__version__,
                    # Every notification option left at its default `False`. The
                    # tool list is frozen at import for a given package version --
                    # that is R004-06's determinism requirement -- so a
                    # `tools/list_changed` capability would advertise an event
                    # this server can never send.
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )


#: What each profile advertises, exactly, and what R004 section 9.2 step 7 says
#: an installed setup must have proved before it reports success.
#:
#: Two numbers rather than a derivation, because the point is to notice a change:
#: the inventory is settled in :mod:`omnivia_core_mcp.manifest` and the counts
#: there are what a build produces, so a check that recomputed them from the
#: manifest would agree with any inventory the manifest happened to hold. These
#: are the requirement's own figures, and a build whose manifest has moved fails
#: this check rather than certifying itself.
EXPECTED_TOOL_COUNT: Final[dict[str, int]] = {
    RESTRICTED_PROFILE: 6,
    AUTHORING_PROFILE: 11,
}

_UNEXPECTED_INVENTORY: Final = (
    "this configuration does not start a server with the exposure this "
    "installation's setup requires"
)
#: What a peer that answered the handshake as somebody else is told. Distinct
#: from the inventory refusal because the remedy is different: an inventory that
#: does not match is this build's own surface having moved, and a server that
#: names itself something else is not this build at all.
_UNEXPECTED_SERVER: Final = (
    "the configured command did not start this installation's own MCP server"
)
#: The one thing every failed exchange is told, whatever failed -- the child
#: could not be spawned, refused its own startup and exited, never completed
#: initialization, answered nothing before the budget expired, or wrote something
#: the SDK would not parse. Naming which would mean relaying a child's exit
#: status, a transport exception or the sentence the child wrote to its own
#: stderr, and that stderr is discarded precisely so there is nothing to relay.
_NOT_QUALIFIED: Final = (
    "the configured MCP server did not complete an initialize and tools/list "
    "exchange within this installation's setup budget"
)

#: The whole budget for one qualification: spawning the child, its own startup --
#: which includes reaching, and if necessary starting, the managed service --
#: initialization, and `tools/list`.
#:
#: Above :data:`MANAGED_START_TIMEOUT_SECONDS` rather than equal to it, because
#: the child spends that budget *before* it writes one protocol byte: a
#: qualification that expired first would report a cold start as an unqualified
#: server and make the installed setup fail for being slow.
QUALIFICATION_TIMEOUT_SECONDS: Final = MANAGED_START_TIMEOUT_SECONDS + 30.0

#: The module a qualification child runs, and with :data:`sys.executable` the
#: whole of its command line besides `--config`.
#:
#: `-m omnivia_core_mcp.server` rather than the `omnivia-core-mcp` console script
#: a host would name: this interpreter, and therefore this import path, so the
#: child is the build being qualified rather than whichever distribution happens
#: to be first on `PATH`. `main` is the same function the console script points
#: at, so what is exercised is the entry point either way.
_QUALIFICATION_MODULE: Final = "omnivia_core_mcp.server"


def verify_installed_setup(config_path: Path) -> int:
    """Qualify one protected configuration over real MCP; return its tool count.

    The handshake and `tools/list` verification an installed setup must pass
    before it reports success, owned here because every part of it is this
    package's: what a trusted configuration is, what this server's identity is,
    and which tools each profile advertises. The installed administration
    command calls this and holds none of it.

    **It is a protocol exchange with a real child, not an in-process rehearsal.**
    :func:`_qualification` runs this module's own entry point as a subprocess and
    drives it with the official SDK's `stdio_client` and `ClientSession`: the
    same transport, framing and handshake an MCP host would use. So what is
    proved is the thing a setup actually needs proved -- that launching this
    command with this configuration yields a server that initializes and
    advertises the expected surface -- rather than that a session object could be
    constructed in this process.

    **The child is told a path and nothing else.** Its whole command line is this
    interpreter, this module and `--config <absolute path>`, and its environment
    is the SDK's sanitized default. No credential, token or bearer appears in
    either: the child resolves its own from this installation's protected store,
    exactly as it does under a host.

    **Three things are then checked.** The peer must identify itself as this
    build -- :data:`SERVER_NAME` at this package's version -- so a command that
    started something else does not qualify. The advertised inventory must be one
    of the two the manifest defines, exactly: every tool name, in order, at the
    count :data:`EXPECTED_TOOL_COUNT` fixes. And the configuration's allowed
    purposes must be exactly the purposes that profile's manifest states, so a
    setup that wrote a purpose list the exposed tools do not need -- or needs one
    it did not write -- is refused rather than published.

    Which profile the child settled on is read off the inventory it advertised
    and never assumed from the document: a `mutation_enabled: true` configuration
    whose protected authority declines to admit it starts `restricted`, and the
    purpose comparison is then what refuses it.

    The count comes back so the caller can report it. Every failure is an
    exception with this module's fixed, payload-free text, and the child is
    stopped on every one of them.
    """
    configuration = read_configuration(config_path)
    name, version, advertised = _qualification(config_path)
    if name != SERVER_NAME or version != __version__:
        raise StartupError(_UNEXPECTED_SERVER)
    profile = _advertised_profile(advertised)
    if profile is None or set(configuration.allowed_purposes) != {
        exposed.purpose for exposed in exposure_manifest(profile)
    }:
        raise StartupError(_UNEXPECTED_INVENTORY)
    return len(advertised)


def _advertised_profile(advertised: tuple[str, ...]) -> str | None:
    """Which profile advertises exactly this inventory, or `None` for neither.

    Exact and ordered, against the manifest this build holds *and* against the
    requirement's own two numbers. A listing that is one of the two inventories
    but the wrong length is impossible unless the manifest has moved, which is
    precisely the drift :data:`EXPECTED_TOOL_COUNT` exists to catch, so both are
    asked rather than one standing in for the other.
    """
    for profile, expected in EXPECTED_TOOL_COUNT.items():
        names = tuple(tool.name for tool in tools(profile))
        if len(names) == expected and advertised == names:
            return profile
    return None


def _qualification(config_path: Path) -> tuple[str, str | None, tuple[str, ...]]:
    """One bounded exchange with a child server: who answered, and what it listed.

    Every way this can fail is one refusal, raised outside the handler so that a
    transport error, a spawn failure or an expired budget is not reachable
    through `__context__` from the exception a CLI prints. `Exception` rather
    than a named set on purpose: the SDK, anyio and the operating system each
    have their own vocabulary for "no server here", and a qualification that
    admitted a cause it had not enumerated would be a qualification that passed
    by accident.
    """
    observed: tuple[str, str | None, tuple[str, ...]] | None = None
    try:
        # **The child's stderr goes to the null device.** It is the one channel
        # that carries a refusal in the child's own words -- a path, a workspace,
        # a reference -- and a setup command must not relay any of it.
        # Discarding it at the descriptor is stronger than capturing it and
        # choosing not to print it: there is then no buffer for a later
        # diagnostic to reach into.
        with Path(os.devnull).open("w", encoding="utf-8") as discarded:
            observed = anyio.run(lambda: _exchange(config_path, discarded))
    except Exception:  # noqa: BLE001 -- an exchange that failed has not qualified.
        observed = None
    if observed is None:
        raise StartupError(_NOT_QUALIFIED)
    return observed


async def _exchange(
    config_path: Path, discarded: TextIO
) -> tuple[str, str | None, tuple[str, ...]]:
    """Spawn, initialize, list, and stop. Bounded twice and shut down once.

    :func:`anyio.fail_after` bounds the whole exchange, and the session carries
    the same budget as its per-request read timeout, so neither a child that
    never answers nor one that answers the handshake and then stops can hold this
    process past :data:`QUALIFICATION_TIMEOUT_SECONDS`. Termination is
    `stdio_client`'s, which closes stdin, waits, and then kills the process tree
    inside a cancellation shield -- so the child is stopped on the expiry path
    exactly as it is on the successful one, and this function holds no process
    object to have to remember to reap.

    `discarded` is where the child's stderr goes -- the null device, opened by
    the caller -- so nothing it says about a path, a workspace or a reference is
    held anywhere this process could later relay it from.

    **The child is told a path and nothing else.** `env` is left `None`, so the
    SDK hands the child its own sanitized default environment: no credential,
    token or bearer appears in the argument vector or in the environment, and the
    child resolves its own from this installation's protected store exactly as it
    does under a host.
    """
    parameters = StdioServerParameters(
        command=sys.executable,
        # `-P` keeps the working directory off the child's `sys.path`: a
        # qualification that imported an `omnivia_core_mcp` somebody left beside
        # the terminal would be qualifying that package instead of this one.
        args=["-P", "-m", _QUALIFICATION_MODULE, "--config", str(config_path)],
    )
    with anyio.fail_after(QUALIFICATION_TIMEOUT_SECONDS):
        async with (
            stdio_client(parameters, errlog=discarded) as (read_stream, write_stream),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=QUALIFICATION_TIMEOUT_SECONDS,
            ) as session,
        ):
            initialized = await session.initialize()
            listed = await session.list_tools()
            return (
                initialized.server_info.name,
                initialized.server_info.version,
                tuple(tool.name for tool in listed.tools),
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnivia-core-mcp",
        description=(
            "Serve one OmniVia Core workspace to an MCP host over stdio. "
            "Read-only, and never creates a workspace."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help=(
            "absolute path to the trusted omnivia.mcp-config.v1 file. It must be "
            "a regular owner-private file, and it is the only place the "
            "principal, the workspace allow-list, the allowed purposes and the "
            "service location come from. There is no default, no environment "
            "variable and no fallback."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Read the trusted configuration, connect, then serve stdio.

    Writes nothing to stdout on any path: the startup line and every diagnostic
    go to stderr and the exit status carries the outcome. That is R004-07's
    protocol-safe failure in its strongest form -- a host reading this process's
    stdout sees either valid MCP or nothing at all.

    **There is no ambient credential resolver here, and that is the design.**
    A console process holds no trusted way to release a secret, so a
    configuration in `service_client` mode fails closed at :func:`connect`
    rather than reaching for an environment variable, an argv secret or a file
    beside the configuration. A host that has a resolver calls :func:`connect`
    and :func:`serve` itself and injects one.

    **The authoring admission is different, and it is built here.** A console
    process holds no protected record of a human's authoring intent -- but an
    installation does, and a managed-local configuration written by the installed
    setup path names the credential that can ask for it. So this entry point
    injects :func:`_installed_admission` when the configuration carries a
    credential reference and nothing when it does not.

    That is also the upgrade boundary. A managed-local configuration from before
    the installed setup path existed carries no reference. This entry point may
    finish an interrupted explicit restricted setup when exactly one matching
    active authority and its protected bearer already exist. It never creates a
    grant or guesses a host; every other legacy state refuses and requires
    ``omnivia mcp configure --host ...``. A document that
    already carries a reference still reaches `authoring` only if the protected
    authority admits exactly this principal and this workspace when asked, on
    this startup -- editing `mutation_enabled` alone raises a ceiling over an
    empty room.
    """
    args = build_parser().parse_args(argv)
    try:
        configuration = read_configuration(args.config)
        configuration = upgrade_legacy_configuration(args.config, configuration)
        session = connect(
            configuration, authoring_admission=_installed_admission(configuration)
        )
    except (
        McpConfigurationError,
        StartupError,
        ManagedStartError,
        ClientError,
    ) as refusal:
        sys.stderr.write(f"{refusal}\n")
        return 1

    sys.stderr.write(f"{SERVER_NAME}: {session.status} {session.workspace_id}\n")
    try:
        anyio.run(lambda: serve(session=session))
    finally:
        session.clear_credentials()
    return 0


if __name__ == "__main__":  # pragma: no cover - module execution shim
    raise SystemExit(main())
