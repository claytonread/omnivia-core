"""Wiring the installed-MCP authority to the local endpoint, from either side.

:mod:`omnivia_core_runtime.service.installed_mcp` can answer every question this
module asks, and can answer it only in the one process that holds the
installation catalogue open. Every *other* process in an installation -- every
follower workspace service -- has the same questions and none of the access, so
this module is the two implementations of one seam: the owner answers from the
store, and a follower forwards the identical control document to the owner's
private endpoint and returns what comes back.

**A follower never opens the catalogue.** That is the whole reason the forwarding
half exists rather than each service opening the installation database "just to
read". The catalogue is guarded by a lifetime lock precisely so one process
writes it; a second reader would see a snapshot with no fencing generation behind
it, and a revocation would take effect for the owner and not for the follower
that had already read the row. Forwarding makes the authoritative process the
only one that ever answers, which is what makes revocation immediate everywhere.

**Nothing is cached, on either side.** :class:`AuthenticatedApplicationDispatch`
holds a seam and a dispatcher and no session: every call resolves the presented
bearer again -- a store read for the owner, a round trip for a follower -- and
then dispatches under exactly what that resolution returned. A bearer that was
revoked, rotated away, or replayed a microsecond later gets a fresh answer,
because there is no older one to get.

**The administrator is the server's, never the caller's.** An administration
control carries no credential at all. Reaching this endpoint is the operating
system's owner-private proof, and the :class:`AuthenticatedSession` presented to
:meth:`InstalledMcpAuthority.configure` is built here, in the service, holding
the same ``INSTALLATION_ADMINISTRATOR_ROLE`` the installation service already
requires to create a workspace. There is no field in the wrapper a caller could
put a role in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from omnivia_core.contracts.v1 import (
    CapabilityRef,
    RequestEnvelope,
    ResponseEnvelope,
)
from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.service.installed_mcp import (
    InstalledMcpAdministrationError,
    InstalledMcpAuthenticationError,
    InstalledMcpAuthority,
)
from omnivia_core_runtime.service.local_control import (
    LOCAL_CONTROL_FIELD,
    LOCAL_CONTROL_RESULT_FIELD,
    LOCAL_CONTROL_VERSION,
    LocalControlError,
    LocalControlKind,
    LocalControlRefusal,
    LocalControlRequest,
)
from omnivia_core_runtime.service.transport import (
    LocalEndpoint,
    LocalSocketTransport,
    TransportError,
)
from omnivia_core_runtime.storage.installation_store import (
    InstallationStoreError,
    InstalledMcpSetup,
    McpHost,
    McpProfile,
)

__all__ = [
    "AuthenticatedApplicationDispatch",
    "ControlExchange",
    "InstalledMcpSeam",
    "OwnedInstalledMcp",
    "ProxiedInstalledMcp",
    "SessionDispatch",
    "forward_control",
    "setup_view",
]

#: What a forwarded reply may carry. Far above anything an installation holds
#: -- two hosts, and a session's grants are a profile's dozen-odd rows -- and
#: far below what a peer squatting the endpoint could make this process build.
_MAXIMUM_WIRE_ITEMS: Final = 4096

#: Every string admitted off this wire is an identifier or a vocabulary word --
#: a principal, a workspace, an operation, a scope, a purpose, a capability id
#: or version. None is prose, so one bound covers all of them.
_MAXIMUM_WIRE_TEXT: Final = 256

#: What one answered control may carry at the top level. Exact in both
#: directions: a member this build does not read is still a member the peer
#: chose, and admitting one is how a later build starts reading it.
_ANSWER_RESULT: Final = frozenset({LOCAL_CONTROL_RESULT_FIELD, "kind", "result"})
_ANSWER_ERROR: Final = frozenset({LOCAL_CONTROL_RESULT_FIELD, "kind", "error"})
_ERROR_MEMBERS: Final = frozenset({"code", "message"})

#: The exact members of a forwarded session, and of one capability inside it.
#: `roles` and `installations` are absent from both, so a reply carrying either
#: is refused rather than read-and-ignored -- the difference matters, because an
#: ignored field is one a wrong peer still succeeded in putting on this wire.
_SESSION_MEMBERS: Final = frozenset(
    {
        "principal_id",
        "workspaces",
        "operations",
        "scopes",
        "purposes",
        "capabilities",
    }
)
_CAPABILITY_MEMBERS: Final = frozenset({"id", "version"})

#: How a follower reaches the owner. A function rather than a held connection,
#: so the endpoint is dialled at the moment of the call and an owner that
#: restarted is reached rather than a socket that used to be one.
ControlExchange = Callable[[LocalEndpoint, Mapping[str, object]], Mapping[str, object]]


def forward_control(
    endpoint: LocalEndpoint, document: Mapping[str, object]
) -> Mapping[str, object]:
    """Carry one control document to `endpoint` over the ordinary local wire."""
    return LocalSocketTransport(endpoint=endpoint).exchange(document)


class SessionDispatch(Protocol):
    """The application path, executed under a caller session the server resolved.

    Satisfied by :class:`~omnivia_core_runtime.service.application.ApplicationDispatcher`
    structurally. Declared as one method rather than taken as that class so this
    module does not pull the whole application surface in to name one seam.
    """

    def dispatch_for_session(
        self, request: RequestEnvelope, session: AuthenticatedSession
    ) -> ResponseEnvelope: ...


class InstalledMcpSeam(Protocol):
    """Resolve bearers and answer controls against the authoritative catalogue.

    Two implementations and no third: :class:`OwnedInstalledMcp` in the process
    that holds the catalogue, and :class:`ProxiedInstalledMcp` in every other.
    Both refuse with :class:`LocalControlRefusal` and its frozen sentences, so a
    caller cannot tell from a refusal which side of the installation answered it.
    """

    def authenticate(self, credential: str) -> AuthenticatedSession: ...

    def administer(self, control: LocalControlRequest) -> Mapping[str, object]: ...


def setup_view(setup: InstalledMcpSetup) -> dict[str, object]:
    """One durable setup as wire members, which is already its redacted form.

    :class:`InstalledMcpSetup` has no salt, digest or secret field, so there is
    nothing here to leave out -- redaction is the shape of the type rather than a
    filter applied to it, and a field added to the store does not silently start
    travelling because this function enumerates what it sends.
    """
    return {
        "setup_id": setup.setup_id,
        "host": setup.host.value,
        "workspace_id": setup.workspace_id,
        "principal_id": setup.principal_id,
        "profile": setup.profile.value,
        "authoring_intent": setup.authoring_intent,
        "credential_reference": setup.credential_reference,
        "status": setup.status.value,
        "setup_generation": setup.setup_generation,
    }


@dataclass(frozen=True)
class OwnedInstalledMcp:
    """The seam in the process that holds the installation catalogue open.

    `administrator` is the session this service established for itself at start
    up; it is passed to every administration call and is the only reason one is
    authorized. It is a field rather than something built per call so the wiring
    that grants it is reviewable in one place.
    """

    authority: InstalledMcpAuthority
    administrator: AuthenticatedSession

    def authenticate(self, credential: str) -> AuthenticatedSession:
        """Resolve one bearer, now, against the catalogue.

        The store's refusal is translated rather than propagated: its message is
        already payload-free, but letting a storage exception cross the transport
        seam would make every future message in that module a wire disclosure
        decision. One frozen code goes out instead.
        """
        principal = None
        try:
            principal = self.authority.authenticate(credential)
        except InstalledMcpAuthenticationError:
            principal = None
        if principal is None:
            raise LocalControlRefusal(LocalControlError.UNAUTHENTICATED)
        return principal.session

    def administer(self, control: LocalControlRequest) -> Mapping[str, object]:
        if control.kind is LocalControlKind.MCP_AUTHENTICATE:
            session = self.authenticate(control.credential)
            return _session_view(session)
        if control.kind is LocalControlKind.MCP_AUTHORING_ADMISSION:
            return self._admission(control.credential)
        return self._administration(control)

    def _admission(self, credential: str) -> Mapping[str, object]:
        """Whether this bearer's own principal is admitted for its own workspace.

        Neither identifier is an argument, deliberately. A caller that could name
        the principal and the workspace to ask about would be asking a question
        about somebody else's authority, and the honest question -- the only one
        the MCP console needs -- is about the authority it just presented. Both
        come back so the caller can confirm the answer is about what it meant.
        """
        principal = None
        try:
            principal = self.authority.authenticate(credential)
        except InstalledMcpAuthenticationError:
            principal = None
        if principal is None:
            raise LocalControlRefusal(LocalControlError.UNAUTHENTICATED)
        setup = principal.setup
        return {
            "admitted": self.authority.admits_authoring(
                setup.principal_id, setup.workspace_id
            ),
            "principal_id": setup.principal_id,
            "workspace_id": setup.workspace_id,
        }

    def _administration(self, control: LocalControlRequest) -> Mapping[str, object]:
        """Configure, status or revoke, under this service's own administrator.

        Every failure below is decided inside a handler and raised after it ends,
        which is this tree's stated convention: an exception raised inside the
        handler keeps ``__context__`` pointing at the storage error being handled,
        and one attribute access would recover whatever that error quoted.
        """
        refusal: LocalControlError | None = None
        result: Mapping[str, object] | None = None
        try:
            result = self._apply(control)
        except InstalledMcpAdministrationError:
            refusal = LocalControlError.UNAUTHORIZED
        except InstallationStoreError:
            refusal = LocalControlError.REFUSED
        if refusal is not None:
            raise LocalControlRefusal(refusal)
        assert result is not None
        return result

    def _apply(self, control: LocalControlRequest) -> Mapping[str, object]:
        if control.kind is LocalControlKind.MCP_CONFIGURE:
            profile = McpProfile(_text(control.argument("profile")))
            provisioning = self.authority.configure(
                self.administrator,
                host=McpHost(_text(control.argument("host"))),
                workspace_id=_text(control.argument("workspace_id")),
                profile=profile,
                authoring_intent=control.argument("authoring_intent") is True,
            )
            answer: dict[str, object] = {
                "setup": setup_view(provisioning.setup),
                "rotated": provisioning.rotated,
            }
            if provisioning.secret is not None:
                # The one place a secret is ever written to this wire, and only
                # over the owner-private endpoint, only on the call that minted
                # it, and only to the administrator who asked for the rotation.
                # `status` and `revoke` have no branch that could reach here.
                answer["secret"] = provisioning.secret.reveal()
            return answer
        if control.kind is LocalControlKind.MCP_STATUS:
            host = control.argument("host")
            setups = self.authority.status(
                self.administrator,
                host=None if host is None else McpHost(_text(host)),
            )
            return {"setups": [setup_view(setup) for setup in setups]}
        revoked = self.authority.revoke(
            self.administrator, host=McpHost(_text(control.argument("host")))
        )
        return {"setup": None if revoked is None else setup_view(revoked)}


@dataclass(frozen=True)
class ProxiedInstalledMcp:
    """The seam in a follower: forward the control, return what the owner said.

    Holds an endpoint and a dial function rather than a connection, matching the
    rest of this transport: one connection per call, nothing pooled, so a control
    issued after the owner restarted dials the current owner rather than a socket
    that used to be one.
    """

    endpoint: LocalEndpoint
    exchange: ControlExchange = field(default=forward_control)

    def authenticate(self, credential: str) -> AuthenticatedSession:
        answer = self._forward(
            {
                LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
                "kind": LocalControlKind.MCP_AUTHENTICATE.value,
                "credential": credential,
            },
            LocalControlKind.MCP_AUTHENTICATE,
        )
        return _session_from_view(answer)

    def administer(self, control: LocalControlRequest) -> Mapping[str, object]:
        document: dict[str, object] = {
            LOCAL_CONTROL_FIELD: LOCAL_CONTROL_VERSION,
            "kind": control.kind.value,
        }
        if control.credential:
            document["credential"] = control.credential
        if control.arguments is not None:
            document["arguments"] = dict(control.arguments)
        return self._forward(document, control.kind)

    def _forward(
        self, document: Mapping[str, object], kind: LocalControlKind
    ) -> Mapping[str, object]:
        """One round trip to the owner, with its refusal preserved as its own.

        A transport failure becomes ``unavailable`` and never ``unauthenticated``:
        the two mean opposite things to a caller deciding whether to re-provision,
        and collapsing an unreachable owner into a rejected bearer would send a
        human to rotate a credential that was never the problem.
        """
        answer: Mapping[str, object] | None = None
        try:
            answer = self.exchange(self.endpoint, dict(document))
        except TransportError:
            answer = None
        if answer is None:
            raise LocalControlRefusal(LocalControlError.UNAVAILABLE)
        return _admitted_answer(answer, kind)


@dataclass(frozen=True)
class AuthenticatedApplicationDispatch:
    """Resolve the presented bearer on every call, then dispatch under it.

    Two statements and no state between them. `seam` is asked to resolve the
    credential -- from the catalogue, or from the owner over IPC -- and the
    session it returns is handed straight to `dispatcher.dispatch_for_session`,
    which narrows it against the service's own configured session before any
    handler runs. This object holds no session field, so a revoked or rotated
    bearer cannot be served from one.
    """

    seam: InstalledMcpSeam
    dispatcher: SessionDispatch

    def dispatch(
        self, credential: str, request: RequestEnvelope
    ) -> ResponseEnvelope:
        return self.dispatcher.dispatch_for_session(
            request, self.seam.authenticate(credential)
        )


def _session_view(session: AuthenticatedSession) -> dict[str, object]:
    """A resolved session as wire members, sorted so the frame is canonical.

    No credential, by construction: :class:`AuthenticatedSession` has no field
    that could hold one, which is the property that makes forwarding the
    *resolution* safe even though forwarding the bearer to reach it was necessary.
    """
    return {
        "principal_id": session.principal_id,
        "workspaces": sorted(session.workspaces),
        "operations": sorted(session.operations),
        "scopes": sorted(session.scopes),
        "purposes": sorted(session.purposes),
        "capabilities": [
            {"id": capability.id, "version": capability.version}
            for capability in session.capabilities
        ],
    }


def _session_from_view(answer: Mapping[str, object]) -> AuthenticatedSession:
    """Rebuild the owner's answer, refusing anything that is not one.

    `roles` and `installations` are not on this wire and cannot be put on it: the
    admitted key set is exact, so a reply naming either is refused outright
    rather than read and discarded. A dedicated MCP principal holds neither, and
    a follower that could be *told* it holds a role would be a follower an owner
    impersonator could make an administrator. They are empty here because there
    is no admitted reply in which they are anything else.
    """
    if frozenset(answer) != _SESSION_MEMBERS:
        raise LocalControlRefusal(LocalControlError.UNAVAILABLE)
    capabilities: list[CapabilityRef] = []
    raw = answer.get("capabilities")
    if not isinstance(raw, list) or len(raw) > _MAXIMUM_WIRE_ITEMS:
        raise LocalControlRefusal(LocalControlError.UNAVAILABLE)
    for entry in raw:
        if not isinstance(entry, Mapping) or frozenset(entry) != _CAPABILITY_MEMBERS:
            raise LocalControlRefusal(LocalControlError.UNAVAILABLE)
        capabilities.append(
            CapabilityRef(
                id=_text(entry.get("id")), version=_text(entry.get("version"))
            )
        )
    return AuthenticatedSession(
        principal_id=_text(answer.get("principal_id")),
        workspaces=frozenset(_texts(answer.get("workspaces"))),
        operations=frozenset(_texts(answer.get("operations"))),
        scopes=frozenset(_texts(answer.get("scopes"))),
        purposes=frozenset(_texts(answer.get("purposes"))),
        capabilities=tuple(capabilities),
    )


def _admitted_answer(
    answer: Mapping[str, object], kind: LocalControlKind
) -> Mapping[str, object]:
    """Admit one control reply, or turn it into this module's own refusal.

    The owner's error code is carried through rather than replaced, because it is
    already one of this module's frozen codes and its message is already frozen
    text. A reply that is not a control result at all -- a wrong version, a wrong
    kind, an unknown code -- is ``unavailable``: something answered that endpoint
    and it was not the installation service this build expects.
    """
    if (
        answer.get(LOCAL_CONTROL_RESULT_FIELD) != LOCAL_CONTROL_VERSION
        or answer.get("kind") != kind.value
    ):
        raise LocalControlRefusal(LocalControlError.UNAVAILABLE)
    members = frozenset(answer)
    if members == _ANSWER_ERROR:
        error = answer.get("error")
        if not isinstance(error, Mapping) or frozenset(error) != _ERROR_MEMBERS:
            raise LocalControlRefusal(LocalControlError.UNAVAILABLE)
        code = None
        for candidate in LocalControlError:
            if candidate.value == error.get("code"):
                code = candidate
        raise LocalControlRefusal(
            LocalControlError.UNAVAILABLE if code is None else code
        )
    result = answer.get("result")
    if members != _ANSWER_RESULT or not isinstance(result, Mapping):
        raise LocalControlRefusal(LocalControlError.UNAVAILABLE)
    return result


def _text(value: object) -> str:
    if type(value) is not str or len(value) > _MAXIMUM_WIRE_TEXT:
        raise LocalControlRefusal(LocalControlError.UNAVAILABLE)
    return value


def _texts(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAXIMUM_WIRE_ITEMS:
        raise LocalControlRefusal(LocalControlError.UNAVAILABLE)
    return [_text(entry) for entry in value]
