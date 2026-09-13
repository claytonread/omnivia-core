"""Startup, the connected session, and the authority every call is made under.

V06-6 moved three decisions out of the command line and into a trusted
`omnivia.mcp-config.v1` document: who this server acts as, which workspace it
reaches, and what it may claim to be doing. This module is the adversarial half
of that -- each test states a way the boundary could be crossed and shows it is
refused, and refused *before* anything is sent.

**"Before the client call" is asserted, not assumed.** Every refusal test uses a
transport that raises if it is ever asked to carry anything, so a refusal that
happened after a dial, a credential resolution or a round trip fails here rather
than passing quietly. That is the difference between a policy and a message.

The client under test is a real :class:`~omnivia_core_client.ServiceClient` --
the shared one -- holding a recording transport. Nothing here re-implements
`call`, and nothing here stands in for the session: production takes exactly the
object these tests build.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
import pytest
from mcp import Client
from omnivia_core_client import (
    MAXIMUM_JSON_BYTES,
    AuthenticatedLocalTransport,
    AuthoringAdmissionResult,
    CancellationToken,
    Credential,
    CredentialCache,
    CredentialMissingError,
    CredentialReference,
    Deadline,
    HttpServiceConfig,
    InstallationServiceConfig,
    InstalledCredentialStore,
    LocalIpcTransport,
    ManagedServiceConnection,
    ManagedStartError,
    NegotiatedEndpoint,
    ServiceClient,
    TransportError,
    authenticated_client,
    encode_frame,
)
from omnivia_core_mcp import server
from omnivia_core_mcp.configuration import McpConfiguration, parse_configuration
from omnivia_core_mcp.manifest import (
    ADMITTED_MUTATIONS,
    EXPOSURE_MANIFEST,
    exposure_manifest,
)

from omnivia_core.contracts.v1 import (
    EVIDENCE_CAPTURE_MAX_CONTENT_BYTES,
    ApiError,
    CapabilityRef,
    CapabilitySet,
    CompatibilityMetadata,
    ErrorResponseEnvelope,
    GrantedAuthority,
    RequestEnvelope,
    ResponseEnvelope,
    ResponseMetadata,
    ServiceEndpointDescriptor,
    SuccessResponseEnvelope,
    UpgradeState,
    VersionCapabilityEnvelope,
    VersionWindow,
    codec,
)

WORKSPACE = "ws-authority-01"
OTHER_WORKSPACE = "ws-somebody-else-01"
PRINCIPAL = "mcp-authority-principal"
STATE = Path("/srv/omnivia/installation-state")
ENDPOINT = "https://core.example.com"
REFERENCE = "core-api"
INSTALLED_REFERENCE = "omcp-installed-principal-0001"
INSTALLED_SECRET = "omcp_live_5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d"
ROTATED_SECRET = "omcp_live_00112233445566778899aabbccddeeff"

ALL_PURPOSES = ["workspace_inspection", "knowledge_retrieval"]

#: What an authoring installation additionally allows. Three more purposes, each
#: the service's own for the operations the wider profile adds, so a refusal
#: below is about the profile or the payload and never about a purpose nobody
#: granted.
AUTHORING_PURPOSES = [
    *ALL_PURPOSES,
    "memory_authoring",
    "content_ingestion",
    "job_observation",
]

#: The smallest call each tool the authoring profile adds actually accepts.
#:
#: *Smallest* is now decided by the canonical contract rather than by the
#: advertised key list: the adapter decodes an authoring input through
#: `omnivia_core.contracts.v1`'s own decoder before it sends anything, so a
#: payload that merely names declared keys no longer reaches a transport at all.
#: Every entry here is a document that decoder accepts -- checked by
#: `test_every_admitted_call_is_one_the_canonical_contract_accepts`, so this
#: table cannot quietly drift into being shape-only again -- and the values stay
#: as small and as obviously synthetic as that allows, because what the tests
#: below read is the envelope, not the content.
AUTHORING_CALLS: dict[str, dict[str, Any]] = {
    "memory_create": {
        "input": {
            "record_type": "memory.fact",
            "domain_scope": "product.core",
            "content": {"statement": "a fact proposed through MCP"},
            "evidence_disposition": "available",
            "sources": [{"kind": "document", "source_id": "note-1"}],
            "assertion": {
                "actor_id": PRINCIPAL,
                "actor_kind": "agent",
                "actor_role": "author",
                "asserted_at": "2026-01-01T00:00:00Z",
                "evidence": [{"source": {"kind": "document", "source_id": "note-1"}}],
            },
        },
        "idempotency_key": "k-1",
    },
    "evidence_capture": {
        "input": {
            "source_native_id": "note-1",
            "media_type": "text/markdown",
            "text": "one captured line\n",
        },
        "idempotency_key": "k-2",
    },
    "import_start": {
        "input": {
            "source": {
                "staged_source_ref": "stg-0001",
                "source_kind": "archive",
                "content_checksum": "sha256:" + "a" * 64,
                "content_length_bytes": 1024,
                "media_type": "application/zip",
            }
        },
        "idempotency_key": "k-3",
    },
    "job_get": {"job_id": "job-1"},
    "job_events": {"job_id": "job-1"},
}


# --- the trusted configuration, as a document -----------------------------------


@pytest.fixture(autouse=True)
def installed_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real installation root, holding the credential every document below names.

    Autouse because an installed server presents its own bearer or does not
    start: a managed-local configuration without one is refused at
    :func:`server.connect`, so a test about workspace selection, managed start,
    or the profile would otherwise never reach the code it is about. `STATE`
    points here for the duration, which is what keeps the documents
    :func:`configuration` builds and the assertions that compare against `STATE`
    one thing rather than two.
    """
    root = tmp_path / "installation-state"
    root.mkdir()
    InstalledCredentialStore(root).store(
        CredentialReference(INSTALLED_REFERENCE), Credential(INSTALLED_SECRET)
    )
    monkeypatch.setitem(globals(), "STATE", root)
    return root


def configuration(**overrides: Any) -> McpConfiguration:
    """One validated configuration, parsed from a document rather than built.

    Through `parse_configuration` on purpose: a test that constructed
    `McpConfiguration` directly could assemble a combination the document reader
    would never accept, and would then be proving something about a shape that
    cannot reach the server.

    The installed shape, because it is the only managed-local shape that starts:
    it names the credential `installed_state` filed. :func:`legacy_configuration`
    is the pre-setup document, and the only thing it is good for now is proving
    that it is refused.
    """
    document: dict[str, Any] = {
        "format": "omnivia.mcp-config.v1",
        "principal_id": PRINCIPAL,
        "allowed_workspace_ids": [WORKSPACE],
        "allowed_purposes": list(ALL_PURPOSES),
        "service_mode": "managed_local",
        "installation_state": str(STATE),
        "credential_reference": INSTALLED_REFERENCE,
    }
    document.update(overrides)
    return parse_configuration(document)


def legacy_configuration(**overrides: Any) -> McpConfiguration:
    """The managed-local document every installation had before the setup path.

    Well-formed, readable, and naming no dedicated principal -- which is why the
    reader still parses it and the server no longer starts on it.
    """
    document: dict[str, Any] = {
        "format": "omnivia.mcp-config.v1",
        "principal_id": PRINCIPAL,
        "allowed_workspace_ids": [WORKSPACE],
        "allowed_purposes": list(ALL_PURPOSES),
        "service_mode": "managed_local",
        "installation_state": str(STATE),
    }
    document.update(overrides)
    return parse_configuration(document)


def remote_configuration(**overrides: Any) -> McpConfiguration:
    document: dict[str, Any] = {
        "format": "omnivia.mcp-config.v1",
        "principal_id": PRINCIPAL,
        "allowed_workspace_ids": [WORKSPACE],
        "allowed_purposes": list(ALL_PURPOSES),
        "service_mode": "service_client",
        "endpoint": ENDPOINT,
        "credential_reference": REFERENCE,
    }
    document.update(overrides)
    return parse_configuration(document)


# --- a real ServiceClient over a transport that records, or refuses to be used ---


def descriptor(workspace_id: str = WORKSPACE) -> ServiceEndpointDescriptor:
    return ServiceEndpointDescriptor(
        descriptor_version="1.0",
        workspace_id=workspace_id,
        service_instance_id="svc-authority",
        installation_id="inst-authority",
        endpoint_uri="unix:///tmp/omnivia-authority/s.sock",
        protocol_version="1.0",
        server_version="0.1.0",
        supported_api_versions=VersionWindow(minimum="1.0", maximum="1.0"),
        supported_workspace_versions=VersionWindow(minimum="1", maximum="1"),
        workspace_format_version="1",
        ready=True,
        lifecycle_state="ready",
        fencing_generation=1,
        published_at="2026-01-01T00:00:00Z",
    )


def response_metadata(request: RequestEnvelope, **overrides: Any) -> ResponseMetadata:
    """The metadata a service puts on any answer, correlated to this request."""
    refs = (CapabilityRef(id="workspace.read", version="1.0"),)
    fields: dict[str, Any] = {
        "request_id": request.metadata.request_id,
        "correlation_id": request.metadata.correlation_id,
    }
    fields.update(overrides)
    return ResponseMetadata(
        version=VersionCapabilityEnvelope(
            api_version="1.0",
            server_version="0.1.0",
            workspace_format_version="1",
            compatibility=CompatibilityMetadata(
                selected_api_version="1.0",
                selected_workspace_version="1",
                supported_api_versions=VersionWindow(minimum="1.0", maximum="1.0"),
                supported_workspace_versions=VersionWindow(minimum="1", maximum="1"),
                status="compatible",
                upgrade_state=UpgradeState(value="none"),
                deprecations=(),
            ),
            capabilities=CapabilitySet(supported=refs, granted=refs, effective=refs),
        ),
        authority=GrantedAuthority(principal_id=PRINCIPAL, roles=(), capabilities=refs),
        **fields,
    )


@dataclass
class RecordingTransport:
    """Records what it was asked to carry and answers with a prepared envelope.

    `answer` takes the request so a response can correlate with it, which is
    what makes the correlation test able to produce one that does not.
    """

    answer: Any = None
    calls: list[RequestEnvelope] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        self.calls.append(request)
        if self.answer is None:
            return SuccessResponseEnvelope(
                metadata=response_metadata(request), result={"workspace": {"ok": True}}
            )
        return self.answer(request)  # type: ignore[no-any-return]

    def probe(
        self, request: Any, *, deadline: Deadline, cancellation: Any = None
    ) -> Any:
        raise AssertionError("nothing in the call path probes")


class RefusingTransport:
    """A transport that fails the test if the call path ever reaches it.

    This is how "refused before the client call" is proved: a refusal that had
    already dialled, resolved a credential or sent a frame raises here instead of
    returning a tidy `isError`.
    """

    def call(self, request: RequestEnvelope, **_kwargs: Any) -> ResponseEnvelope:
        raise AssertionError("a refused call must not reach the client")

    def probe(self, request: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a refused call must not reach the client")


def client(transport: Any, workspace_id: str = WORKSPACE) -> ServiceClient:
    """The shared client, assembled around a transport a test can watch."""
    return ServiceClient(
        transport=transport,
        descriptor=descriptor(workspace_id),
        negotiated=NegotiatedEndpoint(
            api_version="1.0", protocol_version="1.0", descriptor_version="1.0"
        ),
    )


def session(
    transport: Any | None = None,
    *,
    config: McpConfiguration | None = None,
    credentials: CredentialCache | None = None,
    profile: str = "restricted",
) -> server.ConnectedSession:
    return server.ConnectedSession(
        configuration=config if config is not None else configuration(),
        client=client(RefusingTransport() if transport is None else transport),
        workspace_id=WORKSPACE,
        status="attached",
        credentials=credentials,
        profile=profile,
    )


def authoring_session(
    transport: Any | None = None, *, config: McpConfiguration | None = None
) -> server.ConnectedSession:
    """A session with the profile `connect` would have frozen on an admitted one.

    Built directly rather than through `connect` because the seam is the profile,
    not the dial: `test_connect_freezes_the_profile_it_was_admitted` covers the
    other half, and everything here is about what an authoring session then does.
    """
    return session(
        transport,
        config=config
        if config is not None
        else configuration(
            mutation_enabled=True, allowed_purposes=list(AUTHORING_PURPOSES)
        ),
        profile="authoring",
    )


def call(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    connected: server.ConnectedSession | None = None,
) -> types.CallToolResult:
    return server._call_tool(
        types.CallToolRequestParams(name=tool_name, arguments=arguments),
        session=connected if connected is not None else session(),
    )


# --- refusals that never reach the client ---------------------------------------


def test_a_purpose_outside_the_configuration_refuses_before_the_client() -> None:
    """The manifest states a purpose; the configuration decides whether it is allowed.

    `workspace_inspection` alone is granted below, so the five knowledge reads
    are visible in `tools/list` and uncallable -- and the refusal costs no dial,
    which is what the refusing transport proves.
    """
    connected = session(config=configuration(allowed_purposes=["workspace_inspection"]))
    for entry in EXPOSURE_MANIFEST:
        if entry.purpose == "workspace_inspection":
            continue
        result = call(entry.tool_name, {}, connected=connected)
        assert result.is_error is True, entry.tool_name
        assert result.structured_content is None
        assert "purpose" in result.content[0].text


def test_no_purpose_at_all_leaves_nothing_callable() -> None:
    """A configuration granting only an unrelated purpose calls nothing here."""
    connected = session(config=configuration(allowed_purposes=["audit_export"]))
    for entry in EXPOSURE_MANIFEST:
        assert call(entry.tool_name, {}, connected=connected).is_error is True


def test_a_guessed_tool_name_refuses_before_the_client() -> None:
    """R004-06: the allow-list is the only lookup, so absent means uncallable.

    `workspace_create` is the bootstrap operation R004-06 forbids exposing, and
    it is in the catalogue -- a name that resolves to a real operation everywhere
    except here.
    """
    for guessed in ("workspace_create", "memory_create", "evidence.search", "search"):
        result = call(guessed, {})
        assert result.is_error is True
        assert result.structured_content is None
        assert "is not a tool this server exposes" in result.content[0].text


@pytest.mark.parametrize("reserved", sorted(server.RESERVED_ARGUMENTS))
def test_no_tool_argument_can_restate_the_configured_authority(reserved: str) -> None:
    """The principal, the workspace, the purpose, the grant, the endpoint and the
    credential are the configuration's. A tool call that names any of them is
    refused by name, before the advertised-schema check and before the client.

    By name rather than only as an undeclared key, so this stays a refusal even
    if a canonical contract ever declares a field with one of these names: the
    schema check would then admit it and this would not.
    """
    result = call("knowledge_search", {"query": "anything", reserved: "attacker"})
    assert result.is_error is True
    assert result.structured_content is None
    assert reserved in result.content[0].text
    assert "trusted configuration" in result.content[0].text


def test_an_unadvertised_argument_is_refused_before_a_request_is_built() -> None:
    """The advertised payload is closed, and the call path enforces that itself."""
    result = call("knowledge_search", {"query": "anything", "sort_by": "../other"})
    assert result.is_error is True
    assert "accepts no argument named 'sort_by'" in result.content[0].text


def test_a_client_failure_becomes_a_readable_tool_error() -> None:
    """Every documented client failure is an answer for the model, not a traceback.

    The client's diagnostics are payload-free by construction, so relaying one
    quotes no endpoint, no path and no workspace content.
    """

    def refuse(_request: RequestEnvelope) -> ResponseEnvelope:
        raise TransportError("the service could not be reached")

    result = call(
        "workspace_inspect", {}, connected=session(RecordingTransport(answer=refuse))
    )
    assert result.is_error is True
    assert result.structured_content is None
    assert "could not be called" in result.content[0].text


# --- what a call carries when it is allowed -------------------------------------


def test_every_request_carries_the_configured_principal_claim() -> None:
    """The claim is the configuration's `principal_id`, on every operation.

    A claim, not authority: the service decides from its own grant. What matters
    here is that it is stated, that it is the configured one, and that no tool
    argument could have put anything else there.
    """
    for entry in EXPOSURE_MANIFEST:
        transport = RecordingTransport()
        call(entry.tool_name, {}, connected=session(transport))
        (request,) = transport.calls
        assert request.metadata.principal_claim is not None
        assert request.metadata.principal_claim.claimed_principal_id == PRINCIPAL
        assert request.metadata.principal_claim.claimed_roles is None
        assert request.metadata.workspace_id == WORKSPACE
        assert request.metadata.purpose == entry.purpose


def test_the_request_states_the_catalogue_entrys_own_authority() -> None:
    """Scopes, the capability and its minimum version are read off the frozen
    catalogue entry rather than transcribed, and the purpose is the manifest's
    claim. A model supplies none of them."""
    from omnivia_core.contracts.v1 import get_operation_metadata

    for entry in EXPOSURE_MANIFEST:
        transport = RecordingTransport()
        call(entry.tool_name, {}, connected=session(transport))
        (request,) = transport.calls
        catalogue = get_operation_metadata(entry.operation)

        assert request.operation == entry.operation
        assert request.metadata.scopes == tuple(catalogue.scope.required_scopes)
        assert request.metadata.client.id == server.CLIENT_NAME
        (required,) = request.metadata.required_capabilities
        assert required.id == catalogue.required_capability.id
        assert required.minimum_version == catalogue.required_capability.minimum_version
        assert required.required == catalogue.required_capability.required


def test_an_advertised_argument_reaches_the_service_unchanged() -> None:
    """Value-level validation is the service's; the keys are this server's."""
    transport = RecordingTransport()
    call(
        "knowledge_search",
        {"query": "a governed question", "limit": 5},
        connected=session(transport),
    )
    (request,) = transport.calls
    assert request.input == {"query": "a governed question", "limit": 5}


def test_a_success_is_published_as_structured_content_and_one_json_text_item() -> None:
    result = call("workspace_inspect", {}, connected=session(RecordingTransport()))
    assert result.is_error is False
    assert result.structured_content == {"workspace": {"ok": True}}
    (item,) = result.content
    assert json.loads(item.text) == result.structured_content


def test_a_service_refusal_is_relayed_with_no_structured_content() -> None:
    def refuse(request: RequestEnvelope) -> ResponseEnvelope:
        return ErrorResponseEnvelope(
            metadata=response_metadata(request),
            error=ApiError(
                code="workspace_not_granted",
                message="not granted",
                retry_class="non_retryable",
            ),
        )

    result = call(
        "workspace_inspect", {}, connected=session(RecordingTransport(answer=refuse))
    )
    assert result.is_error is True
    assert result.structured_content is None
    message = result.content[0].text
    assert "was refused by the service" in message
    relayed = json.loads(message.split("was refused by the service: ", 1)[1])
    assert relayed["error"]["code"] == "workspace_not_granted"


@pytest.mark.parametrize("field", ["correlation_id", "request_id"])
def test_an_answer_that_does_not_correlate_is_not_published(field: str) -> None:
    """A response carrying another call's identifiers is not this call's answer.

    Publishing it as `structuredContent` would attribute one operation's result
    to another, which a host has no way to detect: it validates the document
    against the schema of the tool it asked for, and a well-formed answer to a
    different question passes.
    """

    def stale(request: RequestEnvelope) -> ResponseEnvelope:
        return SuccessResponseEnvelope(
            metadata=response_metadata(request, **{field: "mcp-a-different-call"}),
            result={"workspace": {"ok": True}},
        )

    result = call(
        "workspace_inspect", {}, connected=session(RecordingTransport(answer=stale))
    )
    assert result.is_error is True
    assert result.structured_content is None
    assert "does not correlate" in result.content[0].text


def test_a_result_that_is_not_a_json_object_is_refused_not_substituted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server.codec,
        "encode_response",
        lambda _response: {"result": "not-an-object"},
    )
    result = call("workspace_inspect", {}, connected=session(RecordingTransport()))
    assert result.is_error is True
    assert result.structured_content is None
    assert "not a JSON object" in result.content[0].text


# --- tools/list is not filtered by authority ------------------------------------


def listed(connected: server.ConnectedSession) -> list[str]:
    """What `tools/list` advertises for one session, over the official client."""

    async def ask() -> list[str]:
        async with Client(server.build_server(session=connected)) as attached:
            return [tool.name for tool in (await attached.list_tools()).tools]

    return anyio.run(ask)


def test_the_listing_does_not_vary_with_the_configured_purposes() -> None:
    """R004-06 determinism: one package version, one listing, whatever is granted.

    A listing filtered by authority would differ between two hosts running the
    same build, and a model would have no way to tell a tool it may not call from
    a tool that does not exist. The purpose is enforced on call instead.

    Asserted over three configurations that differ only in `allowed_purposes`,
    including one that allows nothing either profile claims: the six names come
    back unchanged every time, so the listing is the profile's and the purposes
    are a per-call check that never reaches it.
    """
    for purposes in (["workspace_inspection"], ["audit_export"], list(ALL_PURPOSES)):
        assert listed(session(config=configuration(allowed_purposes=purposes))) == [
            entry.tool_name for entry in EXPOSURE_MANIFEST
        ]
    assert listed(authoring_session(config=configuration(mutation_enabled=True))) == [
        entry.tool_name for entry in exposure_manifest("authoring")
    ]


# --- the profile: settled once, and the only thing that widens the surface -------


def test_the_default_session_profile_is_restricted() -> None:
    """A session built without naming a profile advertises the read-only six.

    The failure mode this default should have: code that predates profiles, or a
    future constructor that forgets to pass one, gets the narrow inventory rather
    than the wide one.
    """
    assert session().profile == "restricted"
    assert listed(session()) == [entry.tool_name for entry in EXPOSURE_MANIFEST]


@dataclass
class AdmissionRecorder:
    """A protected admission that records every call, in order, with its client.

    Standing in for what Phase 6 must write, and shaped like it: a real one reads
    a durable record through the connected client it is handed. This one only
    remembers being handed it, which is enough to prove the two facts the seam's
    contract turns on -- *when* it is asked, and *what* it is asked with.
    """

    answer: Any = True
    seen: list[tuple[Any, str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.seen is None:
            self.seen = []

    def __call__(self, client: Any, principal_id: str, workspace_id: str) -> Any:
        self.seen.append((client, principal_id, workspace_id))
        return self.answer


@pytest.mark.parametrize(
    ("mutation_enabled", "answer", "inject", "profile"),
    [
        (False, None, False, "restricted"),
        (False, True, True, "restricted"),
        (True, None, False, "restricted"),
        (True, False, True, "restricted"),
        (True, True, True, "authoring"),
    ],
    ids=["closed", "closed-admitted", "ceiling-only", "denied", "admitted"],
)
def test_connect_freezes_the_profile_it_was_admitted(
    monkeypatch: pytest.MonkeyPatch,
    mutation_enabled: bool,
    answer: Any,
    inject: bool,
    profile: str,
) -> None:
    """The profile is decided at startup, from the configuration and the seam.

    `mutation_enabled: true` with no injected admission is the production case
    and is restricted: the console entry point injects none, so editing that
    field in a configuration file widens nothing. It is also frozen with the rest
    of the session, so nothing serving it can raise it afterwards.
    """
    monkeypatch.setattr(
        server.ServiceClient,
        "connect",
        classmethod(lambda _cls, _config, **_kw: client(local_transport())),
    )
    admission = AdmissionRecorder(answer=answer) if inject else None
    connected = server.connect(
        configuration(
            mutation_enabled=mutation_enabled,
            allowed_purposes=list(AUTHORING_PURPOSES),
        ),
        authoring_admission=admission,
    )
    assert connected.profile == profile
    with pytest.raises(AttributeError):
        connected.profile = "authoring"  # type: ignore[misc]


def test_the_admission_is_asked_with_the_connected_client_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam receives the *live* client, the configured principal, this
    workspace -- and receives them only once.

    The client identity is the whole point of the argument: Phase 6's record
    lives behind the same authenticated Core service this session has just
    reached and agreed a workspace with, so an implementation reads it through
    this object. Handing over two identifiers instead would leave it opening the
    installation database itself or dialling a second connection, which is a way
    around authority that has already been established.
    """
    held = local_transport()
    connected_client = client(held)
    monkeypatch.setattr(
        server.ServiceClient,
        "connect",
        classmethod(lambda _cls, _config, **_kw: connected_client),
    )
    admission = AdmissionRecorder()

    connected = server.connect(
        configuration(mutation_enabled=True, allowed_purposes=list(AUTHORING_PURPOSES)),
        authoring_admission=admission,
    )

    assert connected.profile == "authoring"
    (asked,) = admission.seen
    assert asked[0] is connected.client, "the seam was handed a different client"
    # The session's own client: the shared client's, wrapped so the admission is
    # asked over the same authenticated control every later call travels.
    assert asked[0].transport.transport is held  # type: ignore[union-attr]
    assert asked[1:] == (PRINCIPAL, WORKSPACE)
    assert asked[0].descriptor.workspace_id == WORKSPACE


@pytest.mark.parametrize("failure", ["unreachable", "wrong_workspace"])
def test_the_admission_is_never_asked_without_a_matching_connection(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Nothing to admit until there is a service, and the right one.

    Both of these used to be decided *after* the profile had already been
    settled, which is a seam being asked about a connection that does not exist
    -- and a Phase 6 implementation handed no client would have had to go around
    Core to answer at all. Now the order is connect, agree the workspace, then
    ask; a failure at either of the first two raises before the question, so the
    session that never existed is restricted in the only way that means anything.
    """

    def connect(_cls: Any, _config: Any, **_kwargs: Any) -> ServiceClient:
        if failure == "unreachable":
            raise TransportError("the endpoint could not be reached")
        return client(RecordingTransport(), OTHER_WORKSPACE)

    monkeypatch.setattr(server.ServiceClient, "connect", classmethod(connect))
    admission = AdmissionRecorder()

    with pytest.raises((server.StartupError, TransportError)):
        server.connect(
            configuration(
                mutation_enabled=True, allowed_purposes=list(AUTHORING_PURPOSES)
            ),
            authoring_admission=admission,
        )
    assert admission.seen == []


def test_an_ambiguous_workspace_is_refused_before_the_admission_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no one workspace to be admitted for, so there is no question."""
    monkeypatch.setattr(
        server.ServiceClient,
        "connect",
        classmethod(lambda _cls, _config, **_kw: client(RecordingTransport())),
    )
    admission = AdmissionRecorder()
    with pytest.raises(server.StartupError, match="unambiguous"):
        server.connect(
            configuration(
                allowed_workspace_ids=[WORKSPACE, OTHER_WORKSPACE],
                mutation_enabled=True,
            ),
            authoring_admission=admission,
        )
    assert admission.seen == []


def test_the_two_inventories_are_the_frozen_six_and_eleven() -> None:
    """What each profile advertises *and* what each can dispatch, as one fact.

    The listing and the lookup are the same allow-list, so a restricted server
    does not merely omit the five authoring tools: it cannot resolve their names
    at all, which is what makes the refusal below a policy rather than a message.
    """
    restricted, authoring = session(), authoring_session()
    assert len(listed(restricted)) == 6
    assert len(listed(authoring)) == 11
    assert listed(authoring)[:6] == listed(restricted)
    assert listed(authoring)[6:] == [
        "memory_create",
        "evidence_capture",
        "import_start",
        "job_get",
        "job_events",
    ]


@pytest.mark.parametrize("tool_name", sorted(AUTHORING_CALLS))
def test_an_authoring_tool_is_uncallable_on_a_restricted_server(tool_name: str) -> None:
    """Including on one whose configuration says `mutation_enabled: true`.

    The refusing transport is the point: the ceiling alone admits nothing, and
    the refusal costs no dial. A restricted server with every authoring purpose
    allowed still cannot reach a tool its profile does not expose -- so the
    purpose check is not what is holding the line here.
    """
    permissive = session(
        config=configuration(
            mutation_enabled=True, allowed_purposes=list(AUTHORING_PURPOSES)
        )
    )
    result = call(tool_name, AUTHORING_CALLS[tool_name], connected=permissive)
    assert result.is_error is True
    assert result.structured_content is None
    assert "is not a tool this server exposes" in result.content[0].text
    # And the refusal offers what *is* available, which is the six and only six.
    offered = result.content[0].text.split("Available: ", 1)[1]
    available = offered.rstrip(".").split(", ")
    assert available == [entry.tool_name for entry in EXPOSURE_MANIFEST]


# --- what an authoring call carries ----------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(ADMITTED_MUTATIONS))
def test_a_mutation_dispatches_the_nested_input_and_the_key_in_the_metadata(
    tool_name: str,
) -> None:
    """The wrapper is a call shape, not a payload: it is unwrapped here.

    `input` becomes the canonical request input -- no outer key survives into
    it and capture text takes its equivalent compact base64 form -- while
    `idempotency_key` becomes `RequestMetadata.idempotency_key`, which
    is where the contract puts it and where the service's durable mutation
    coordinator looks. A key left in the payload would be an undeclared field the
    operation contract refuses; a key dropped would make every submission a new
    one.
    """
    name = tool_name.replace(".", "_")
    transport = RecordingTransport()
    arguments = AUTHORING_CALLS[name]
    result = call(name, arguments, connected=authoring_session(transport))
    assert result.is_error is False, result.content[0].text

    (request,) = transport.calls
    assert request.operation == tool_name
    expected = dict(arguments["input"])
    if tool_name == "evidence.capture":
        text = expected.pop("text")
        expected["content_base64"] = base64.b64encode(text.encode("utf-8")).decode(
            "ascii"
        )
    assert request.input == expected
    assert "idempotency_key" not in request.input
    assert request.metadata.idempotency_key == arguments["idempotency_key"]


def test_maximum_escaped_text_capture_uses_compact_form_within_ovc1_v1() -> None:
    content = "\x00" * EVIDENCE_CAPTURE_MAX_CONTENT_BYTES
    transport = RecordingTransport()
    result = call(
        "evidence_capture",
        {
            "input": {
                "source_native_id": "worst-json-escape",
                "media_type": "text/plain",
                "text": content,
            },
            "idempotency_key": "k-frame-capacity",
        },
        connected=authoring_session(transport),
    )

    assert result.is_error is False
    (request,) = transport.calls
    assert "text" not in request.input
    assert (
        base64.b64decode(request.input["content_base64"], validate=True)
        == content.encode()
    )
    assert len(encode_frame(codec.encode_request(request))) <= 8 + MAXIMUM_JSON_BYTES


@pytest.mark.parametrize("tool_name", ["job_get", "job_events"])
def test_a_read_dispatches_its_canonical_input_with_no_key(tool_name: str) -> None:
    """The two job observations are reads and are shaped like every other read:
    the canonical operation input directly, and no idempotency key, because a
    read has no settled outcome to replay."""
    transport = RecordingTransport()
    call(tool_name, AUTHORING_CALLS[tool_name], connected=authoring_session(transport))
    (request,) = transport.calls
    assert request.input == AUTHORING_CALLS[tool_name]
    assert request.metadata.idempotency_key is None


def test_every_authoring_call_states_the_catalogues_own_purpose_and_capability() -> (
    None
):
    """Read off the frozen catalogue entry and the manifest, never transcribed --
    for the five wider tools as much as for the six reads.

    The purposes are the service's own (`memory_authoring`, `content_ingestion`,
    `job_observation`), so a request states the claim the grant is checked
    against rather than one this package invented.
    """
    from omnivia_core.contracts.v1 import get_operation_metadata

    for entry in exposure_manifest("authoring"):
        arguments = AUTHORING_CALLS.get(entry.tool_name, {})
        transport = RecordingTransport()
        call(entry.tool_name, arguments, connected=authoring_session(transport))
        (request,) = transport.calls
        catalogue = get_operation_metadata(entry.operation)

        assert request.operation == entry.operation
        assert request.metadata.purpose == entry.purpose
        assert request.metadata.scopes == tuple(catalogue.scope.required_scopes)
        (required,) = request.metadata.required_capabilities
        assert required.id == catalogue.required_capability.id
        assert required.minimum_version == catalogue.required_capability.minimum_version
        assert required.required == catalogue.required_capability.required
        assert request.metadata.workspace_id == WORKSPACE
        assert request.metadata.principal_claim is not None
        assert request.metadata.principal_claim.claimed_principal_id == PRINCIPAL


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"input": {"source_native_id": "note-1"}},
        {"idempotency_key": "k-1"},
        {"input": {}, "idempotency_key": "k-1", "workspace_id": WORKSPACE},
        {"input": {}, "idempotency_key": "k-1", "purpose": "content_ingestion"},
        {"input": {}, "idempotency_key": "k-1", "source_native_id": "note-1"},
        {"input": "not-an-object", "idempotency_key": "k-1"},
        {"input": {}, "idempotency_key": 17},
        {"input": {}, "idempotency_key": None},
    ],
    ids=[
        "neither",
        "no-key",
        "no-input",
        "outer-workspace",
        "outer-purpose",
        "operation-field-outside-input",
        "input-not-an-object",
        "key-not-a-string",
        "key-null",
    ],
)
def test_a_mutation_wrapper_that_is_not_the_advertised_shape_is_refused(
    arguments: dict[str, Any],
) -> None:
    """The advertised wrapper is enforced, not described, and before the client.

    Two of these are the ones that matter: an outer `workspace_id` or `purpose`
    is a caller restating authority the configuration fixes, and an operation
    field left *outside* `input` is a caller who has understood the shape only
    half way -- accepting it would silently drop a field the contract requires.
    """
    result = call("evidence_capture", arguments, connected=authoring_session())
    assert result.is_error is True
    assert result.structured_content is None


@pytest.mark.parametrize("reserved", ["workspace_id", "principal_id", "purpose"])
def test_a_reserved_name_is_refused_inside_a_mutations_nested_input_too(
    reserved: str,
) -> None:
    """Unwrapping is not a way in.

    The outer object is closed, so an authority-shaped key there is refused as an
    unrecognised property; nesting it inside `input` gets it past that check and
    into the payload, which is why the reserved-name refusal runs again on what
    the wrapper unwrapped to.
    """
    result = call(
        "evidence_capture",
        {
            "input": {"source_native_id": "note-1", reserved: "attacker"},
            "idempotency_key": "k-1",
        },
        connected=authoring_session(),
    )
    assert result.is_error is True
    assert reserved in result.content[0].text
    assert "trusted configuration" in result.content[0].text


def test_an_unadvertised_key_inside_a_mutations_input_is_refused() -> None:
    """The nested payload is checked against the operation's own advertised
    properties -- the ones under `input` in the wrapper -- rather than against
    the wrapper's two."""
    result = call(
        "evidence_capture",
        {
            "input": {"source_native_id": "note-1", "parser": "markdown"},
            "idempotency_key": "k-1",
        },
        connected=authoring_session(),
    )
    assert result.is_error is True
    assert "accepts no argument named 'parser'" in result.content[0].text


# --- the canonical contract, checked before the call ------------------------------


def test_every_admitted_call_is_one_the_canonical_contract_accepts() -> None:
    """The guard on :data:`AUTHORING_CALLS` itself.

    Every test above reads an envelope a transport recorded, which it can only do
    if the call got past the contract decode. Asserting that directly keeps this
    table from drifting back into shape-only payloads that would then silently
    turn every one of those tests into an assertion about a refusal.
    """
    for entry in exposure_manifest("authoring"):
        arguments = AUTHORING_CALLS.get(entry.tool_name)
        if arguments is None:
            continue
        payload = arguments.get("input", arguments)
        server._CANONICAL_INPUT[entry.operation](payload)


#: One way each authoring operation's input can be wrong without being the wrong
#: *shape*: a required field missing, and a declared field carrying a value the
#: contract does not admit. Both are keys the advertised schema declares, so
#: neither is caught by the closed-property check -- only the contract's own
#: decoder sees them.
UNCANONICAL_INPUTS: list[tuple[str, str, dict[str, Any]]] = [
    ("memory_create", "missing", {"record_type": "memory.fact"}),
    (
        "memory_create",
        "bad-value",
        {**AUTHORING_CALLS["memory_create"]["input"], "record_type": ""},
    ),
    ("evidence_capture", "missing", {"source_native_id": "note-1"}),
    (
        "evidence_capture",
        "bad-value",
        {
            **AUTHORING_CALLS["evidence_capture"]["input"],
            "media_type": "application/x-sh",
        },
    ),
    ("import_start", "missing", {"source": {"staged_source_ref": "stg-0001"}}),
    (
        "import_start",
        "bad-value",
        {
            "source": {
                **AUTHORING_CALLS["import_start"]["input"]["source"],
                "content_checksum": "not-a-digest",
            }
        },
    ),
    ("job_get", "missing", {}),
    ("job_get", "bad-value", {"job_id": ""}),
    ("job_events", "missing", {}),
    ("job_events", "bad-value", {"job_id": "job-1", "limit": 0}),
]


@pytest.mark.parametrize(
    ("tool_name", "payload"),
    [(name, payload) for name, _, payload in UNCANONICAL_INPUTS],
    ids=[f"{name}-{kind}" for name, kind, _ in UNCANONICAL_INPUTS],
)
def test_an_input_the_contract_refuses_never_reaches_the_client(
    tool_name: str, payload: dict[str, Any]
) -> None:
    """v1.3 5.1: the adapter validates the canonical input, not only the wrapper.

    The advertised schema is a key list at this seam -- the closed-property check
    reads `properties` and nothing else -- so a missing required field or an
    inadmissible value passes it and would previously have become a round trip
    whose only possible outcome was the service's own refusal. Every payload here
    names declared keys, and every one is refused on this side of the transport:
    the refusing transport is what proves the second half.
    """
    arguments: dict[str, Any] = (
        {"input": payload, "idempotency_key": "k-1"}
        if tool_name in {"memory_create", "evidence_capture", "import_start"}
        else payload
    )
    result = call(tool_name, arguments, connected=authoring_session())
    assert result.is_error is True
    assert result.structured_content is None
    assert "is not a valid document for" in result.content[0].text


@pytest.mark.parametrize(
    "payload",
    [
        {
            "source_native_id": "oversize-text",
            "media_type": "text/plain",
            # Larger than OVC1 itself, proving the adapter never serializes or
            # base64-encodes in proportion to this caller-controlled value.
            "text": "a" * (4 * 1024 * 1024 + 1),
        },
        {
            "source_native_id": "oversize-base64",
            "media_type": "text/plain",
            "content_base64": "!"
            * (4 * ((EVIDENCE_CAPTURE_MAX_CONTENT_BYTES + 2) // 3) + 4),
        },
    ],
    ids=["decoded-text", "provably-overlong-base64"],
)
def test_capture_size_refusal_is_classified_by_core_and_relayed_by_mcp(
    payload: dict[str, Any],
) -> None:
    """MCP prevalidation must not replace Core's canonical typed size branch."""

    def too_large(request: RequestEnvelope) -> ResponseEnvelope:
        expected = dict(payload)
        expected.pop("text", None)
        expected.pop("content_base64", None)
        expected["content_base64"] = "A" * (
            4 * ((EVIDENCE_CAPTURE_MAX_CONTENT_BYTES + 2) // 3) + 4
        )
        assert request.input == expected
        assert len(json.dumps(request.input).encode("utf-8")) < 4 * 1024 * 1024
        return ErrorResponseEnvelope(
            metadata=response_metadata(request),
            error=ApiError(
                code="size_limit_exceeded",
                message="capture exceeds its limit",
                retry_class="non_retryable",
            ),
        )

    transport = RecordingTransport(answer=too_large)
    result = call(
        "evidence_capture",
        {"input": payload, "idempotency_key": "k-size"},
        connected=authoring_session(transport),
    )

    assert len(transport.calls) == 1
    assert result.is_error is True
    relayed = json.loads(
        result.content[0].text.split("was refused by the service: ", 1)[1]
    )
    assert relayed["error"]["code"] == "size_limit_exceeded"
    assert relayed["error"]["retry_class"] == "non_retryable"


#: The secret each mutation carries in a *declared* field below, so the refusal
#: that follows is the contract's rather than the closed-property check's.
SECRET = "eyJzZWNyZXQiOiJkby1ub3QtZWNobyJ9"

CARRIES_SECRET: dict[str, dict[str, Any]] = {
    "memory_create": {"record_type": SECRET},
    "evidence_capture": {"source_native_id": SECRET},
    "import_start": {"source": {"staged_source_ref": SECRET}},
}


@pytest.mark.parametrize("tool_name", sorted(CARRIES_SECRET))
def test_a_contract_refusal_repeats_nothing_of_what_was_sent(tool_name: str) -> None:
    """Fixed text, like every other refusal this server writes.

    A contract error names the path it failed at and often the value, and this
    answer goes to a model over a channel the caller does not own. The advertised
    input schema already carries every constraint, so a caller reading it has
    what it needs without the server quoting the payload back.
    """
    result = call(
        tool_name,
        {"input": CARRIES_SECRET[tool_name], "idempotency_key": "k-1"},
        connected=authoring_session(),
    )
    assert result.is_error is True
    message = result.content[0].text
    assert SECRET not in message
    assert "is not a valid document for" in message


@pytest.mark.parametrize(
    "key",
    ["", " ", "k 1", "k/../1", "\n", "a" * 129, "key\u0000"],
    ids=["empty", "space", "inner-space", "path-ish", "newline", "too-long", "nul"],
)
def test_an_idempotency_key_the_envelope_refuses_never_reaches_the_client(
    key: str,
) -> None:
    """The key is checked against the envelope's own predicate before dispatch.

    `is_idempotency_key` is asked rather than a pattern restated here, so the
    advertised wrapper, the request envelope and this check are one definition.
    A key the envelope would refuse used to travel as far as `RequestMetadata`,
    where it is an exception out of a handler rather than an answer -- and for a
    write, a key that cannot settle a replay is the one thing worth refusing
    before the write happens rather than after.
    """
    arguments = dict(AUTHORING_CALLS["evidence_capture"], idempotency_key=key)
    result = call("evidence_capture", arguments, connected=authoring_session())
    assert result.is_error is True
    assert result.structured_content is None
    assert "idempotency_key" in result.content[0].text


def test_a_read_the_contract_accepts_still_carries_its_input_unchanged() -> None:
    """The check decides whether to send, never what to send.

    A decoded input is thrown away: what travels is the caller's own document, so
    a contract that tolerates a spelling this adapter does not know about keeps
    deciding that for itself.
    """
    transport = RecordingTransport()
    call(
        "job_events",
        {"job_id": "job-1", "limit": 25},
        connected=authoring_session(transport),
    )
    (request,) = transport.calls
    assert request.input == {"job_id": "job-1", "limit": 25}


def test_a_purpose_outside_the_configuration_refuses_an_authoring_tool() -> None:
    """The per-call check is unchanged by the profile: a wider inventory is not a
    wider grant, and an installation that never allowed `content_ingestion` sees
    `evidence_capture` and cannot call it."""
    narrow = authoring_session(config=configuration(mutation_enabled=True))
    assert "content_ingestion" not in narrow.configuration.allowed_purposes
    result = call(
        "evidence_capture", AUTHORING_CALLS["evidence_capture"], connected=narrow
    )
    assert result.is_error is True
    assert "purpose" in result.content[0].text


def test_a_same_key_replay_is_a_real_call_every_time() -> None:
    """No result cache here, and no automatic retry either.

    Two identical submissions are two requests on the wire, each with a fresh
    correlation identifier, because settling a repeat against a stored outcome --
    and re-checking the authority behind it -- is the service's decision and this
    adapter must not pre-empt either. A failed mutation is likewise sent once: a
    second attempt under a new key would be a second write.
    """
    transport = RecordingTransport()
    connected = authoring_session(transport)
    arguments = AUTHORING_CALLS["evidence_capture"]
    call("evidence_capture", arguments, connected=connected)
    call("evidence_capture", arguments, connected=connected)

    first, second = transport.calls
    assert first.input == second.input
    assert first.metadata.idempotency_key == second.metadata.idempotency_key
    assert first.metadata.request_id != second.metadata.request_id

    def refuse(request: RequestEnvelope) -> ResponseEnvelope:
        return ErrorResponseEnvelope(
            metadata=response_metadata(request),
            error=ApiError(
                code="service_unavailable",
                message="not now",
                retry_class="retryable_after_delay",
            ),
        )

    failing = RecordingTransport(answer=refuse)
    result = call("evidence_capture", arguments, connected=authoring_session(failing))
    assert result.is_error is True
    assert len(failing.calls) == 1, "a mutation was retried on this server's initiative"


# --- startup: the workspace must be unambiguous and must be the one served -------


def test_two_allow_listed_workspaces_without_a_default_select_none() -> None:
    """This server takes no argument that would choose between them, so it refuses.

    Choosing would be the server picking a workspace on the model's behalf, which
    is the decision R004-06 keeps out of the exposed surface entirely.
    """
    ambiguous = configuration(allowed_workspace_ids=[WORKSPACE, OTHER_WORKSPACE])
    assert ambiguous.selected_workspace_id is None
    with pytest.raises(server.StartupError, match="unambiguous"):
        server.connect(ambiguous)


def test_a_sole_allow_listed_workspace_selects_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server.ServiceClient,
        "connect",
        classmethod(lambda _cls, _config, **_kw: client(local_transport())),
    )
    assert server.connect(configuration()).workspace_id == WORKSPACE


def test_a_default_workspace_selects_itself_out_of_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server.ServiceClient,
        "connect",
        classmethod(lambda _cls, _config, **_kw: client(local_transport())),
    )
    connected = server.connect(
        configuration(
            allowed_workspace_ids=[OTHER_WORKSPACE, WORKSPACE],
            default_workspace_id=WORKSPACE,
        )
    )
    assert connected.workspace_id == WORKSPACE


def test_a_service_serving_another_workspace_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reachable, compatible, live -- and serving somebody else's workspace.

    The refusal is before MCP initialization and quotes neither identifier.
    """
    monkeypatch.setattr(
        server.ServiceClient,
        "connect",
        classmethod(
            lambda _cls, _config, **_kw: client(local_transport(), OTHER_WORKSPACE)
        ),
    )
    with pytest.raises(server.StartupError) as refusal:
        server.connect(configuration())
    assert "does not serve the selected workspace" in str(refusal.value)
    assert OTHER_WORKSPACE not in str(refusal.value)
    assert WORKSPACE not in str(refusal.value)


# --- startup: managed local ------------------------------------------------------


@dataclass
class ConnectRecorder:
    """Answers a sequence of connects and records the configs it was given."""

    answers: list[ServiceClient | None]
    configs: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.configs is None:
            self.configs = []

    def __call__(self, _cls: Any, config: Any, **_kwargs: Any) -> ServiceClient | None:
        self.configs.append(config)
        return self.answers.pop(0)


def test_managed_local_delegates_the_whole_startup_to_the_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter supplies configuration and one deadline, and owns no launcher."""
    held = local_transport()
    expected = client(held)
    seen: list[tuple[Any, Deadline]] = []

    def managed(config: Any, *, deadline: Deadline, **_kwargs: Any) -> Any:
        seen.append((config, deadline))
        return ManagedServiceConnection(client=expected, status="attached")

    monkeypatch.setattr(server, "connect_managed_local", managed)

    connected = server.connect(configuration())

    assert len(seen) == 1
    assert isinstance(seen[0][0], InstallationServiceConfig)
    assert seen[0][0].installation_state == STATE
    assert seen[0][0].workspace_id == WORKSPACE
    assert isinstance(seen[0][1], Deadline)
    assert connected.status == "attached"
    # The shared client's, presented as the dedicated principal: the session
    # carries the wrapper, and under it the very transport that came back.
    assert connected.client.transport.transport is held  # type: ignore[union-attr]


def test_managed_local_preserves_the_shared_client_start_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = local_transport()
    monkeypatch.setattr(
        server,
        "connect_managed_local",
        lambda _config, **_kwargs: ManagedServiceConnection(
            client=client(held), status="started"
        ),
    )

    connected = server.connect(configuration())

    assert connected.status == "started"
    assert connected.client.transport.transport is held  # type: ignore[union-attr]


def test_a_shared_managed_start_failure_becomes_a_fixed_startup_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_config: Any, **_kwargs: Any) -> Any:
        raise ManagedStartError("contains " + ENDPOINT + " and " + str(STATE))

    monkeypatch.setattr(server, "connect_managed_local", fail)
    with pytest.raises(server.StartupError, match="could not be started") as refusal:
        server.connect(configuration())
    assert ENDPOINT not in str(refusal.value)
    assert str(STATE) not in str(refusal.value)


def install(monkeypatch: pytest.MonkeyPatch, recorder: ConnectRecorder) -> None:
    """Put `recorder` behind remote-mode `ServiceClient.connect`."""
    monkeypatch.setattr(server.ServiceClient, "connect", classmethod(recorder))


# --- startup: the remote service client -----------------------------------------


def resolver(_reference: CredentialReference, _origin: str) -> Credential:
    return Credential("a-test-secret")


def test_remote_mode_without_an_injected_resolver_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console entry point has no resolver, and there is nowhere else to look.

    No environment variable, no argv secret, no file beside the configuration and
    no credential in the document: a remote endpoint with no injected resolver is
    refused before MCP initialization, and before any connect is attempted.
    """
    recorder = ConnectRecorder([client(RecordingTransport())])
    install(monkeypatch, recorder)

    with pytest.raises(server.StartupError, match="credential resolver"):
        server.connect(remote_configuration())
    assert recorder.configs == []


def test_remote_mode_connects_through_the_shared_client_with_a_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint and the credential *name* come from the configuration.

    The secret exists only inside the cache this function built from the injected
    resolver, and the endpoint the client is given is the configuration's
    normalized origin rather than the text as written.
    """
    recorder = ConnectRecorder([client(RecordingTransport())])
    install(monkeypatch, recorder)

    connected = server.connect(remote_configuration(), credential_resolver=resolver)

    (config,) = recorder.configs
    assert isinstance(config, HttpServiceConfig)
    assert config.endpoint_uri == "https://core.example.com:443"
    assert config.credential_reference == CredentialReference(REFERENCE)
    assert isinstance(config.credentials, CredentialCache)
    assert connected.credentials is config.credentials
    assert connected.status == "connected"


def test_the_credential_cache_is_cleared_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = ConnectRecorder([client(RecordingTransport())])
    install(monkeypatch, recorder)
    connected = server.connect(remote_configuration(), credential_resolver=resolver)
    cache = connected.credentials
    assert cache is not None

    cache.credential_for(CredentialReference(REFERENCE), "https://core.example.com:443")
    assert "entries=1" in repr(cache)

    connected.clear_credentials()
    assert "entries=0" in repr(cache)


@pytest.mark.parametrize("failure", ["unreachable", "wrong_workspace"])
def test_a_failed_remote_startup_clears_the_cache(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Two ways a remote startup ends without a session, and neither leaves a
    resolved secret behind: a connect that raised, and a service that answered
    for another workspace."""
    caches: list[CredentialCache] = []
    real_cache = server.CredentialCache

    def record(*args: Any, **kwargs: Any) -> CredentialCache:
        cache = real_cache(*args, **kwargs)
        cache.credential_for(
            CredentialReference(REFERENCE), "https://core.example.com:443"
        )
        caches.append(cache)
        return cache

    def connect(_cls: Any, _config: Any, **_kwargs: Any) -> ServiceClient:
        if failure == "unreachable":
            raise TransportError("the endpoint could not be reached")
        return client(RecordingTransport(), OTHER_WORKSPACE)

    monkeypatch.setattr(server, "CredentialCache", record)
    monkeypatch.setattr(server.ServiceClient, "connect", classmethod(connect))

    with pytest.raises((server.StartupError, TransportError)):
        server.connect(remote_configuration(), credential_resolver=resolver)

    (cache,) = caches
    assert "entries=0" in repr(cache), "a refused startup left a resolved credential"


# --- the shape of the integration itself ----------------------------------------


def test_both_modes_go_through_the_shared_service_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One composition, two configurations, and no transport built in this package.

    The source-level half matters as much as the behavioural one: a package that
    imported a transport directly would be free to grow a second dial loop later,
    whatever this test observed today.
    """
    recorder = ConnectRecorder([client(local_transport())])
    install(monkeypatch, recorder)
    local = server.connect(configuration())
    assert isinstance(local.client, ServiceClient)

    recorder.answers.append(client(RecordingTransport()))
    remote = server.connect(remote_configuration(), credential_resolver=resolver)
    assert isinstance(remote.client, ServiceClient)

    source = Path(server.__file__).read_text(encoding="utf-8")
    for absent in (
        "LocalIpcTransport",
        "HttpTransport",
        "socket_path_for",
        "read_local_descriptor",
        "discover_endpoint",
    ):
        assert absent not in source, absent
    assert not hasattr(server, "TransportFactory")


def test_the_session_is_immutable() -> None:
    """Nothing serving a session may swap the service or the authority under it."""
    connected = session()
    with pytest.raises(AttributeError):
        connected.workspace_id = "ws-something-else"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        connected.configuration = configuration()  # type: ignore[misc]


def test_clearing_credentials_is_safe_when_there_are_none() -> None:
    """Local mode holds no cache, and shutdown does not have to know that."""
    session().clear_credentials()


# --- the installed managed-local principal --------------------------------------
#
# A `managed_local` configuration the installed setup path wrote names a
# credential this installation holds in its own protected store. Everything below
# is about what that name does and, just as importantly, what its *absence* does:
# a configuration without one has no dedicated principal to call as, and an
# installed server presents its own bearer or does not run at all.


def installed_store() -> InstalledCredentialStore:
    """The store `installed_state` filed this session's credential into."""
    return InstalledCredentialStore(STATE)


def stored_files() -> list[Path]:
    return sorted((STATE / "runtime" / ".installed-credentials").iterdir())


def local_transport() -> LocalIpcTransport:
    """A real local transport that is never dialled by the tests that hold one."""
    return LocalIpcTransport(endpoint_uri="unix:///nonexistent/omnivia/s.sock")


def attach(monkeypatch: pytest.MonkeyPatch, transport: Any) -> ServiceClient:
    """Make managed-local startup hand back a client over `transport`."""
    connected = client(transport)
    monkeypatch.setattr(
        server,
        "connect_managed_local",
        lambda *_a, **_k: ManagedServiceConnection(client=connected, status="attached"),
    )
    return connected


def test_an_installed_configuration_calls_as_its_dedicated_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every application call is wrapped; nothing above the transport changes.

    `ConnectedSession` and every handler still go through `ServiceClient.call` --
    what that reaches is the authenticated local control rather than the plain
    application path, so the service dispatches under the dedicated principal
    instead of its own.
    """
    held = local_transport()
    attach(monkeypatch, held)

    connected = server.connect(configuration())

    assert isinstance(connected.client.transport, AuthenticatedLocalTransport)
    assert connected.client.transport.transport is held
    assert connected.client.transport.credential().reveal() == INSTALLED_SECRET


@pytest.mark.parametrize("mutation_enabled", [False, True])
def test_a_configuration_with_no_reference_gets_no_session_at_all(
    monkeypatch: pytest.MonkeyPatch, mutation_enabled: bool
) -> None:
    """The pre-setup shape is a refusal now, not an unauthenticated session.

    A managed-local endpoint accepts the plain application path, so the fallback
    this replaces was a *working* session dispatching as whatever the service
    itself runs as -- the service's own administrator identity, silently, with
    `authoring` the only thing it could not reach. Both bytes of
    `mutation_enabled` end the same way: no session comes back, and
    `RefusingTransport` proves no call was issued on the way to saying so.
    """
    attach(monkeypatch, RefusingTransport())

    with pytest.raises(server.StartupError) as refused:
        server.connect(legacy_configuration(mutation_enabled=mutation_enabled))

    assert "no usable credential" in str(refused.value)


def test_the_legacy_document_is_still_read_and_only_refused_at_startup() -> None:
    """The refusal is `connect`'s, not the reader's, and that split is deliberate.

    `parse_configuration` describes a document it can describe -- a legacy
    installation's configuration is well-formed and says exactly what it means --
    and the server is what declines to run on it. A reader that refused instead
    would make an upgrade look like a corrupt file.
    """
    config = legacy_configuration()
    assert config.service_mode == "managed_local"
    assert config.credential_reference is None


def test_the_bearer_is_read_again_for_every_call_rather_than_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation and revocation land on the next call, not at the next restart."""
    store = installed_store()
    attach(monkeypatch, local_transport())

    connected = server.connect(configuration())
    source = connected.client.transport.credential  # type: ignore[union-attr]

    store.store(CredentialReference(INSTALLED_REFERENCE), Credential(ROTATED_SECRET))
    assert source().reveal() == ROTATED_SECRET

    store.remove(CredentialReference(INSTALLED_REFERENCE))
    with pytest.raises(CredentialMissingError):
        source()


@pytest.mark.parametrize(
    "damage",
    ["no-store", "revoked", "unreadable", "not-a-credential", "wrong-reference"],
)
def test_a_credential_this_installation_cannot_produce_fails_closed_at_startup(
    monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """Before one tool is advertised, and without falling back to the service.

    The refusal is one fixed sentence for every cause, because the reference, the
    store and the bytes are all things it reaches a host's stderr carrying.
    """
    store = installed_store()
    reference = CredentialReference(INSTALLED_REFERENCE)
    if damage == "no-store":
        for path in stored_files():
            path.unlink()
        (STATE / "runtime" / ".installed-credentials").rmdir()
    elif damage == "revoked":
        store.remove(reference)
    elif damage == "unreadable":
        for path in stored_files():
            path.chmod(0o644)
    elif damage == "not-a-credential":
        for path in stored_files():
            path.write_bytes(b"not a credential\n")
            path.chmod(0o600)
    elif damage == "wrong-reference":
        store.remove(reference)
        store.store(
            CredentialReference("omcp-some-other-reference"),
            Credential(INSTALLED_SECRET),
        )

    attach(monkeypatch, local_transport())

    with pytest.raises(server.StartupError) as refused:
        server.connect(configuration())
    rendered = " ".join((str(refused.value), repr(refused.value.args)))
    assert "no usable credential" in rendered
    for absent in (INSTALLED_SECRET, str(STATE), INSTALLED_REFERENCE):
        assert absent not in rendered


def test_a_startup_refusal_never_leaves_a_session_calling_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no fallback branch: a missing credential is a refusal, not a downgrade."""
    installed_store().remove(CredentialReference(INSTALLED_REFERENCE))
    attach(monkeypatch, RefusingTransport())
    with pytest.raises(server.StartupError):
        server.connect(configuration())


# --- the production authoring admission -----------------------------------------


@dataclass
class AdmissionPeer:
    """Stands in for `mcp_authoring_admission`, recording what it was presented."""

    answer: Any
    seen: list[tuple[Any, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.seen is None:
            self.seen = []

    def __call__(self, transport: Any, credential: str, **_kwargs: Any) -> Any:
        self.seen.append((transport, credential))
        return self.answer


def admission_result(
    *,
    admitted: bool = True,
    principal_id: str = PRINCIPAL,
    workspace_id: str = WORKSPACE,
) -> AuthoringAdmissionResult:
    return AuthoringAdmissionResult(
        admitted=admitted, principal_id=principal_id, workspace_id=workspace_id
    )


def test_no_reference_means_no_admission_to_inject() -> None:
    """Nothing to present means nothing to ask with.

    A remote configuration's credential is the injecting host's rather than this
    installation's, so no admission is built and `restricted` is the only profile
    `effective_profile` can reach. A managed-local configuration with no reference
    answers the same way -- and never gets as far as a profile, because
    :func:`connect` has already refused it.
    """
    assert server._installed_admission(remote_configuration()) is None
    assert server._installed_admission(legacy_configuration()) is None
    assert server._installed_store(legacy_configuration()) is None


def test_the_admission_presents_the_bearer_over_the_session_s_own_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = local_transport()
    peer = AdmissionPeer(admission_result())
    monkeypatch.setattr(server, "mcp_authoring_admission", peer)

    admission = server._installed_admission(configuration())
    assert admission is not None
    wrapped = authenticated_client(client(held), lambda: Credential(INSTALLED_SECRET))

    assert admission(wrapped, PRINCIPAL, WORKSPACE) is True
    assert peer.seen == [(held, INSTALLED_SECRET)]


@pytest.mark.parametrize(
    ("answer", "admitted"),
    [
        (admission_result(), True),
        (admission_result(admitted=False), False),
        (admission_result(principal_id="some-other-principal"), False),
        (admission_result(workspace_id="ws-some-other-workspace"), False),
        (admission_result(principal_id="", workspace_id=""), False),
    ],
)
def test_admission_requires_the_protected_answer_to_name_this_very_session(
    monkeypatch: pytest.MonkeyPatch,
    answer: AuthoringAdmissionResult,
    admitted: bool,
) -> None:
    """A true `admitted` for somebody else is an answer about somebody else.

    Accepting it would let a credential filed for one workspace author in
    another, so both identifiers are compared against the trusted configuration's
    -- which no prompt, argument or tool call can reach.
    """
    monkeypatch.setattr(server, "mcp_authoring_admission", AdmissionPeer(answer))
    admission = server._installed_admission(configuration())
    assert admission is not None
    wrapped = authenticated_client(
        client(local_transport()), lambda: Credential(INSTALLED_SECRET)
    )
    assert admission(wrapped, PRINCIPAL, WORKSPACE) is admitted


def test_a_revoked_credential_makes_the_admission_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution is fresh inside the admission, so a revocation is felt there too."""
    store = installed_store()
    peer = AdmissionPeer(admission_result())
    monkeypatch.setattr(server, "mcp_authoring_admission", peer)
    admission = server._installed_admission(configuration())
    assert admission is not None
    wrapped = authenticated_client(
        client(local_transport()), lambda: Credential(INSTALLED_SECRET)
    )

    store.remove(CredentialReference(INSTALLED_REFERENCE))
    with pytest.raises(CredentialMissingError):
        admission(wrapped, PRINCIPAL, WORKSPACE)
    assert peer.seen == []


# --- the whole startup decision -------------------------------------------------


@pytest.mark.parametrize(
    ("mutation_enabled", "answer", "profile"),
    [
        # The ceiling and the protected floor together, and nothing less.
        (True, admission_result(), "authoring"),
        (True, admission_result(admitted=False), "restricted"),
        (True, admission_result(principal_id="elsewhere"), "restricted"),
        (True, admission_result(workspace_id="ws-elsewhere"), "restricted"),
        (False, admission_result(), "restricted"),
    ],
)
def test_the_profile_a_started_server_freezes(
    monkeypatch: pytest.MonkeyPatch,
    mutation_enabled: bool,
    answer: AuthoringAdmissionResult,
    profile: str,
) -> None:
    """Every profile a server can actually start with, and every row names one.

    There is no unreferenced row any more: a managed-local configuration with no
    credential has no profile to freeze because it has no session --
    `test_a_configuration_with_no_reference_gets_no_session_at_all` is that case.
    """
    monkeypatch.setattr(server, "mcp_authoring_admission", AdmissionPeer(answer))
    attach(monkeypatch, local_transport())

    config = configuration(
        mutation_enabled=mutation_enabled,
        allowed_purposes=list(AUTHORING_PURPOSES),
    )
    connected = server.connect(
        config, authoring_admission=server._installed_admission(config)
    )
    assert connected.profile == profile
    assert [tool.name for tool in server.tools(connected.profile)] == [
        tool.name for tool in server.tools(profile)
    ]


def test_public_intent_alone_never_reaches_a_mutation_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully installed server with `mutation_enabled: true` and no protected record.

    The one case the public document can produce on its own now that a credential
    is mandatory: everything the editable file can say is said, the bearer
    resolves, the session starts -- and the protected authority has recorded
    nothing, so the profile is `restricted`. Not merely unlisted: a restricted
    session has no way to resolve a mutation tool's name to an operation at all,
    so the call is refused by the allow-list before a request exists.
    """
    monkeypatch.setattr(
        server,
        "mcp_authoring_admission",
        AdmissionPeer(admission_result(admitted=False)),
    )
    attach(monkeypatch, local_transport())
    config = configuration(
        mutation_enabled=True,
        allowed_purposes=list(AUTHORING_PURPOSES),
    )
    connected = server.connect(
        config, authoring_admission=server._installed_admission(config)
    )
    assert connected.profile == "restricted"
    for tool_name in sorted(ADMITTED_MUTATIONS):
        result = call(tool_name, {}, connected=connected)
        assert result.is_error is True
        assert "is not a tool this server exposes" in result.content[0].text  # type: ignore[union-attr]


def test_an_installed_server_never_calls_as_the_service_s_own_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point, asserted where a regression would actually land.

    A future edit that dropped the wrapper -- or added a fallback for a transport
    the authenticated control cannot travel -- would leave the session calling
    over the plain application path as whatever the service runs as. Both halves
    fail here if that ever happens: a local transport must come back wrapped, and
    one the control has no form for must be refused rather than used bare.
    """
    held = local_transport()
    attach(monkeypatch, held)
    connected = server.connect(configuration())
    assert connected.client.transport is not held
    assert isinstance(connected.client.transport, AuthenticatedLocalTransport)

    attach(monkeypatch, RecordingTransport())
    with pytest.raises(TransportError):
        server.connect(configuration())


@dataclass
class ExchangingPeer:
    """Stands where the local endpoint does, recording the document written to it.

    `call_authenticated` reaches a transport through `exchange` and nothing else,
    so this is the whole of what an installed session puts on the wire -- built by
    the real wrapper, from the real request, with the real bearer.
    """

    answer: Any = None
    exchanges: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.exchanges is None:
            self.exchanges = []

    def exchange(
        self,
        document: dict[str, Any],
        *,
        deadline: Any,
        cancellation: Any = None,
        operation: str,
    ) -> dict[str, Any]:
        self.exchanges.append(document)
        return self.answer(document)


def test_an_installed_session_dispatches_every_tool_call_authenticated() -> None:
    """The application request reaches the wire inside an authenticated control.

    Not a probe, not the plain application path, and carrying the bearer the store
    holds -- with the request itself the contract-encoded envelope the tool built.
    """

    def answer(document: dict[str, Any]) -> dict[str, Any]:
        request = codec.decode_request(document["request"])
        # Built through the codec in both directions, so what comes back is a
        # document a real service could have written and this package decodes.
        refs = (CapabilityRef(id="workspace.read", version="1.0"),)
        metadata = ResponseMetadata(
            request_id=request.metadata.request_id,
            correlation_id=request.metadata.correlation_id,
            version=VersionCapabilityEnvelope(
                api_version=request.metadata.api_version,
                server_version="0.1.0",
                workspace_format_version="1.0",
                compatibility=CompatibilityMetadata(
                    selected_api_version=request.metadata.api_version,
                    selected_workspace_version="1.0",
                    supported_api_versions=VersionWindow(
                        minimum=request.metadata.api_version,
                        maximum=request.metadata.api_version,
                    ),
                    supported_workspace_versions=VersionWindow(
                        minimum="1.0", maximum="1.0"
                    ),
                    status="compatible",
                    upgrade_state=UpgradeState(value="none"),
                    deprecations=(),
                ),
                capabilities=CapabilitySet(
                    supported=refs, granted=refs, effective=refs
                ),
            ),
            # The dedicated principal, as the service reports who it dispatched
            # under -- not the service's own worker or administrator identity.
            authority=GrantedAuthority(
                principal_id=PRINCIPAL, roles=(), capabilities=refs
            ),
        )
        return {
            "local_control_result": "omnivia.local-control.v1",
            "kind": "application.call",
            "result": {
                "response": codec.encode_response(
                    SuccessResponseEnvelope(metadata=metadata, result={"workspace": {}})
                )
            },
        }

    peer = ExchangingPeer(answer=answer)
    store = installed_store()
    reference = CredentialReference(INSTALLED_REFERENCE)
    connected = session(
        AuthenticatedLocalTransport(
            transport=peer,  # type: ignore[arg-type]
            credential=lambda: store.resolve(reference),
        )
    )

    result = call("workspace_inspect", {}, connected=connected)

    assert result.is_error is not True
    assert len(peer.exchanges) == 1
    written = peer.exchanges[0]
    assert written["local_control"] == "omnivia.local-control.v1"
    assert written["kind"] == "application.call"
    assert written["credential"] == INSTALLED_SECRET
    assert written["request"]["operation"] == "workspace.inspect"
    assert written["request"]["metadata"]["principal_claim"][
        "claimed_principal_id"
    ] == (PRINCIPAL)


def test_a_revoked_credential_fails_the_next_tool_call_rather_than_the_next_restart() -> (
    None
):
    """No session to expire and no cached bearer: the store is read per call."""
    store = installed_store()
    reference = CredentialReference(INSTALLED_REFERENCE)
    peer = ExchangingPeer(
        answer=lambda _document: pytest.fail("nothing should be sent")
    )
    connected = session(
        AuthenticatedLocalTransport(
            transport=peer,  # type: ignore[arg-type]
            credential=lambda: store.resolve(reference),
        )
    )

    store.remove(reference)
    result = call("workspace_inspect", {}, connected=connected)

    assert result.is_error is True
    assert peer.exchanges == []
    rendered = result.content[0].text  # type: ignore[union-attr]
    assert "could not be called" in rendered
    for absent in (INSTALLED_SECRET, INSTALLED_REFERENCE, str(STATE)):
        assert absent not in rendered


def test_no_command_line_option_can_carry_a_credential() -> None:
    """The only argument is a path to the trusted document, and it holds a name.

    A secret must never reach a process argument: an argument vector is readable
    by every process this user runs and is copied into shells, logs and crash
    reports. There is no option here to put one in, and the document the one
    option names carries a reference rather than material.
    """
    options = {
        option
        for action in server.build_parser()._actions
        for option in action.option_strings
    }
    assert options == {"-h", "--help", "--config"}
    for option in options:
        for forbidden in ("credential", "secret", "token", "bearer", "password", "key"):
            assert forbidden not in option
