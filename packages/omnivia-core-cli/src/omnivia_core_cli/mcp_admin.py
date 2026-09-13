"""`omnivia mcp configure|status|revoke`: the installed MCP administration family.

R004 section 9.2's required installed experience, and nothing beyond it. A human
who owns this installation configures a dedicated MCP principal for one host and
one workspace, inspects what is configured, and revokes it -- without editing a
secret into a host configuration file, and without any of it being reachable by
a model. Nothing here is in `OPERATION_CATALOGUE`, has a schema, or can be named
by an MCP tool: these are controls, and authority a model could mint is not
authority.

**The explicit installation state root is still the only anchor.** There is no
working-directory search, no home-directory fallback and no environment variable
here, exactly as everywhere else in this CLI. What these three commands do *not*
need is the root `--workspace-id`: they administer an installation's MCP
principals rather than call one workspace's service, and `configure` names the
workspace it binds with its own `--workspace`.

**Every value a caller supplies is from a closed vocabulary.** A host is one of
:data:`HOSTS`, a profile one of :data:`PROFILES`, and a workspace is checked
against the contract's own identifier grammar. There is no scope, capability,
purpose, operation, principal, path, endpoint or credential argument, because
every one of those is the service's to derive or this module's to place: the
rights a profile implies are derived by the authoritative service from the frozen
catalogue, and the protected configuration is written by
:class:`~omnivia_core_client.InstalledConfigStore`, which derives the whole path
from the installation root and a closed host word and takes no path at all.

**Both halves of the local state are written and removed through a protected
store, never through a pathname.** The bearer goes to
:class:`~omnivia_core_client.InstalledCredentialStore` and the configuration to
:class:`~omnivia_core_client.InstalledConfigStore`, and each proves its own way
down -- the installation root, `runtime/`, its own directory -- before it reads,
replaces or unlinks anything, and proves it again afterwards. This module composes
no path it writes to and calls no `unlink` of its own. The one path it does
compute is the *public* one, :func:`configuration_path`, which is what a host's
own configuration names on the MCP server's command line; it is asked of the
store, and it is never a way in.

**The bearer travels from the service to owner-private storage and stops there.**
`configure` receives it once, inside :class:`~omnivia_core_client.McpConfigureResult`,
writes it straight into this installation's
:class:`~omnivia_core_client.InstalledCredentialStore` and drops it. It never
reaches the protected configuration -- which carries the opaque *reference*, the
name the store filed it under -- never reaches the host-native snippet, never
reaches a process argument, and never reaches stdout, stderr or a diagnostic.
`status` and `revoke` have no branch that could produce one.

**Failure is compensated, and compensation is fail-closed.** A failure after the
service has minted authority revokes that authority before removing the local
half, so the outcome is never an active grant nobody can use. A revocation that
was *not* confirmed removes nothing at all: every safe local artifact is left
exactly as it stands, because the grant may still be live and the local half is
the only thing that can present it -- `status` can then show what is there and a
bare `revoke` can invalidate the authority first. The previous setup's bearer
cannot be restored -- it was handed over once and this installation kept only
what it filed -- so a rotation that then fails is not rolled back to it: it is
revoked, and a fixed recoverable sentence says the remedy is to run `configure`
again.

Standard library, the public contracts, the shared client, and the CLI's own
surface. The MCP distribution is deliberately *not* a dependency of this one --
ADR-036 forbids the edge -- so the handshake verification this module must run
before reporting success is an injected seam, resolved at the moment it is needed
from the installed MCP server if that distribution is present. See
:func:`_default_verification`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from omnivia_core_client import (
    CONFIGURATION_STORE_DIRECTORY,
    ClientError,
    Credential,
    CredentialReference,
    Deadline,
    InstallationServiceConfig,
    InstalledConfigStore,
    InstalledCredentialStore,
    LocalControlRefused,
    LocalIpcTransport,
    ManagedStartError,
    McpConfigureResult,
    McpSetupView,
    ServiceClient,
    connect_managed_local,
    local_control_transport,
    mcp_configure,
    mcp_revoke,
    mcp_status,
)

from omnivia_core.contracts.v1 import WORKSPACE_ID_PATTERN

__all__ = [
    "CONFIGURATION_DIRECTORY",
    "CONFIGURATION_FORMAT",
    "DEFAULT_SEAMS",
    "HOSTS",
    "MCP_EXECUTABLE",
    "PROFILES",
    "PROFILE_PURPOSES",
    "STATUS_DOCUMENT_VERSION",
    "Seams",
    "configuration_path",
    "host_snippet",
    "run",
    "workspace_id",
]

#: The hosts this installation knows how to configure, spelled as the service's
#: own `McpHost` spells them. Closed: `--host` is an argparse `choices` list, so
#: anything else is a usage error before a socket is opened.
HOSTS: Final[tuple[str, ...]] = ("claude-code", "codex")

#: The two exposure profiles, spelled as the MCP manifest and the service's
#: `McpProfile` both spell them. `authoring` is the only one that records intent.
PROFILES: Final[tuple[str, ...]] = ("restricted", "authoring")

#: The document family `configure` writes. The existing one: R004 section 9.3
#: keeps `omnivia.mcp-config.v1` as the configuration family and introduces no
#: new profile field.
CONFIGURATION_FORMAT: Final = "omnivia.mcp-config.v1"

#: Where a protected configuration lives, relative to the installation state
#: root, and the whole of the layout this module knows.
#:
#: Re-exported from the client package rather than restated, because the layout
#: is not this module's any more: :class:`~omnivia_core_client.InstalledConfigStore`
#: owns the directory, derives every path in it and is the only thing that writes
#: or removes one. What is kept here is the name, for the callers and tests that
#: already say `mcp_admin.CONFIGURATION_DIRECTORY`.
CONFIGURATION_DIRECTORY: Final[tuple[str, ...]] = CONFIGURATION_STORE_DIRECTORY

#: The purposes each profile's tools declare, sorted, and the whole of what a
#: written configuration allows.
#:
#: Restated here rather than imported for the reason
#: `omnivia_core_runtime.service.installed_mcp` restates the operations it
#: derives rights from: this distribution must not depend on the MCP one. A
#: restatement that drifted would be a configuration allowing a purpose its tools
#: never use, or refusing one they do -- so it is not left to a reader:
#: `verify_installed_setup`, in the MCP package, compares a written
#: configuration's purposes against the running profile's manifest before
#: `configure` may report success, and a drift fails there.
_RESTRICTED_PURPOSES: Final[tuple[str, ...]] = (
    "knowledge_retrieval",
    "workspace_inspection",
)
PROFILE_PURPOSES: Final[Mapping[str, tuple[str, ...]]] = {
    "restricted": _RESTRICTED_PURPOSES,
    "authoring": tuple(
        sorted(
            _RESTRICTED_PURPOSES
            + ("content_ingestion", "job_observation", "memory_authoring")
        )
    ),
}

#: The console script an MCP host launches, and the only executable a host-native
#: snippet ever names. It is the MCP distribution's own entry point.
MCP_EXECUTABLE: Final = "omnivia-core-mcp"

#: Where the default handshake verification comes from when the MCP distribution
#: is installed beside this one. Resolved by name at the moment it is needed --
#: see :func:`_default_verification` -- never imported.
_MCP_SERVER_MODULE: Final = "omnivia_core_mcp.server"
_MCP_VERIFIER: Final = "verify_installed_setup"

#: The descriptor a workspace's service publishes, which is how this module
#: recognises a directory under `runtime/` as a workspace rather than as
#: something else that happens to be there.
_DESCRIPTOR_NAME: Final = "service.json"
_RUNTIME_DIRECTORY: Final = "runtime"

_WORKSPACE_RE: Final = re.compile(WORKSPACE_ID_PATTERN)

#: The health vocabulary `status` reports, and the whole of it. Fixed words
#: rather than reasons: the reasons are all the same sentence -- something local
#: is wrong and the remedy is to run `configure` again -- and distinguishing
#: "wrong owner" from "not a credential" would publish what an attacker changed
#: it to.
_ABSENT: Final = "absent"
_PRESENT: Final = "present"
_UNUSABLE: Final = "unusable"
_MISMATCHED: Final = "mismatched"
_UNKNOWN: Final = "unknown"

#: Every sentence this module prints to stderr. None is built from an argument, a
#: path, a document, an exception, a peer's words or anything a store read.
_NO_SERVICE: Final = "the installation service could not be reached"
_REFUSED: Final = "the installed MCP authority refused the requested change"
_NOT_ADMINISTRATOR: Final = (
    "installed MCP administration requires a local installation administrator"
)
_NOT_PUBLISHED: Final = "the local half of this setup could not be published"
_NOT_VERIFIED: Final = "the configured MCP server did not pass its startup check"
_SPLIT_BRAIN: Final = (
    "this installation's MCP state could not be settled; run configure again"
)
_RECOVERABLE: Final = (
    "the setup failed and its authority could not be revoked; run revoke, then "
    "configure again"
)
_ROLLED_BACK: Final = (
    "the setup failed and its authority was revoked; run configure again"
)
#: The third compensation outcome, and the honest one: the grant is gone, so
#: nothing is live, but a protected store refused a removal and unusable local
#: material is still there. Distinct from :data:`_ROLLED_BACK` because that
#: sentence would claim a cleanup that did not happen, and distinct from
#: :data:`_RECOVERABLE` because there is no authority left to revoke.
_ROLLED_BACK_PARTLY: Final = (
    "the setup failed and its authority was revoked, but unusable local material "
    "could not be removed; run configure again"
)
_NO_VERIFIER: Final = "the installed OmniVia MCP server is not available here"

#: What each refusal code the service can answer with means here, and the status
#: it exits with. Keyed by the client package's own closed vocabulary and never
#: by a word off the wire; an unrecognised code lands on the same entry a plain
#: refusal does, because an unknown refusal is still a refusal.
_REFUSALS: Final[Mapping[str, tuple[str, int]]] = {
    "unauthorized": (_NOT_ADMINISTRATOR, 3),
    "unauthenticated": (_NOT_ADMINISTRATOR, 3),
    "unavailable": (_NO_SERVICE, 1),
}

#: The version of the redacted `status --json` document. It carries no path, no
#: endpoint, no salt, no digest, no grant row and no workspace content -- only
#: identifiers a human needs to recognise what is configured, and fixed health
#: words.
STATUS_DOCUMENT_VERSION: Final = 1


def workspace_id(value: str) -> str:
    """`value` as a contract workspace identifier, or an argparse usage error.

    Checked with the contract's own pattern rather than one restated here, and
    checked during the parse so a malformed identifier is a usage error naming
    the flag instead of a refusal from the middle of a control. The value never
    appears in the refusal: a usage error is exactly what ends up in a shell
    history or a CI log.
    """
    if _WORKSPACE_RE.fullmatch(value) is None or len(value) > 128:
        raise argparse.ArgumentTypeError("must be a well-formed workspace id")
    return value


def configuration_path(installation_state: Path, host: str) -> Path:
    """The one protected configuration path this installation uses for `host`.

    Deterministic and installation-owned: a function of the trusted root and a
    closed vocabulary word, with no caller-supplied component. There is no flag
    that chooses it, because a configuration path a caller could name is a
    configuration this installation did not place.

    Asked of the store rather than composed here, so the path a snippet prints and
    the path the MCP child is given is the same one the store proves its way down
    to. It is the only thing about that store that is public: reading, writing and
    removal all go through it and never through this pathname.
    """
    return InstalledConfigStore(installation_state).path(host)


def host_snippet(host: str, path: Path) -> str:
    """The minimum host-native entry for `host`, naming the command and the path.

    Two members and no third: the installed MCP executable and the absolute path
    to the protected configuration. **No credential**, by construction -- there is
    no parameter here a secret could be passed through -- which is R004 section
    9.2's rule that a host-native entry must never contain the Core credential.

    Printed rather than written. Editing a user's host configuration is not this
    command's to do: the file is theirs, its other servers are theirs, and a
    setup command that rewrote it would be a setup command that could break
    everything else in it.
    """
    if host == "codex":
        command = _toml_basic_string(MCP_EXECUTABLE)
        configuration = _toml_basic_string(str(path))
        return (
            f"[mcp_servers.omnivia-core]\n"
            f"command = {command}\n"
            f'args = ["--config", {configuration}]\n'
        )
    return (
        json.dumps(
            {
                "mcpServers": {
                    "omnivia-core": {
                        "command": MCP_EXECUTABLE,
                        "args": ["--config", str(path)],
                    }
                }
            },
            indent=2,
        )
        + "\n"
    )


def _toml_basic_string(value: str) -> str:
    """Render one TOML basic string without JSON surrogate escapes.

    JSON and TOML share the escapes emitted here for quotes, backslashes and C0
    controls.  ``ensure_ascii=False`` is the important distinction from the
    default JSON renderer: supplementary Unicode remains a real scalar instead
    of becoming a JSON-only UTF-16 surrogate pair, which TOML correctly rejects.
    """
    if any("\ud800" <= character <= "\udfff" for character in value):
        raise ValueError(
            "a Codex configuration path must contain Unicode scalar values"
        )
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


#: How this module reaches the installation-local control endpoint.
#:
#: A callable rather than a connection, so the endpoint is dialled at the moment
#: of the call. `workspace` is the one `configure` binds, or `None` for the two
#: commands that name none and must find a published workspace to reach the
#: installation through. `start` says whether a service that is not running may
#: be started: `configure` and `revoke` must change durable authority and so they
#: start one, while `status` reports and never starts anything.
Control = Callable[..., LocalIpcTransport | None]

#: The MCP-owned handshake and `tools/list` check, returning the advertised tool
#: count and raising for every refusal.
Verification = Callable[[Path], int]


@dataclass(frozen=True, slots=True)
class Seams:
    """The two things this module does not implement and must not fake.

    Injected so the focused tests can exercise every refusal and every
    compensation branch without an installation, a service or an MCP host. The
    defaults are what the console script uses, and they are the production ones.
    """

    control: Control
    verify: Verification


def run(
    arguments: argparse.Namespace, action: str, *, seams: Seams | None = None
) -> int:
    """Run one installed-MCP administration command and return the exit status."""
    resolved = seams if seams is not None else DEFAULT_SEAMS
    deadline = Deadline.after_ms(arguments.timeout_ms)
    state: Path = arguments.installation_state
    try:
        if action == "configure":
            return _configure(state, arguments, deadline=deadline, seams=resolved)
        if action == "revoke":
            return _revoke(state, arguments, deadline=deadline, seams=resolved)
        return _status(state, arguments, deadline=deadline, seams=resolved)
    except _Refused as refusal:
        return _refuse(refusal.diagnostic, refusal.status)


class _Refused(Exception):
    """One fixed sentence and the status it exits with. Carries nothing else."""

    def __init__(self, diagnostic: str, status: int) -> None:
        self.diagnostic = diagnostic
        self.status = status
        super().__init__(diagnostic)


# --- configure ----------------------------------------------------------------


def _configure(
    state: Path, arguments: argparse.Namespace, *, deadline: Deadline, seams: Seams
) -> int:
    """Provision or re-provision one host, and publish the local half of it.

    The order is the one R004 section 9.2 fixes and the one compensation
    requires. Authority first, at the authoritative service, which is the only
    process that may mint a principal or a bearer and the only one that knows
    whether the requested state is already live. Then the bearer into
    owner-private storage, immediately, because it is handed over exactly once.
    Then the protected configuration, atomically. Then the handshake, against the
    file that was just written and through the real server entry point. Only then
    is it a success, and only then is a snippet printed.

    **A configure that changes nothing prints the same snippet.** It is still a
    successful configure: the requested state is live, the local half is healthy,
    and the caller asked what to put in their host configuration.
    """
    host: str = arguments.host
    profile: str = arguments.profile
    workspace: str = arguments.workspace
    config = InstalledConfigStore(state)
    path = config.path(host)
    store = InstalledCredentialStore(state)
    transport = _reach(state, workspace, start=True, deadline=deadline, seams=seams)

    result = _provision(transport, host, workspace, profile, deadline=deadline)
    document = _document(state, workspace, profile, result.setup)
    if not result.rotated:
        if _settled(store, config, host, result.setup, document):
            _verify(seams, path)
            return _report(host, path)
        # The service holds the requested state and this installation's half of
        # it is missing, superseded or unsafe. Nothing can re-derive the bearer
        # that setup was issued with -- it was handed over once -- so the only
        # honest repair is a fresh one: invalidate, drop what is local, provision
        # again. Invalidation comes first for the reason it always does here: a
        # local half removed under a live grant is the state that must never
        # exist, even for the microsecond between two lines.
        superseded = _references(config, host, result.setup)
        if not _revoke_quietly(transport, host, deadline=deadline):
            raise _Refused(_RECOVERABLE, 1)
        _discard(store, config, superseded, host)
        result = _provision(transport, host, workspace, profile, deadline=deadline)
        document = _document(state, workspace, profile, result.setup)
        if not result.rotated:
            raise _Refused(_SPLIT_BRAIN, 1)

    secret = result.reveal()
    if secret is None:  # pragma: no cover - the client model forbids it
        raise _Refused(_SPLIT_BRAIN, 1)
    superseded = _references(config, host, None) - {result.setup.credential_reference}
    _publish(
        store,
        config,
        result.setup,
        document,
        secret,
        # Everything this host has material for, so a compensation removes the
        # superseded bearer too. The rotation already made it useless -- the
        # service overwrote the verifier -- and a compensation that removed only
        # the new one would leave dead material behind under a configuration it
        # has just deleted.
        material=superseded | {result.setup.credential_reference},
        transport=transport,
        host=host,
        deadline=deadline,
        seams=seams,
    )
    _discard(store, config, superseded, None)
    return _report(host, path)


def _provision(
    transport: LocalIpcTransport,
    host: str,
    workspace: str,
    profile: str,
    *,
    deadline: Deadline,
) -> McpConfigureResult:
    """One `mcp.configure` control, with this module's sentence for its refusals.

    The client's own diagnostics are already payload-free -- it looks each one up
    by code and never carries a word the peer wrote -- and they are still not
    re-raised. What a CLI prints is its own vocabulary about its own command, and
    translating here is what keeps the authorisation refusal separable: an
    owner/administrator check that failed is exit 3, like every other
    authorisation failure this CLI reports, and not exit 1.

    Raised after the handler, never inside one, which is the convention the
    unauthenticated seams below keep: a `LocalControlRefused` reachable through
    `__context__` is one exception-attribute access away from whatever produced
    it, and this family's exchanges carry bearers.
    """
    result: McpConfigureResult | None = None
    code = ""
    try:
        result = mcp_configure(
            transport,
            host=host,
            workspace_id=workspace,
            profile=profile,
            # The separate explicit act R004 section 9.3 requires. It is derived
            # from the profile the caller chose rather than taken as a fourth
            # flag, because choosing `--profile authoring` *is* the explicit
            # intent: there is no way to reach this line without having typed it,
            # and a second flag would only be a second way to say the same thing.
            authoring_intent=profile == "authoring",
            deadline=deadline,
        )
    except LocalControlRefused as refusal:
        code = refusal.code
    except (ClientError, OSError):
        code = "unavailable"
    if result is not None:
        return result
    diagnostic, status = _REFUSALS.get(code, (_REFUSED, 1))
    raise _Refused(diagnostic, status)


def _publish(
    store: InstalledCredentialStore,
    config: InstalledConfigStore,
    setup: McpSetupView,
    document: Mapping[str, Any],
    secret: str,
    *,
    material: set[str],
    transport: LocalIpcTransport,
    host: str,
    deadline: Deadline,
    seams: Seams,
) -> None:
    """Put the bearer, then the configuration, then prove the two start a server.

    The configuration goes through the protected store rather than the generic
    owner-private writer: the same proved walk the bearer took, refusing a
    substituted `runtime/`, a substituted store directory or a parent swapped under
    the write, rather than a pathname this module composed and handed to a writer
    that can only prove the last component of it.

    Every failure below lands in the same place: :func:`_compensate`, which
    invalidates the authority that was just minted *before* it removes anything
    local, and which removes nothing at all when that invalidation was not
    confirmed. That ordering is the whole point -- a local half removed first
    would leave a live grant this installation can no longer present, which is
    exactly the state R004 section 9.2 forbids.

    It applies identically to all three failure points below, including the first
    one, where the bearer never reached the store and there is no new local half
    to remove. What there may be is the *previous* setup's, and it is not deleted
    under an authority that may still be live merely because the thing replacing
    it never arrived. It is not restored either: the rotation already handed this
    installation's only copy of the new bearer over, and the old one was handed
    over exactly once, so there is nothing to put back and nothing to undo.
    """
    reference = _reference(setup.credential_reference)
    if reference is None:
        _compensate(store, config, material, host, transport, deadline, _NOT_PUBLISHED)
    else:
        stored = True
        try:
            store.store(reference, Credential(secret))
        except ClientError:
            stored = False
        if not stored:
            # Outside the handler: a `ClientError` reachable through
            # `__context__` is the store's own refusal about a file holding a
            # bearer.
            _compensate(
                store, config, material, host, transport, deadline, _NOT_PUBLISHED
            )
    if not config.write(host, _encoded(document)):
        _compensate(store, config, material, host, transport, deadline, _NOT_PUBLISHED)
    verified = True
    reason = _NOT_VERIFIED
    try:
        seams.verify(config.path(host))
    except _Refused as refusal:
        verified, reason = False, refusal.diagnostic
    except Exception:  # noqa: BLE001 -- a startup that failed has not verified.
        verified = False
    if not verified:
        _compensate(store, config, material, host, transport, deadline, reason)


def _compensate(
    store: InstalledCredentialStore,
    config: InstalledConfigStore,
    references: set[str],
    host: str,
    transport: LocalIpcTransport,
    deadline: Deadline,
    diagnostic: str,
) -> None:
    """Invalidate the authority, and only *then* drop the local half. Never both.

    Fail-closed, and strictly in that order. The previous setup cannot be
    restored: its bearer was handed over once and this installation kept only
    what it filed, so there is nothing left to put back. What is left is the
    choice between an active grant nobody holds a usable credential for and no
    grant at all, and R004 section 9.2 answers it -- never leave the first.

    **A revocation that was not confirmed removes nothing.** The service may
    still hold the grant, and the local half is the only thing that can present
    it, so every safe artifact stays exactly as it stands -- including a
    pre-existing one this configure was about to supersede. Deleting it here
    would be the forbidden state in the other direction: a live grant with no
    recoverable configuration, and no way for `revoke` to be told which
    references to invalidate. The recoverable sentence names the order that
    settles it instead, and `status` reports what is there until it is run.

    Two fixed sentences reach stderr: what failed, then what state that leaves.
    The state sentence says what is true -- rolled back, rolled back with local
    material a protected store would not remove, or not revoked at all -- and
    never claims a cleanup that did not happen. Either way the status is non-zero
    and nothing has been printed to stdout.
    """
    sys.stderr.write(diagnostic + "\n")
    if not _revoke_quietly(transport, host, deadline=deadline):
        raise _Refused(_RECOVERABLE, 1)
    cleared = _discard(store, config, references, host)
    raise _Refused(_ROLLED_BACK if cleared else _ROLLED_BACK_PARTLY, 1)


def _report(host: str, path: Path) -> int:
    """Print the host-native snippet for a successful configure. Exit 0."""
    sys.stdout.write(host_snippet(host, path))
    return 0


# --- status -------------------------------------------------------------------


def _status(
    state: Path, arguments: argparse.Namespace, *, deadline: Deadline, seams: Seams
) -> int:
    """Report every configured host, or one, as stable redacted state.

    **Reporting is not a reason to bring an installation up.** The control
    endpoint is dialled without starting anything, and a service that is not
    running is itself part of what is being reported: what cannot be answered
    without one -- whether a grant is live, what the server would advertise -- is
    `unknown` or absent rather than guessed. The advertised tool count is asked
    of the installed MCP server, which starts a short-lived server process that
    attaches to the service this command has already found reachable, and it is
    not asked at all when it is not.

    Nothing printed here is a credential, a raw grant, a salt, a digest, a path,
    an endpoint, a peer's words or any workspace content. Every non-identifier
    value is a word from this module's fixed health vocabulary.
    """
    hosts = HOSTS if arguments.host is None else (arguments.host,)
    config = InstalledConfigStore(state)
    transport = _reach_quietly(state, None, start=False, deadline=deadline, seams=seams)
    setups: tuple[McpSetupView, ...] = ()
    reachable = transport is not None
    if transport is not None:
        try:
            setups = mcp_status(transport, deadline=deadline).setups
        except (ClientError, OSError):
            reachable = False
    rows = [
        _row(
            state,
            config,
            host,
            next((entry for entry in setups if entry.host == host), None),
            seams,
            reachable=reachable,
        )
        for host in hosts
    ]
    if arguments.json:
        sys.stdout.write(
            json.dumps(
                {"mcp_status_version": STATUS_DOCUMENT_VERSION, "hosts": rows},
                sort_keys=True,
            )
            + "\n"
        )
    else:
        for row in rows:
            sys.stdout.write(
                " ".join(f"{name}={row[name]}" for name in sorted(row)) + "\n"
            )
    return 0 if reachable else 1


def _row(
    state: Path,
    config: InstalledConfigStore,
    host: str,
    setup: McpSetupView | None,
    seams: Seams,
    *,
    reachable: bool,
) -> dict[str, Any]:
    """One host's redacted view, from durable service state and the local half."""
    stored = _stored(config, host)
    configuration = _configuration_health(config, host, stored, setup, state)
    reference = _reference(
        setup.credential_reference
        if setup is not None
        else _member(stored, "credential_reference")
    )
    credential = _ABSENT
    if reference is not None:
        try:
            credential = InstalledCredentialStore(state).health(reference)
        except ClientError:
            credential = _UNUSABLE
    tools: int | None = None
    if reachable and configuration == _PRESENT:
        try:
            tools = seams.verify(config.path(host))
        except Exception:  # noqa: BLE001 -- a startup that failed advertises none.
            tools = None
    return {
        "advertised_tool_count": tools,
        "authoring_intent": None if setup is None else setup.authoring_intent,
        "configuration": configuration,
        "credential": credential,
        "grant": (
            _UNKNOWN if not reachable else (_ABSENT if setup is None else setup.status)
        ),
        "host": host,
        "principal_id": (
            setup.principal_id if setup is not None else _member(stored, "principal_id")
        ),
        "profile": _profile(stored, setup),
        "service": "reachable" if reachable else "unreachable",
        "workspace_id": (
            setup.workspace_id
            if setup is not None
            else _member(stored, "default_workspace_id")
        ),
    }


def _profile(
    stored: Mapping[str, Any] | None, setup: McpSetupView | None
) -> str | None:
    """The profile in force, from the service if it answered and the file if not.

    The file has no profile member and must not grow one: R004 section 9.3 says
    no new unversioned profile field is introduced. What it has is
    `mutation_enabled`, which this module writes from the profile and nothing
    else, so reading it back is reading the ceiling rather than inventing a
    field.
    """
    if setup is not None:
        return setup.profile
    if stored is None:
        return None
    return "authoring" if stored.get("mutation_enabled") is True else "restricted"


def _configuration_health(
    config: InstalledConfigStore,
    host: str,
    stored: Mapping[str, Any] | None,
    setup: McpSetupView | None,
    state: Path,
) -> str:
    """`absent`, `unusable`, `mismatched` or `present`, and nothing else.

    "Nothing there" and "something there this installation will not read" are the
    store's own two answers rather than an `exists()` on a pathname this module
    composed: a file behind a substituted store directory is not absent, and
    reporting it as absent would say a host is unconfigured when what is true is
    that something local is wrong.
    """
    if stored is None:
        return _UNUSABLE if config.health(host) != _ABSENT else _ABSENT
    if setup is None:
        return _PRESENT
    expected = _document(state, setup.workspace_id, setup.profile, setup)
    return _PRESENT if stored == expected else _MISMATCHED


# --- revoke -------------------------------------------------------------------


def _revoke(
    state: Path, arguments: argparse.Namespace, *, deadline: Deadline, seams: Seams
) -> int:
    """Invalidate authority first, then drop the local half. Idempotent.

    Every host by default, because that is what "revoke this installation's MCP
    access" means and because a host left configured by an omission is the one
    outcome a revoke must not have. A closed `--host` narrows it.

    A service is started if one is not running. Removing the local half while the
    grant is still live would be the same forbidden state a failed configure must
    not leave, and the only process that can invalidate a grant is the
    authoritative one.

    Workspace data, audits, committed mutations and service-owned jobs are
    untouched: nothing below names any of them, and the control this calls
    revokes a principal rather than deleting anything.
    """
    hosts = HOSTS if arguments.host is None else (arguments.host,)
    transport = _reach(state, None, start=True, deadline=deadline, seams=seams)
    store = InstalledCredentialStore(state)
    config = InstalledConfigStore(state)
    failed = False
    for host in hosts:
        outcome = None
        answered = True
        try:
            outcome = mcp_revoke(transport, host=host, deadline=deadline)
        except (ClientError, OSError):
            answered = False
        if not answered:
            # The local half stays exactly where it is. Removing it here would
            # leave a live grant with no way to present it, which is the state
            # this command exists to prevent rather than to create.
            failed = True
            continue
        _discard(
            store,
            config,
            _references(config, host, None if outcome is None else outcome.setup),
            host,
        )
        sys.stdout.write(f"revoked {host}\n")
    return 1 if failed else 0


# --- reaching the installation ------------------------------------------------


def _reach(
    state: Path,
    workspace: str | None,
    *,
    start: bool,
    deadline: Deadline,
    seams: Seams,
) -> LocalIpcTransport:
    """The control endpoint, or this module's one fixed sentence about not having it."""
    transport = _reach_quietly(
        state, workspace, start=start, deadline=deadline, seams=seams
    )
    if transport is None:
        raise _Refused(_NO_SERVICE, 1)
    return transport


def _reach_quietly(
    state: Path,
    workspace: str | None,
    *,
    start: bool,
    deadline: Deadline,
    seams: Seams,
) -> LocalIpcTransport | None:
    transport = None
    try:
        transport = seams.control(state, workspace, start=start, deadline=deadline)
    except (ClientError, ManagedStartError, OSError, ValueError):
        transport = None
    return transport


def _default_control(
    state: Path,
    workspace: str | None,
    *,
    start: bool,
    deadline: Deadline,
) -> LocalIpcTransport | None:
    """Dial the installation-local endpoint of one of this installation's services.

    **Any workspace service will do, and that is a property of the endpoint
    rather than a shortcut.** An administration control is answered by the
    process holding the installation catalogue; a service that is not that
    process forwards the identical document to the one that is. So `status` and
    `revoke`, which name no workspace, reach the installation through whichever
    workspace this installation has published a descriptor for.

    The candidates come from the trusted root and nothing else: the directories
    under `runtime/` that carry a published service descriptor and are named by a
    well-formed workspace identifier. There is no working-directory search, no
    home fallback and no environment variable, here or anywhere else in this CLI.
    """
    candidates = (workspace,) if workspace is not None else _published_workspaces(state)
    for candidate in candidates:
        config = InstallationServiceConfig(
            installation_state=state, workspace_id=candidate
        )
        client: ServiceClient | None = None
        try:
            client = (
                connect_managed_local(config, deadline=deadline).client
                if start
                else ServiceClient.connect(config, deadline=deadline)
            )
        except (ClientError, ManagedStartError, OSError):
            # One candidate that will not come up is not the installation being
            # unreachable: another published workspace answers the same
            # administration control, because a service that is not the one
            # holding the catalogue forwards it to the one that is.
            client = None
        if client is not None:
            return local_control_transport(client)
    return None


def _published_workspaces(state: Path) -> tuple[str, ...]:
    """Every workspace under this root that has published a service descriptor."""
    runtime = state / _RUNTIME_DIRECTORY
    found: list[str] = []
    try:
        entries = sorted(entry.name for entry in runtime.iterdir() if entry.is_dir())
    except OSError:
        return ()
    for name in entries:
        if _WORKSPACE_RE.fullmatch(name) is None or len(name) > 128:
            continue
        if (runtime / name / _DESCRIPTOR_NAME).is_file():
            found.append(name)
    return tuple(found)


def _default_verification(path: Path) -> int:
    """The installed MCP server's own startup check, resolved when it is needed.

    ADR-036 forbids `omnivia-core-cli` depending on or importing
    `omnivia-core-mcp`, and that boundary is right: this CLI must be usable by
    somebody who has never installed an MCP server. But an installed *MCP setup*
    plainly cannot be verified without one, and R004 section 9.2 step 7 requires
    that verification before `configure` reports success.

    So the MCP distribution is an optional component of this command rather than
    a dependency of this distribution: it is looked up by name at the moment it
    is needed, there is no import of it anywhere in this package, and an
    installation without it gets one fixed sentence saying so rather than a
    traceback. What is looked up is a single public function that owns the whole
    check -- it runs the MCP server as a child process and completes a real
    `initialize` and `tools/list` exchange with it, then judges the identity, the
    advertised inventory and the configured purposes -- so nothing about the MCP
    protocol, the manifest or the exposure rules is restated on this side of the
    boundary. It is given a path and nothing else; the credential it presents is
    the one it reads from this installation's protected store for itself.
    """
    verifier: Verification | None = None
    try:
        verifier = getattr(importlib.import_module(_MCP_SERVER_MODULE), _MCP_VERIFIER)
    except (ImportError, AttributeError):
        verifier = None
    if verifier is None:
        # Outside the handler: an `ImportError` reachable through `__context__`
        # names a module path, and every sentence this module prints is fixed.
        raise _Refused(_NO_VERIFIER, 1)
    return verifier(path)


def _verify(seams: Seams, path: Path) -> int:
    """The handshake check with this module's refusal for every way it can fail.

    A :class:`_Refused` the seam raised is already one of this module's own fixed
    sentences -- the absent MCP distribution is the one that reaches here -- so it
    is re-raised bare rather than collapsed into the generic one. A bare re-raise
    carries no new exception and chains nothing.
    """
    verified = -1
    try:
        verified = seams.verify(path)
    except _Refused:
        raise
    except Exception:  # noqa: BLE001 -- a startup that failed has not verified.
        verified = -1
    if verified < 0:
        raise _Refused(_NOT_VERIFIED, 1)
    return verified


# --- the local half -----------------------------------------------------------


def _document(
    state: Path, workspace: str, profile: str, setup: McpSetupView
) -> dict[str, Any]:
    """The exact `omnivia.mcp-config.v1` this installation writes for one setup.

    Every member is fixed by the setup the service settled or by the profile, and
    none by anything a caller typed beyond the closed host, profile and
    workspace. In particular `credential_reference` is the opaque *name* the
    service filed the bearer under -- never the bearer -- and `mutation_enabled`
    is the ceiling R004 section 9.3 makes it: true for `authoring` and false
    otherwise, and never on its own an authorisation, because the MCP server
    still asks the protected authority whether this principal may author.

    Deterministic, so a second `configure` over an unchanged setup produces the
    same bytes and the comparison in :func:`_settled` means what it says.
    """
    return {
        "allowed_purposes": list(PROFILE_PURPOSES[profile]),
        "allowed_workspace_ids": [workspace],
        "credential_reference": setup.credential_reference,
        "default_workspace_id": workspace,
        "format": CONFIGURATION_FORMAT,
        "installation_state": str(state),
        "mutation_enabled": profile == "authoring",
        "principal_id": setup.principal_id,
        "service_mode": "managed_local",
    }


def _encoded(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _stored(config: InstalledConfigStore, host: str) -> dict[str, Any] | None:
    """The configuration this installation holds for `host`, or `None`.

    `None` for every reason there might not be one: absent, not owner-private,
    not a regular file, a symlink or reparse point, reached only through a store
    directory this installation would not descend into, longer than the bound, not
    UTF-8, not JSON, or not a JSON object. The first several of those are the
    store's, in the one package that owns them -- and the proof the store applies
    to the file itself is the proof the MCP server applies before it will start on
    it, so what this module calls healthy is what that server will accept.
    """
    content = config.read(host)
    if content is None:
        return None
    document: object = None
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        document = None
    return document if isinstance(document, dict) else None


def _member(stored: Mapping[str, Any] | None, name: str) -> str | None:
    if stored is None:
        return None
    value = stored.get(name)
    return value if isinstance(value, str) else None


def _settled(
    store: InstalledCredentialStore,
    config: InstalledConfigStore,
    host: str,
    setup: McpSetupView,
    document: Mapping[str, Any],
) -> bool:
    """Whether the whole requested state -- service *and* local half -- is live.

    Three conditions, all required. The service's row is active. The
    configuration on disk is byte-for-byte the document this installation would
    write for that row, read back through the owner-private proof, so a
    hand-edited, group-readable or superseded file is not settled. And this
    installation holds a usable credential under the reference that row names.

    Anything less is not reported as success: a setup whose halves disagree is
    the split-brain state R004 section 9.2 forbids reporting, and the repair is a
    rotation.
    """
    reference = _reference(setup.credential_reference)
    if setup.status != "active" or reference is None:
        return False
    if _stored(config, host) != dict(document):
        return False
    healthy = False
    try:
        healthy = store.health(reference) == _PRESENT
    except ClientError:
        healthy = False
    return healthy


def _references(
    config: InstalledConfigStore, host: str, setup: McpSetupView | None
) -> set[str]:
    """Every credential reference this installation associates with one host."""
    names = {_member(_stored(config, host), "credential_reference")}
    if setup is not None:
        names.add(setup.credential_reference)
    return {name for name in names if name is not None}


def _reference(value: str | None) -> CredentialReference | None:
    reference = None
    try:
        reference = None if value is None else CredentialReference(value)
    except ClientError:
        reference = None
    return reference


def _discard(
    store: InstalledCredentialStore,
    config: InstalledConfigStore,
    references: set[str],
    host: str | None,
) -> bool:
    """Remove the local half: the named credentials, then the configuration.

    `host` is `None` for the one caller that must remove superseded material and
    keep the configuration it has just written.

    Both removals go through their protected store, so neither deletes through an
    unproved chain: a substituted `runtime/` or store directory leaves the removal
    unperformed rather than unlinking whatever stands at the end of it.

    Best effort by design, and it answers whether the effort succeeded. Every
    caller has already invalidated the authority these name, so a file that
    cannot be removed is an unusable leftover rather than an access anybody has
    -- and refusing here would turn a completed revocation into a reported
    failure. What the answer is for is the one caller that must not describe such
    a leftover as a clean rollback.
    """
    cleared = True
    for value in references:
        reference = _reference(value)
        if reference is None:
            continue
        try:
            store.remove(reference)
        except ClientError:
            cleared = False
    if host is not None and not config.remove(host):
        cleared = False
    return cleared


def _revoke_quietly(
    transport: LocalIpcTransport, host: str, *, deadline: Deadline
) -> bool:
    """Invalidate one host's authority, reporting only whether it is now gone."""
    revoked = True
    try:
        mcp_revoke(transport, host=host, deadline=deadline)
    except (ClientError, OSError):
        revoked = False
    return revoked


# --- refusals -----------------------------------------------------------------


def _refuse(diagnostic: str, status: int) -> int:
    """Write one fixed sentence to stderr and return `status`. Never stdout."""
    sys.stderr.write(diagnostic + "\n")
    return status


#: The production wiring, and what the console script runs with. Declared after
#: both halves so it names the functions rather than forward-references them.
DEFAULT_SEAMS: Final = Seams(control=_default_control, verify=_default_verification)
