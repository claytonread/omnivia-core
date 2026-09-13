"""Trusted, bounded configuration for the stdio MCP server.

The configuration file fixes the principal and the maximum authority an MCP
session may claim.  It is coordination data, not a credential store: both modes
carry an opaque credential *reference* and never credential material.  In
`service_client` mode a host resolves that name through its own injected
resolver; in `managed_local` mode the installed setup path wrote the material
into this installation's own protected store and the reference is the name it
filed it under.  Neither the secret nor its location is in this document.

A `managed_local` configuration without a reference is the shape every
installation had before that setup path existed.  It is still *read* -- this
module parses a document it can describe rather than rejecting it -- but it names
no dedicated principal, and :func:`~omnivia_core_mcp.server.connect` refuses to
start a server on it: an installed server presents its own bearer or does not
run, because the local endpoint would otherwise admit it as the service's own
principal.  Re-running the installed setup for that host is what writes the
reference.

Only explicit paths are accepted.  The reader opens a regular owner-private
file without following symlinks, verifies that the pathname and descriptor keep
the same identity throughout the bounded read, and then decodes one UTF-8 JSON
object.  All public failures are fixed, payload-free sentences.

"Owner-private" is proved from the open descriptor on both platform families --
the POSIX owner and mode bits, or on Windows a native owner and DACL proof that
the file's owner is this process's user and that no access-allowed ACE grants
anyone else -- and either proof fails closed.  That proof is
:func:`~omnivia_core_client.read_owner_private`, in the shared client, because
the protected credential store reads its files under exactly the same rules and
two copies of one security check are two to keep correct.

This module also decides which exposure profile a server advertises, once, from
that validated document plus one protected answer it cannot give itself: see
:func:`effective_profile`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, NoReturn

from omnivia_core_client import (
    ClientError,
    CredentialReference,
    ServiceClient,
    parse_http_endpoint,
    read_owner_private,
)

from omnivia_core.contracts.v1 import (
    IDENTIFIER_PATTERN,
    PURPOSE_PATTERN,
    WORKSPACE_ID_PATTERN,
)
from omnivia_core_mcp.manifest import AUTHORING_PROFILE, RESTRICTED_PROFILE

__all__ = [
    "CONFIGURATION_FORMAT",
    "MAXIMUM_CONFIGURATION_BYTES",
    "AuthoringAdmission",
    "McpConfiguration",
    "McpConfigurationError",
    "effective_profile",
    "parse_configuration",
    "read_configuration",
]

CONFIGURATION_FORMAT: Final = "omnivia.mcp-config.v1"
MAXIMUM_CONFIGURATION_BYTES: Final = 65_536

_FIELDS: Final = frozenset(
    {
        "format",
        "principal_id",
        "allowed_workspace_ids",
        "default_workspace_id",
        "allowed_purposes",
        "mutation_enabled",
        "service_mode",
        "installation_state",
        "endpoint",
        "credential_reference",
    }
)
_IDENTIFIER_RE: Final = re.compile(IDENTIFIER_PATTERN)
_WORKSPACE_RE: Final = re.compile(WORKSPACE_ID_PATTERN)
_PURPOSE_RE: Final = re.compile(PURPOSE_PATTERN)


class McpConfigurationError(Exception):
    """A fixed-text refusal raised before MCP initialization."""


class _DuplicateMember(ValueError):
    pass


def _raise_file() -> NoReturn:
    raise McpConfigurationError(
        "the MCP configuration file is not a trusted owner-only file"
    )


def _raise_document() -> NoReturn:
    raise McpConfigurationError("the MCP configuration document is not valid")


def _raise_semantics() -> NoReturn:
    raise McpConfigurationError("the MCP configuration values are not admissible")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMember
        result[key] = value
    return result


def _invalid_constant(_value: str) -> NoReturn:
    raise ValueError


def _exact_text(value: object, pattern: re.Pattern[str], maximum: int) -> str | None:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        return None
    return value if pattern.fullmatch(value) is not None else None


def _unique_texts(
    value: object,
    *,
    maximum_items: int,
    maximum_length: int,
    pattern: re.Pattern[str],
) -> tuple[str, ...] | None:
    if type(value) is not list or not 1 <= len(value) <= maximum_items:
        return None
    accepted: list[str] = []
    for item in value:
        text = _exact_text(item, pattern, maximum_length)
        if text is None or text in accepted:
            return None
        accepted.append(text)
    return tuple(accepted)


def _validated_endpoint(value: object) -> str | None:
    if type(value) is not str:
        return None
    endpoint = None
    try:
        endpoint = parse_http_endpoint(value)
    except ClientError:
        pass
    return None if endpoint is None else endpoint.origin


def _validated_reference(value: object) -> CredentialReference | None:
    if type(value) is not str:
        return None
    reference = None
    try:
        reference = CredentialReference(value)
    except ClientError:
        pass
    return reference


@dataclass(frozen=True, slots=True, repr=False)
class McpConfiguration:
    """One validated, immutable MCP authority and service configuration."""

    format: str
    principal_id: str
    allowed_workspace_ids: tuple[str, ...]
    default_workspace_id: str | None
    allowed_purposes: tuple[str, ...]
    mutation_enabled: bool
    service_mode: Literal["managed_local", "service_client"]
    installation_state: Path | None
    endpoint: str | None
    credential_reference: CredentialReference | None

    def __post_init__(self) -> None:
        principal = _exact_text(self.principal_id, _IDENTIFIER_RE, 128)
        workspaces = _unique_texts(
            list(self.allowed_workspace_ids),
            maximum_items=128,
            maximum_length=128,
            pattern=_WORKSPACE_RE,
        )
        purposes = _unique_texts(
            list(self.allowed_purposes),
            maximum_items=32,
            maximum_length=128,
            pattern=_PURPOSE_RE,
        )
        invalid = (
            self.format != CONFIGURATION_FORMAT
            or principal is None
            or type(self.allowed_workspace_ids) is not tuple
            or workspaces is None
            or type(self.allowed_purposes) is not tuple
            or purposes is None
            or type(self.mutation_enabled) is not bool
            or self.service_mode not in ("managed_local", "service_client")
            or (
                self.default_workspace_id is not None
                and self.default_workspace_id not in (workspaces or ())
            )
        )
        if self.service_mode == "managed_local":
            invalid = invalid or (
                not isinstance(self.installation_state, Path)
                or not self.installation_state.is_absolute()
                or self.endpoint is not None
                or not (
                    self.credential_reference is None
                    or isinstance(self.credential_reference, CredentialReference)
                )
            )
        elif self.service_mode == "service_client":
            invalid = invalid or (
                self.installation_state is not None
                or _validated_endpoint(self.endpoint) is None
                or not isinstance(self.credential_reference, CredentialReference)
            )
        if invalid:
            _raise_semantics()

    def __repr__(self) -> str:
        return "McpConfiguration(<redacted>)"

    @property
    def selected_workspace_id(self) -> str | None:
        """The unambiguous workspace for tools that carry no workspace selector."""
        if self.default_workspace_id is not None:
            return self.default_workspace_id
        if len(self.allowed_workspace_ids) == 1:
            return self.allowed_workspace_ids[0]
        return None


def parse_configuration(document: object) -> McpConfiguration:
    """Validate one already-decoded configuration object."""
    if type(document) is not dict:
        _raise_document()
    mapping = document
    if set(mapping) - _FIELDS:
        _raise_semantics()
    required = {
        "format",
        "principal_id",
        "allowed_workspace_ids",
        "allowed_purposes",
        "service_mode",
    }
    if not required <= set(mapping):
        _raise_semantics()

    principal = _exact_text(mapping.get("principal_id"), _IDENTIFIER_RE, 128)
    workspaces = _unique_texts(
        mapping.get("allowed_workspace_ids"),
        maximum_items=128,
        maximum_length=128,
        pattern=_WORKSPACE_RE,
    )
    purposes = _unique_texts(
        mapping.get("allowed_purposes"),
        maximum_items=32,
        maximum_length=128,
        pattern=_PURPOSE_RE,
    )
    default = mapping.get("default_workspace_id")
    if default is not None:
        default = _exact_text(default, _WORKSPACE_RE, 128)
    mutation = mapping.get("mutation_enabled", False)
    mode = mapping.get("service_mode")
    if (
        mapping.get("format") != CONFIGURATION_FORMAT
        or principal is None
        or workspaces is None
        or purposes is None
        or type(mutation) is not bool
        or mode not in ("managed_local", "service_client")
        or ("default_workspace_id" in mapping and default is None)
        or (default is not None and default not in workspaces)
    ):
        _raise_semantics()

    installation: Path | None = None
    endpoint: str | None = None
    reference: CredentialReference | None = None
    if mode == "managed_local":
        raw_installation = mapping.get("installation_state")
        if (
            type(raw_installation) is not str
            or not Path(raw_installation).is_absolute()
            or "endpoint" in mapping
        ):
            _raise_semantics()
        installation = Path(raw_installation)
        if "credential_reference" in mapping:
            # Present or absent, never malformed. A reference the grammar does
            # not admit is refused here rather than carried to a store lookup
            # that would have to decide what an inadmissible name means -- and
            # refused *before* MCP initialization, so a configuration whose
            # credential wiring is unsafe never advertises a tool at all.
            reference = _validated_reference(mapping.get("credential_reference"))
            if reference is None:
                _raise_semantics()
    else:
        if "installation_state" in mapping:
            _raise_semantics()
        endpoint = _validated_endpoint(mapping.get("endpoint"))
        reference = _validated_reference(mapping.get("credential_reference"))
        if endpoint is None or reference is None:
            _raise_semantics()

    return McpConfiguration(
        format=CONFIGURATION_FORMAT,
        principal_id=principal,
        allowed_workspace_ids=workspaces,
        default_workspace_id=default,
        allowed_purposes=purposes,
        mutation_enabled=mutation,
        service_mode=mode,
        installation_state=installation,
        endpoint=endpoint,
        credential_reference=reference,
    )


#: The protected authoring-admission seam, and **the whole of what Phase 6 owes
#: this module**.
#:
#: Called with the **already connected** :class:`~omnivia_core_client.ServiceClient`,
#: the configured principal and the selected workspace, it answers one question
#: and only from durable protected state: *did a human owner or administrator
#: explicitly record informed authoring intent for exactly this principal and
#: this workspace, and does that authority hold right now?*  It is not a policy
#: this package can evaluate -- nothing readable from the public configuration is
#: evidence of it -- so it is an argument rather than a default.
#:
#: **The connected client is the first argument because it is the only way Phase
#: 6 can answer honestly.**  That record lives behind the same authenticated,
#: authorised Core service this session has just reached and proved serves this
#: workspace, so an implementation reads it through this client.  Handing over
#: only the two identifiers would leave Phase 6 opening the installation database
#: itself or dialling a second connection -- both of them a way around the
#: authority the session already established, and both of them a boundary this
#: package must not invite anyone across.
#:
#: **The installed implementation is
#: :func:`omnivia_core_mcp.server._installed_admission`**, which the console entry
#: point injects for a managed-local configuration that names a credential
#: reference, and which asks the protected authority through the shared client's
#: `mcp.authoring_admission` control.  It is still an argument and not a default:
#: a configuration with no reference, a remote configuration, a test, or an
#: embedding host each supply their own answer or none, and none is `restricted`.
AuthoringAdmission = Callable[[ServiceClient, str, str], bool]


def effective_profile(
    configuration: McpConfiguration,
    client: ServiceClient,
    workspace_id: str,
    *,
    authoring_admission: AuthoringAdmission | None = None,
) -> str:
    """The one exposure profile this server advertises, decided once at startup.

    Two independent conditions, both required, neither sufficient:

    * `mutation_enabled` is the **ceiling** the public document sets. Absent or
      false is `restricted`, always -- there is no argument, prompt, purpose or
      host setting that widens it.
    * `authoring_admission` is the **floor** only protected state can raise.
      `mutation_enabled: true` alone selects nothing: an editor who flips that
      byte in a configuration file has raised a ceiling over an empty room.

    So a legacy or hand-edited `mutation_enabled: true` cannot silently activate
    authoring, which is the upgrade rule stated as code rather than as migration
    prose: production does inject a resolver, and it answers from a protected
    record only `omnivia mcp configure --profile authoring` can write.

    `client` is connected and already proved to serve `workspace_id`; the caller
    is :func:`~omnivia_core_mcp.server.connect`, which is what guarantees both.
    A service that could not be reached, or that answered for another workspace,
    never gets this far -- so a failed connection and a descriptor mismatch are
    `restricted` by never producing a session at all, and the admission is not
    consulted about a service nobody has agreed with.

    Fails closed in every other direction too: no resolver, a resolver that
    answers anything but `True`, and a resolver that raises all give
    `restricted`. A protected authority that cannot be consulted has not
    confirmed anything, and a server that widened its surface because a lookup
    broke would be widening it for exactly the reason it should not.
    """
    if not configuration.mutation_enabled or authoring_admission is None:
        return RESTRICTED_PROFILE
    admitted = False
    try:
        admitted = (
            authoring_admission(client, configuration.principal_id, workspace_id)
            is True
        )
    except Exception:  # noqa: BLE001 -- an admission that failed has not admitted.
        admitted = False
    return AUTHORING_PROFILE if admitted else RESTRICTED_PROFILE


def _read_trusted_bytes(path: Path) -> bytes:
    """The configuration bytes, or this module's two refusals.

    The proof itself -- a regular file, not a symlink, owned by this process's
    user, unreachable by group or world, whose identity did not change under the
    read -- is :func:`~omnivia_core_client.read_owner_private`'s, in the one
    package that owns it. It answers ``None`` for every refusal and quotes
    nothing, which is what lets this module keep its own fixed sentences: a file
    that is not trustworthy and a document that is too long are different things
    to tell a host, and one byte past the bound is how the second is recognised.
    """
    content = read_owner_private(path, maximum_bytes=MAXIMUM_CONFIGURATION_BYTES + 1)
    if content is None:
        _raise_file()
    if len(content) > MAXIMUM_CONFIGURATION_BYTES:
        _raise_document()
    return content


def read_configuration(path: Path) -> McpConfiguration:
    """Read one explicit, trusted configuration file before MCP initialization."""
    if not isinstance(path, Path) or not path.is_absolute():
        _raise_file()
    content = _read_trusted_bytes(path)
    if content.startswith(b"\xef\xbb\xbf"):
        _raise_document()
    invalid = False
    document: object = None
    try:
        text = content.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_invalid_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        _DuplicateMember,
        TypeError,
        ValueError,
    ):
        invalid = True
    if invalid:
        _raise_document()
    return parse_configuration(document)
