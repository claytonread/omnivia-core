"""The internal local-control wrapper carried on the OVC1 endpoint.

Two things already travel this socket and neither can carry what Phase 6 needs.
A ``RequestEnvelope`` is an application request executed as *this service's own*
principal, and a ``ServiceProbeRequest`` is answered before authentication exists
at all. Between them there is no way to say "run this operation as whoever holds
*this* bearer", and no way to say "administer this installation's MCP setup" --
so this module adds one wrapper that says both, and says which it is in the
document itself rather than by guessing from shape.

**It is not a public contract and must never become one.** There is no schema
file, no generated type, no operation-catalogue entry and no MCP tool for
anything here: an exposure manifest that could name a control would be a model
able to mint or inspect its own authority, which is the exact failure Gate B
exists to prevent. The version string is frozen in this module, the admitted
kinds are a closed enum, and a peer that sends a kind this build does not know is
refused rather than negotiated with.

**Nothing is inferred.** A document with no ``local_control`` member is not a
control and is handed to the existing router untouched -- that is the whole
compatibility rule, stated once: every request and probe that worked before this
module existed takes a path this module never sees. A document that *does* carry
``local_control`` is admitted only against the exact key set its kind implies. An
unknown key, a missing required one, a key that belongs to another kind, a value
of the wrong JSON type, or a value past its bound is a refusal; nothing is
ignored and nothing is defaulted.

**A refusal never quotes the caller.** Every message is a frozen sentence chosen
by code, because the one value certain to be in a malformed control is the bearer
credential, and a diagnostic that interpolated "unexpected member ``credential``:
<value>" would publish it into whatever logs, audit records and wire errors the
refusal reaches. The same rule is why the error table is keyed by a closed code
enum and why no branch below builds a message at its raise site.

Standard library and the public ``omnivia_core`` canonical-JSON admission only:
this module opens nothing, holds no state, and imports no storage, authority or
transport, so the runtime's layering is unchanged by it being on the wire path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

from omnivia_core.contracts.v1 import RequestEnvelope, ResponseEnvelope

__all__ = [
    "LOCAL_CONTROL_FIELD",
    "LOCAL_CONTROL_HOSTS",
    "LOCAL_CONTROL_PROFILES",
    "LOCAL_CONTROL_RESULT_FIELD",
    "LOCAL_CONTROL_VERSION",
    "MAXIMUM_CREDENTIAL_CHARACTERS",
    "AuthenticatedDispatch",
    "LocalControlError",
    "LocalControlKind",
    "LocalControlRefusal",
    "LocalControlRequest",
    "McpAdministration",
    "control_error_document",
    "control_result_document",
    "decode_local_control",
    "is_local_control",
]

#: The member whose presence -- and only whose presence -- makes a document a
#: control. Chosen to collide with nothing: neither ``RequestEnvelope`` nor
#: ``ServiceProbeRequest`` admits an unknown member, so a peer that sent this to
#: an older build was already refused by the contract decoder.
LOCAL_CONTROL_FIELD: Final = "local_control"

#: The same member name on the way back, deliberately *different* from the
#: request's. A reply and a request are never the same document, so neither can
#: be replayed as the other by a peer that captured one.
LOCAL_CONTROL_RESULT_FIELD: Final = "local_control_result"

#: This wrapper's frozen version. A later shape gets a later string and is
#: admitted by a build that knows it; there is no range, no minimum and no
#: negotiation, because both ends of this wire ship in one installation.
LOCAL_CONTROL_VERSION: Final = "omnivia.local-control.v1"

#: A bearer is ``secrets.token_urlsafe(32)`` -- 43 characters. The bound is far
#: above that and far below anything worth buffering, and exists so a peer cannot
#: make this process hash a megabyte by calling it a credential.
MAXIMUM_CREDENTIAL_CHARACTERS: Final = 512

#: The hosts and profiles an administration control may name, as the wire spells
#: them. Stated here rather than imported so this module stays free of storage;
#: ``test_local_control_codec.py`` pins both to ``McpHost`` and ``McpProfile`` so
#: the two cannot drift apart in silence.
LOCAL_CONTROL_HOSTS: Final = ("claude-code", "codex")
LOCAL_CONTROL_PROFILES: Final = ("restricted", "authoring")

_MAXIMUM_IDENTIFIER_CHARACTERS: Final = 128


class LocalControlKind(str, Enum):
    """Every control this build answers. A closed set, checked by equality."""

    #: Run one application request as whoever holds the presented bearer.
    APPLICATION_CALL = "application.call"
    #: Resolve one bearer to its durable grants. Served only between the
    #: workspace service and the installation service that owns the catalogue.
    MCP_AUTHENTICATE = "mcp.authenticate"
    #: Ask, for the presented bearer, whether protected authoring intent holds.
    MCP_AUTHORING_ADMISSION = "mcp.authoring_admission"
    #: The three administration controls. No bearer: reaching this endpoint is
    #: the operating system's owner-private proof, and the administrator session
    #: is the *server's*, never a claim a caller makes about itself.
    MCP_CONFIGURE = "mcp.configure"
    MCP_STATUS = "mcp.status"
    MCP_REVOKE = "mcp.revoke"


class LocalControlError(str, Enum):
    """The closed refusal vocabulary. One code, one frozen sentence, no words
    from the caller in either."""

    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    UNAUTHENTICATED = "unauthenticated"
    UNAUTHORIZED = "unauthorized"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


_MESSAGES: Final[dict[LocalControlError, str]] = {
    LocalControlError.MALFORMED: (
        "the local control document is not one this service admits"
    ),
    LocalControlError.UNSUPPORTED: (
        "this endpoint does not serve the requested local control"
    ),
    LocalControlError.UNAUTHENTICATED: (
        "the presented credential does not resolve to live installed MCP authority"
    ),
    LocalControlError.UNAUTHORIZED: (
        "installed MCP administration requires a local installation administrator"
    ),
    LocalControlError.REFUSED: (
        "the installed MCP authority refused the requested change"
    ),
    LocalControlError.UNAVAILABLE: (
        "the authoritative installation service could not be reached"
    ),
}

#: Kinds that carry a bearer, and kinds that carry administration arguments.
#: Disjoint on purpose: a control never both presents a credential and names the
#: state to change, so a captured bearer cannot be pasted into a configure.
_BEARER_KINDS: Final = frozenset(
    {
        LocalControlKind.APPLICATION_CALL,
        LocalControlKind.MCP_AUTHENTICATE,
        LocalControlKind.MCP_AUTHORING_ADMISSION,
    }
)
_ADMINISTRATION_KINDS: Final = frozenset(
    {
        LocalControlKind.MCP_CONFIGURE,
        LocalControlKind.MCP_STATUS,
        LocalControlKind.MCP_REVOKE,
    }
)

_ARGUMENTS: Final[dict[LocalControlKind, tuple[frozenset[str], frozenset[str]]]] = {
    # kind -> (required members, optional members)
    LocalControlKind.MCP_CONFIGURE: (
        frozenset({"host", "workspace_id", "profile", "authoring_intent"}),
        frozenset(),
    ),
    LocalControlKind.MCP_STATUS: (frozenset(), frozenset({"host"})),
    LocalControlKind.MCP_REVOKE: (frozenset({"host"}), frozenset()),
}


class LocalControlRefusal(Exception):
    """A control was refused. Carries a code and nothing the caller wrote.

    ``message`` is looked up from the frozen table rather than passed in, so
    there is no constructor a future edit could hand a formatted string to.
    """

    def __init__(self, code: LocalControlError) -> None:
        self.code = code
        super().__init__(_MESSAGES[code])


@dataclass(frozen=True, slots=True, repr=False)
class LocalControlRequest:
    """One admitted control.

    ``credential`` is present exactly for the bearer kinds and ``arguments``
    exactly for the administration kinds; ``request`` exactly for an application
    call. The decoder is what establishes that, so a handler reads the field its
    kind implies without re-checking presence.

    ``repr`` is redacted and there is no generated one: this value holds a bearer
    secret, and a dataclass repr would print it into any log line, container
    rendering or exception that touched the request.
    """

    kind: LocalControlKind
    credential: str = ""
    request: Mapping[str, object] | None = None
    arguments: Mapping[str, object] | None = None

    def __repr__(self) -> str:
        return f"LocalControlRequest(kind={self.kind.value!r}, <redacted>)"

    def argument(self, name: str) -> object | None:
        """One administration argument, or ``None`` when it was not supplied."""
        return None if self.arguments is None else self.arguments.get(name)


def is_local_control(document: Mapping[str, object]) -> bool:
    """Whether this document claims to be a control at all.

    Presence alone, deliberately: a document carrying the member with a wrong
    value is a *malformed control* and must be refused as one, not silently
    forwarded to the application router to be refused as a malformed request.
    """
    return LOCAL_CONTROL_FIELD in document


def decode_local_control(document: Mapping[str, object]) -> LocalControlRequest:
    """Admit one control document exactly, or refuse it.

    Every branch below refuses; none corrects. The version must be this build's
    exact string, the kind must be one this build serves, the key set must be
    exactly what that kind implies, and every value must be the JSON type and
    within the bound its member declares.
    """
    if document.get(LOCAL_CONTROL_FIELD) != LOCAL_CONTROL_VERSION:
        raise LocalControlRefusal(LocalControlError.MALFORMED)
    raw_kind = document.get("kind")
    if type(raw_kind) is not str:
        raise LocalControlRefusal(LocalControlError.MALFORMED)
    kind = _kind(raw_kind)

    expected = {LOCAL_CONTROL_FIELD, "kind"}
    if kind in _BEARER_KINDS:
        expected.add("credential")
    if kind is LocalControlKind.APPLICATION_CALL:
        expected.add("request")
    if kind in _ADMINISTRATION_KINDS:
        expected.add("arguments")
    if set(document) != expected:
        raise LocalControlRefusal(LocalControlError.MALFORMED)

    credential = ""
    if kind in _BEARER_KINDS:
        credential = _bearer(document.get("credential"))
    request = None
    if kind is LocalControlKind.APPLICATION_CALL:
        request = _object(document.get("request"))
    arguments = None
    if kind in _ADMINISTRATION_KINDS:
        arguments = _admitted_arguments(kind, document.get("arguments"))
    return LocalControlRequest(
        kind=kind, credential=credential, request=request, arguments=arguments
    )


def control_result_document(
    kind: LocalControlKind, result: Mapping[str, object]
) -> dict[str, object]:
    """The wire form of one answered control."""
    return {
        LOCAL_CONTROL_RESULT_FIELD: LOCAL_CONTROL_VERSION,
        "kind": kind.value,
        "result": dict(result),
    }


def control_error_document(
    kind: LocalControlKind | None, code: LocalControlError
) -> dict[str, object]:
    """The wire form of one refusal, with its frozen sentence and no caller text.

    ``kind`` is ``None`` when the document was too malformed to name one; the
    reply then says so with the empty string rather than guessing at a kind,
    because echoing back an unadmitted value is how a refusal starts carrying
    caller material.
    """
    return {
        LOCAL_CONTROL_RESULT_FIELD: LOCAL_CONTROL_VERSION,
        "kind": "" if kind is None else kind.value,
        "error": {"code": code.value, "message": _MESSAGES[code]},
    }


def _kind(value: str) -> LocalControlKind:
    """The kind this string names, refusing anything this build does not serve.

    Raised after the lookup rather than inside it: a ``ValueError`` from ``Enum``
    quotes the value it was given, and this one came off the wire.
    """
    known = None
    for candidate in LocalControlKind:
        if candidate.value == value:
            known = candidate
    if known is None:
        raise LocalControlRefusal(LocalControlError.MALFORMED)
    return known


def _bearer(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAXIMUM_CREDENTIAL_CHARACTERS
        or value.strip() != value
    ):
        raise LocalControlRefusal(LocalControlError.MALFORMED)
    return value


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        type(key) is not str for key in value
    ):
        raise LocalControlRefusal(LocalControlError.MALFORMED)
    return value


def _admitted_arguments(
    kind: LocalControlKind, value: object
) -> Mapping[str, object]:
    """Exactly the members this administration kind declares, each in range."""
    arguments = _object(value)
    required, optional = _ARGUMENTS[kind]
    present = set(arguments)
    if not required <= present or not present <= (required | optional):
        raise LocalControlRefusal(LocalControlError.MALFORMED)
    if "host" in present:
        _member(arguments["host"], LOCAL_CONTROL_HOSTS)
    if "profile" in present:
        _member(arguments["profile"], LOCAL_CONTROL_PROFILES)
    if "workspace_id" in present:
        identifier = arguments["workspace_id"]
        if (
            type(identifier) is not str
            or not 1 <= len(identifier) <= _MAXIMUM_IDENTIFIER_CHARACTERS
        ):
            raise LocalControlRefusal(LocalControlError.MALFORMED)
    if "authoring_intent" in present and type(
        arguments["authoring_intent"]
    ) is not bool:
        raise LocalControlRefusal(LocalControlError.MALFORMED)
    return arguments


def _member(value: object, admitted: tuple[str, ...]) -> None:
    if type(value) is not str or value not in admitted:
        raise LocalControlRefusal(LocalControlError.MALFORMED)


class AuthenticatedDispatch(Protocol):
    """Run one application request as whoever presented ``credential``.

    One method, and it takes the secret rather than a session, because that is
    what makes "no cached sessions" a property of the *type* instead of a rule an
    implementation is asked to remember. There is no way to hand this seam a
    session, so there is nothing for a transport to keep between calls: a bearer
    that was revoked, rotated away or replayed is resolved again, against durable
    state, on the very next call.
    """

    def dispatch(
        self, credential: str, request: RequestEnvelope
    ) -> ResponseEnvelope: ...


class McpAdministration(Protocol):
    """Answer one installed-MCP control against the authoritative catalogue.

    Returns the control's ``result`` members; a refusal is raised as
    :class:`LocalControlRefusal` so the wire form is built in exactly one place.
    Whether this is served from the process that owns the catalogue or forwarded
    to the one that does is the implementation's business and not the transport's.
    """

    def administer(self, control: LocalControlRequest) -> Mapping[str, object]: ...
