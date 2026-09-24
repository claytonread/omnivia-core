"""`verify_installed_setup`: the real protocol qualification a setup must pass.

R004 section 9.2 step 7 requires an installed setup to validate a service
handshake and the `tools/list` result before it reports success. That check is
this package's, because every part of it is: what a trusted configuration is,
what this server's identity is, and which tools each profile advertises. The
installed administration command calls this and holds none of it.

**What is asserted here is that it is a protocol exchange with a real child.**
The qualifications below spawn this package's own entry point as a subprocess,
drive it with the official SDK's `stdio_client` and `ClientSession`, and read the
identity and the inventory off the wire. The installation underneath is real too:
`_mcp_v06_3_fixture` serves a governed workspace, the credential is filed in this
installation's protected store, and the child resolves it for itself. Nothing in
this module stands in for the server, the transport or the handshake.

**The authoring inventory is the one thing this module cannot reach over the
wire.** A child settles on `authoring` only when the *service* answers that a
human recorded authoring intent for exactly its principal and workspace, and that
row is written by `mcp.configure`. The credential this module files is its own:
:data:`SECRET`, put straight into the protected store under :data:`PRINCIPAL`,
which no `mcp.configure` ever issued and which therefore no protected authoring
record names. The fifteen are so asserted at the seam where the wire's answer
arrives -- what `_qualification` returned -- and the *restricted* half of the
same rule is proved live, by a child that really does ask the protected authority
and really is told no. The live authoring path is proved end to end in
`test_mcp_standalone_authoring_acceptance`, which runs the real `omnivia mcp
configure` and gets a child that really is admitted to the wider profile.

The other injected tests are the two things a live installation cannot show: a
peer answering as something other than this build, and what the child is told on
its command line. Both stay at a seam rather than growing a second server.
"""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import _mcp_v06_3_fixture as fixture
import pytest
from omnivia_core_client import (
    Credential,
    CredentialReference,
    InstalledCredentialStore,
    write_owner_private,
)
from omnivia_core_mcp import server
from omnivia_core_mcp.configuration import McpConfigurationError, read_configuration
from omnivia_core_mcp.manifest import exposure_manifest

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="the local IPC transport dials AF_UNIX; Windows pipes are a successor",
)

PRINCIPAL = "mcp-verify-principal"
REFERENCE = "omcp-verify-0001"
SECRET = "omcp_live_0f1e2d3c4b5a69788796a5b4c3d2e1f0"

#: A name this installation has never filed anything under. Well-formed, so it is
#: the *store* that cannot answer for it rather than the grammar.
ABSENT_REFERENCE = "omcp-never-filed-0001"

RESTRICTED_PURPOSES = sorted(
    {exposed.purpose for exposed in exposure_manifest("restricted")}
)
AUTHORING_PURPOSES = sorted(
    {exposed.purpose for exposed in exposure_manifest("authoring")}
)

RESTRICTED_TOOLS = tuple(tool.name for tool in server.tools("restricted"))
AUTHORING_TOOLS = tuple(tool.name for tool in server.tools("authoring"))


@dataclass(frozen=True)
class Installed:
    """A live installation with one dedicated MCP credential filed in its store."""

    state: Path
    workspace_id: str
    directory: Path

    def write(self, name: str = "host.json", **overrides: Any) -> Path:
        """One owner-private `omnivia.mcp-config.v1` naming that credential.

        The same members `omnivia mcp configure` writes, in the same shape,
        through the same owner-private writer -- so a document this qualification
        refuses is a document that command will not publish.
        """
        document: dict[str, Any] = {
            "allowed_purposes": list(RESTRICTED_PURPOSES),
            "allowed_workspace_ids": [self.workspace_id],
            "credential_reference": REFERENCE,
            "default_workspace_id": self.workspace_id,
            "format": "omnivia.mcp-config.v1",
            "installation_state": str(self.state),
            "mutation_enabled": False,
            "principal_id": PRINCIPAL,
            "service_mode": "managed_local",
        }
        document.update(overrides)
        for absent in [key for key, value in overrides.items() if value is None]:
            document.pop(absent, None)
        path = self.directory / name
        assert write_owner_private(
            path, (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
        )
        return path


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Installed]:
    """One governed workspace, owned by a real service, for the whole module.

    Module-scoped because every qualification below attaches to it rather than
    changing it: a child that starts here reads a published descriptor and calls
    nothing, so no test leaves the workspace different for the next.
    """
    with fixture.serving() as service:
        InstalledCredentialStore(service.installation_state).store(
            CredentialReference(REFERENCE), Credential(SECRET)
        )
        yield Installed(
            state=service.installation_state,
            workspace_id=service.workspace_id,
            directory=tmp_path_factory.mktemp("installed-mcp"),
        )


# --- the exact inventory ------------------------------------------------------


def test_the_expected_inventory_is_the_requirement_s_own_two_numbers() -> None:
    """R004 section 9.2 fixes 10 and 15; this is where a manifest drift is caught."""
    assert server.EXPECTED_TOOL_COUNT == {"restricted": 10, "authoring": 15}


def test_the_qualification_budget_outlasts_the_child_s_own_startup() -> None:
    """A cold start must not read as an unqualified server.

    The child spends its whole managed-start budget before it writes one protocol
    byte, so a qualification bounded at or below that budget would expire during
    a legitimate start and fail the setup for being slow.
    """
    assert server.QUALIFICATION_TIMEOUT_SECONDS > server.MANAGED_START_TIMEOUT_SECONDS


def test_a_restricted_setup_qualifies_over_real_mcp_and_reports_ten_tools(
    installed: Installed,
) -> None:
    """A real child, a real handshake, and the ten the manifest advertises.

    Nothing here is in this process: `verify_installed_setup` spawns the entry
    point an MCP host would launch, completes `initialize` and `tools/list` over
    real pipes, and the count comes back off the wire.
    """
    assert server.verify_installed_setup(installed.write()) == 10


def test_an_authoring_inventory_qualifies_and_reports_fifteen_tools(
    installed: Installed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wider profile, at the seam where the wire's answer arrives.

    See the module docstring: no configuration this module can write earns an
    `authoring` child from the live fixture, because the admission is the
    service's and the fixture's workspace is not in its authorised inventory.
    What is still proved here is everything the qualification does with such an
    answer -- the inventory is recognised as `authoring`'s own, exactly and in
    order, and the document's purposes are required to be that profile's own.
    """
    monkeypatch.setattr(
        server,
        "_qualification",
        lambda _path: (server.SERVER_NAME, server.__version__, AUTHORING_TOOLS),
    )
    path = installed.write(
        name="authoring.json",
        mutation_enabled=True,
        allowed_purposes=list(AUTHORING_PURPOSES),
    )
    assert server.verify_installed_setup(path) == 15


def test_an_authoring_ceiling_the_protected_authority_will_not_raise_is_refused(
    installed: Installed,
) -> None:
    """`mutation_enabled: true` alone is a ceiling over an empty room -- live.

    The child really does ask the protected authority, over the session's own
    endpoint, with the bearer this installation filed, and is really told no. So
    it settles on `restricted`, advertises ten tools and five purposes -- and this
    configuration allows eight. A setup that published it would advertise a
    surface whose purposes its own calls would be refused for.
    """
    path = installed.write(
        name="raised-ceiling.json",
        mutation_enabled=True,
        allowed_purposes=list(AUTHORING_PURPOSES),
    )
    with pytest.raises(server.StartupError, match="exposure"):
        server.verify_installed_setup(path)


@pytest.mark.parametrize(
    ("name", "purposes"),
    [
        ("too-many.json", [*RESTRICTED_PURPOSES, "workspace_administration"]),
        ("too-few.json", ["workspace_inspection"]),
    ],
    ids=["one-extra", "one-missing"],
)
def test_a_purpose_list_that_is_not_the_running_profile_s_is_refused(
    installed: Installed, name: str, purposes: list[str]
) -> None:
    """The drift guard on the purposes the installed command restates.

    That command cannot import this package, so it holds its own copy of the
    profile purpose table. This is what makes a copy that drifted fail at setup
    rather than at a call months later -- in both directions: a document allowing
    a purpose nothing advertised will use, and one withholding a purpose an
    advertised call needs.
    """
    path = installed.write(name=name, allowed_purposes=list(purposes))
    with pytest.raises(server.StartupError, match="exposure"):
        server.verify_installed_setup(path)


# --- what never reaches a qualified server ------------------------------------


def test_a_configuration_that_is_not_owner_private_is_refused(
    installed: Installed,
) -> None:
    """Refused by the reader, before a child exists to be told a path."""
    path = installed.write(name="world-readable.json")
    path.chmod(0o644)
    with pytest.raises(McpConfigurationError):
        server.verify_installed_setup(path)


@pytest.mark.parametrize(
    ("name", "overrides"),
    [
        ("absent-credential.json", {"credential_reference": ABSENT_REFERENCE}),
        (
            "ambiguous.json",
            {
                "allowed_workspace_ids": ["ws-verify-other-01"],
                "default_workspace_id": None,
            },
        ),
    ],
    ids=["absent-credential", "no-unambiguous-workspace"],
)
def test_a_configuration_the_server_will_not_start_on_does_not_qualify(
    installed: Installed, name: str, overrides: dict[str, Any]
) -> None:
    """Every startup refusal is one qualification failure, and says nothing more.

    The child refuses in its own words on its own stderr -- nothing filed under
    the reference it carries, or no unambiguous workspace to select -- and that
    stderr is discarded. What the setup
    command is told is that no exchange completed, which is the only thing that
    can be said without relaying a path, a reference or a workspace.
    """
    path = installed.write(name=name, **overrides)
    with pytest.raises(server.StartupError, match="did not complete"):
        server.verify_installed_setup(path)


@pytest.mark.parametrize("legacy_mutation", [False, True])
def test_a_legacy_configuration_is_upgraded_to_restricted_before_qualification(
    installed: Installed, legacy_mutation: bool
) -> None:
    """The real child migrates the old document before its MCP handshake.

    The live installation already holds the restricted setup provisioned by the
    fixture. Startup reuses that authority, never treats a legacy true byte as
    authoring consent, and publishes the dedicated principal and reference into
    the same owner-private file. Qualification then observes the restricted ten.
    """
    path = installed.write(
        name=f"legacy-{legacy_mutation}.json",
        credential_reference=None,
        mutation_enabled=legacy_mutation,
    )

    assert server.verify_installed_setup(path) == 10

    upgraded = read_configuration(path)
    assert upgraded.credential_reference is not None
    assert upgraded.principal_id != PRINCIPAL
    assert upgraded.mutation_enabled is False
    assert upgraded.allowed_workspace_ids == (installed.workspace_id,)


def test_no_refusal_quotes_the_path_the_workspace_or_the_bearer(
    installed: Installed,
) -> None:
    """The redaction rule, on the refusal a failed child produces."""
    path = installed.write(name="redaction.json", credential_reference=ABSENT_REFERENCE)
    with pytest.raises(server.StartupError) as refusal:
        server.verify_installed_setup(path)
    rendered = str(refusal.value)
    assert SECRET not in rendered
    assert str(path) not in rendered
    assert str(installed.state) not in rendered
    assert installed.workspace_id not in rendered
    assert REFERENCE not in rendered
    assert ABSENT_REFERENCE not in rendered


def test_a_child_that_cannot_be_a_server_does_not_qualify(
    installed: Installed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real spawn, a real client, and a child that never speaks the protocol.

    The module the child is asked to run does not exist, so the interpreter exits
    before a byte is written. The exchange is bounded, the child is reaped by the
    SDK's own shutdown, and one fixed sentence comes back.
    """
    monkeypatch.setattr(server, "_QUALIFICATION_MODULE", "omnivia_core_mcp.no_such")
    with pytest.raises(server.StartupError, match="did not complete"):
        server.verify_installed_setup(installed.write(name="no-child.json"))


def test_the_child_is_told_a_configuration_path_and_nothing_else(
    installed: Installed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole command line, and the one thing it must never carry.

    A bearer in a child's argument vector is readable by every process on the
    host, and one in its environment is inherited by everything the child starts,
    so the qualification passes neither: the child is told where the protected
    configuration is and resolves its own credential from this installation's
    store, exactly as it does under a host. `env` stays `None`, so what the child
    gets is the SDK's sanitized default environment.
    """
    path = installed.write(name="argv.json")
    seen: list[Any] = []

    def refuse(parameters: Any, **_: Any) -> Any:
        seen.append(parameters)
        raise AssertionError("this test does not spawn the child")

    monkeypatch.setattr(server, "stdio_client", refuse)
    with pytest.raises(server.StartupError, match="did not complete"):
        server.verify_installed_setup(path)

    (parameters,) = seen
    assert parameters.command == sys.executable
    assert parameters.args == [
        "-P",
        "-m",
        "omnivia_core_mcp.server",
        "--config",
        str(path),
    ]
    assert parameters.env is None
    assert SECRET not in parameters.args


# --- what the wire said, and what it has to say -------------------------------


@pytest.mark.parametrize(
    ("name", "version"),
    [("some-other-server", None), (server.SERVER_NAME, "0.0.0-not-this-build")],
    ids=["another-server", "another-build"],
)
def test_a_peer_that_is_not_this_build_does_not_qualify(
    installed: Installed,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    version: str | None,
) -> None:
    """The identity check, at the seam where the wire's answer arrives.

    A command that starts *something* which speaks MCP has not proved it starts
    this server, and a peer at another version is not the build whose manifest
    the inventory is being compared against.
    """
    monkeypatch.setattr(
        server,
        "_qualification",
        lambda _path: (name, version or server.__version__, RESTRICTED_TOOLS),
    )
    with pytest.raises(server.StartupError, match="own MCP server"):
        server.verify_installed_setup(installed.write(name="identity.json"))


@pytest.mark.parametrize(
    "advertised",
    [(), ("workspace_inspect",), tuple(reversed(RESTRICTED_TOOLS))],
    ids=["empty", "partial", "reordered"],
)
def test_an_inventory_that_is_not_a_profile_s_own_does_not_qualify(
    installed: Installed, monkeypatch: pytest.MonkeyPatch, advertised: tuple[str, ...]
) -> None:
    """Exact and ordered, against the manifest and against the two numbers.

    A listing that is empty, partial or merely reordered is not either profile's
    inventory, so no profile is settled and the setup is refused rather than
    reported with whatever count came back.
    """
    monkeypatch.setattr(
        server,
        "_qualification",
        lambda _path: (server.SERVER_NAME, server.__version__, advertised),
    )
    with pytest.raises(server.StartupError, match="exposure"):
        server.verify_installed_setup(installed.write(name="inventory.json"))
