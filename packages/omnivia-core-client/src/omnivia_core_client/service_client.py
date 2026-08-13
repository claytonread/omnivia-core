"""The high-level client: state where the service is, reach it, call it.

Every piece of a client call already exists in this package -- the descriptor
read, the version agreement, the live probe, the two transports, the credential
seam, the whole-call deadline. What did not exist is the one object that puts
them in order, so each first-party caller assembled its own: read
``service.json``, pick a transport from the scheme it found, dial discovery with
it, compare the two descriptors, then call. That is four decisions per caller,
and the CLI and MCP already differ on some of them.

**Nothing here is a new mechanism.** There is no framing, no contract decoding,
no credential resolution, no transport, no retry, and no process launch in this
module: every one of those is called, not restated. What this module adds is the
composition and the refusals that only the composition can make.

Two configurations, and the difference between them is who says where the
service is.

:class:`InstallationServiceConfig` names an installation state root and a
workspace, and the *installation* says where the service is: the published
descriptor is read through
:func:`~omnivia_core_client.discovery.read_local_descriptor` -- with its
provenance, locality and identity rules intact -- and the local transport is
built from the endpoint it publishes. A caller never reads ``service.json``,
never sees a socket path or a pipe name, and never chooses a transport; the same
configuration reaches a ``unix://`` endpoint on POSIX and a ``pipe://`` endpoint
on Windows because :class:`~omnivia_core_client.LocalIpcTransport` carries both.

:class:`HttpServiceConfig` names an endpoint and the *caller* says where the
service is, so there is no file to check it against and the service's own
``service.discover`` answer is what is negotiated. **It takes the name of a
credential and never a secret**: the field is a
:class:`~omnivia_core_client.CredentialReference` and a
:class:`~omnivia_core_client.Credential` handed to it is refused, so a secret
cannot be configured into a long-lived object, and resolution stays the injected
resolver's at the moment of each call. There is likewise no argument here that
weakens TLS, because :class:`~omnivia_core_client.HttpEndpoint` has none:
``https`` is verified and cleartext reaches a loopback IP literal or nothing.

**Absence is not a failure.** An installation that has published no descriptor
yet returns ``None`` from :meth:`ServiceClient.connect`, exactly as
:func:`~omnivia_core_client.discover_endpoint` returns ``None`` -- a service that
has not started is an ordinary state, and turning it into an exception would
make every caller catch one to ask a question. Everything else fails closed with
this package's declared types.

**Diagnostics are fixed sentences.** No endpoint, path, workspace identifier,
credential reference or secret appears in any message raised here, and the two
refusals this module owns are raised outside every handler so no ``__cause__``
or ``__context__`` survives to carry a transport's own words.

Standard library plus the public ``omnivia_core`` contracts only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, TypeAlias

from omnivia_core.contracts.v1 import (
    RequestEnvelope,
    ResponseEnvelope,
    ServiceEndpointDescriptor,
)
from omnivia_core_client.compatibility import NegotiatedEndpoint, negotiate_endpoint
from omnivia_core_client.credentials import CredentialCache, CredentialReference
from omnivia_core_client.deadline import CancellationToken, Deadline
from omnivia_core_client.discovery import (
    descriptor_path,
    discover_endpoint,
    probe_live_descriptor,
    read_local_descriptor,
)
from omnivia_core_client.errors import CompatibilityError, TransportError
from omnivia_core_client.http_transport import (
    HttpEndpoint,
    HttpTransport,
    parse_http_endpoint,
)
from omnivia_core_client.local_ipc import LocalIpcTransport
from omnivia_core_client.transport import ClientTransport

__all__ = [
    "HttpServiceConfig",
    "InstallationServiceConfig",
    "ServiceClient",
    "ServiceConfig",
]


#: Raised from outside the ``except`` blocks that decide on them, never inside.
#: Same shape and same reason as ``discovery.py``'s ``_raise_*`` helpers, and
#: ``scripts/check-raise-discipline.py`` is what keeps it that way.


def _raise_republished() -> NoReturn:
    raise TransportError("the installation republished its service mid-connect")


def _raise_incompatible() -> NoReturn:
    raise CompatibilityError("the service endpoint is not one this client can talk to")


@dataclass(frozen=True, slots=True)
class InstallationServiceConfig:
    """Reach whatever service this installation publishes for one workspace.

    Both fields are validated where the configuration is written rather than at
    the first call, so a mistyped root or an inadmissible workspace identifier is
    a refusal at the point that can be fixed. The workspace rule is
    :func:`~omnivia_core_client.descriptor_path`'s own -- called for its check,
    not for the path -- because a second statement of what a ``WorkspaceId`` may
    be is a second one to keep in step with the contract.
    """

    installation_state: Path
    """The trusted installation state root. The descriptor path is derived from
    it exactly as discovery derives it; there is no glob, environment lookup,
    home fallback or caller-selected descriptor path here either."""

    workspace_id: str
    """The public ``WorkspaceId`` whose service is wanted."""

    def __post_init__(self) -> None:
        if not isinstance(self.installation_state, Path):
            raise TypeError("installation_state must be a pathlib.Path")
        descriptor_path(self.installation_state, self.workspace_id)


@dataclass(frozen=True, slots=True)
class HttpServiceConfig:
    """Reach one explicitly named HTTP service, with one named credential.

    **A secret is not configurable.** ``credential_reference`` is a
    :class:`~omnivia_core_client.CredentialReference`, which is a name; a
    :class:`~omnivia_core_client.Credential` -- or a bare string that might be
    one -- is refused here, and the secret itself is resolved per call by the
    :class:`~omnivia_core_client.CredentialCache` the host injected. So this
    object can be held, logged and passed around, and holds nothing that must
    not be.

    **Unverified TLS is not configurable either**, and not because it is
    defaulted off: the endpoint is parsed by
    :func:`~omnivia_core_client.parse_http_endpoint` and carried as an
    :class:`~omnivia_core_client.HttpEndpoint`, which admits ``https`` with the
    verified default context or cleartext to a loopback IP literal, and there is
    no field, flag or code path in either that produces anything else.
    """

    endpoint_uri: str
    """``https://host[:port]``, or ``http://<loopback IP literal>[:port]``. No
    userinfo, path, query or fragment: a credential must never travel in a URL,
    and the parser refuses a URL with a place to put one."""

    credential_reference: CredentialReference
    """The *name* of the credential to present at this endpoint."""

    credentials: CredentialCache
    """The host's credential seam. Resolution happens through it, per call."""

    def __post_init__(self) -> None:
        if not isinstance(self.credential_reference, CredentialReference):
            raise TypeError(
                "credential_reference must be a CredentialReference: a client is "
                "configured with the name of a credential, never with a secret"
            )
        if not isinstance(self.credentials, CredentialCache):
            raise TypeError("credentials must be a CredentialCache")
        # Refuse an inadmissible endpoint where it is written. `endpoint` below
        # is what the transport is built from, and both are the same parser.
        parse_http_endpoint(self.endpoint_uri)

    @property
    def endpoint(self) -> HttpEndpoint:
        """The normalized endpoint this configuration names."""
        return parse_http_endpoint(self.endpoint_uri)


ServiceConfig: TypeAlias = InstallationServiceConfig | HttpServiceConfig
"""Either way of saying where the service is."""


@dataclass(frozen=True, slots=True)
class ServiceClient:
    """One compatible live service, and the way to issue application calls to it.

    Built only by :meth:`connect`, which is what makes the three fields mean
    something together: the transport reached the endpoint, the descriptor is
    what that endpoint answered with, and the negotiation is the version
    agreement in force for calls made through it. Frozen, because none of the
    three may change under a caller that has already read one -- a re-negotiated
    endpoint is a new connect, not a mutated client.

    There is nothing to close. Both transports open one connection per call and
    hold none between them, and the credential cache belongs to the host that
    injected it, so a client that is no longer wanted is simply dropped.
    """

    transport: ClientTransport
    """The transport :meth:`connect` composed. Satisfies the same protocol every
    caller of this package already writes against, so anything that takes a
    ``ClientTransport`` takes this."""

    descriptor: ServiceEndpointDescriptor
    """The endpoint's own description of itself, retained. It carries the
    ``workspace_id`` a request is built with, so a caller does not have to hold
    the configuration alongside the client to know what it connected to."""

    negotiated: NegotiatedEndpoint
    """The API, protocol and descriptor versions agreed for this endpoint."""

    @classmethod
    def connect(
        cls,
        config: ServiceConfig,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceClient | None:
        """Compose the transport this configuration names and verify it is live.

        ``None`` means an installation that has published no descriptor -- an
        ordinary state, not a failure. Everything else that goes wrong raises:
        the descriptor's own refusals, a version disagreement, or a transport
        that could not carry the probe.

        The whole-call ``deadline`` and ``cancellation`` cover the connect *and*
        are the caller's to reuse or replace for the calls that follow; both are
        passed through to discovery and the transport unchanged.
        """
        if isinstance(config, InstallationServiceConfig):
            return cls._connect_installation(
                config, deadline=deadline, cancellation=cancellation
            )
        if isinstance(config, HttpServiceConfig):
            return cls._connect_http(
                config, deadline=deadline, cancellation=cancellation
            )
        raise TypeError(
            "config must be an InstallationServiceConfig or an HttpServiceConfig"
        )

    @classmethod
    def _connect_installation(
        cls,
        config: InstallationServiceConfig,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None,
    ) -> ServiceClient | None:
        """Read what the installation published, dial it, and prove it is that one.

        The descriptor is read twice, and deliberately: once here for the
        endpoint URI the transport needs, and once inside
        :func:`~omnivia_core_client.discover_endpoint`, which owns the checks and
        must apply them to the file it will vouch for rather than to one handed
        in. Publication is atomic, so the two reads can legitimately see
        different files -- and a client that dialled the first while discovery
        approved the second would be carrying an approval for an endpoint it is
        not talking to. Comparing them is what closes that, and it is the same
        comparison the CLI already makes for the same reason.
        """
        published = read_local_descriptor(
            config.installation_state, config.workspace_id
        )
        if published is None:
            return None
        transport = LocalIpcTransport(endpoint_uri=published.endpoint_uri)
        discovered = discover_endpoint(
            config.installation_state,
            config.workspace_id,
            transport=transport,
            deadline=deadline,
            cancellation=cancellation,
        )
        if discovered is None:
            return None
        if discovered.descriptor != published:
            _raise_republished()
        return cls(
            transport=transport,
            descriptor=discovered.descriptor,
            negotiated=discovered.negotiated,
        )

    @classmethod
    def _connect_http(
        cls,
        config: HttpServiceConfig,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None,
    ) -> ServiceClient:
        """Dial the endpoint the caller named and negotiate what it answers with.

        No file, so no file comparison: the caller stated the endpoint, and the
        credential presented on the probe is bound to that endpoint's normalized
        origin by the cache's key. What the service answers is negotiated the
        same way a published descriptor is, and a disagreement is collapsed to a
        fixed sentence -- the descriptor came from a peer, and this module quotes
        no peer material.
        """
        transport = HttpTransport(
            endpoint=config.endpoint,
            credential_reference=config.credential_reference,
            credentials=config.credentials,
        )
        descriptor = probe_live_descriptor(
            transport, deadline=deadline, cancellation=cancellation
        )
        negotiated: NegotiatedEndpoint | None = None
        try:
            negotiated = negotiate_endpoint(descriptor)
        except CompatibilityError:
            incompatible = True
        else:
            incompatible = False
        if incompatible or negotiated is None:
            _raise_incompatible()
        return cls(transport=transport, descriptor=descriptor, negotiated=negotiated)

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        """Issue one application call over this client's transport.

        A forward and nothing else. The request, the deadline and the token go
        to the transport as they arrived -- not copied, not re-derived, not
        wrapped -- so the whole-call budget a caller set is the one the wire
        waits on, and cancelling that token still interrupts this call. The
        answer is returned as the transport produced it, including an error
        response envelope: a peer that answered with an application error has
        answered.
        """
        return self.transport.call(
            request, deadline=deadline, cancellation=cancellation
        )
