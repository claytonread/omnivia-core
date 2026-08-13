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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mcp_types as types
import pytest
from omnivia_core_client import (
    CancellationToken,
    Credential,
    CredentialCache,
    CredentialReference,
    Deadline,
    HttpServiceConfig,
    InstallationServiceConfig,
    ManagedServiceConnection,
    ManagedStartError,
    NegotiatedEndpoint,
    ServiceClient,
    TransportError,
)
from omnivia_core_mcp import server
from omnivia_core_mcp.configuration import McpConfiguration, parse_configuration
from omnivia_core_mcp.manifest import EXPOSURE_MANIFEST

from omnivia_core.contracts.v1 import (
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
)

WORKSPACE = "ws-authority-01"
OTHER_WORKSPACE = "ws-somebody-else-01"
PRINCIPAL = "mcp-authority-principal"
STATE = Path("/srv/omnivia/installation-state")
ENDPOINT = "https://core.example.com"
REFERENCE = "core-api"

ALL_PURPOSES = ["workspace_inspection", "knowledge_retrieval"]


# --- the trusted configuration, as a document -----------------------------------


def configuration(**overrides: Any) -> McpConfiguration:
    """One validated configuration, parsed from a document rather than built.

    Through `parse_configuration` on purpose: a test that constructed
    `McpConfiguration` directly could assemble a combination the document reader
    would never accept, and would then be proving something about a shape that
    cannot reach the server.
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
) -> server.ConnectedSession:
    return server.ConnectedSession(
        configuration=config if config is not None else configuration(),
        client=client(RefusingTransport() if transport is None else transport),
        workspace_id=WORKSPACE,
        status="attached",
        credentials=credentials,
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


def test_the_listing_does_not_vary_with_the_configured_purposes() -> None:
    """R004-06 determinism: one package version, one listing, whatever is granted.

    A listing filtered by authority would differ between two hosts running the
    same build, and a model would have no way to tell a tool it may not call from
    a tool that does not exist. The purpose is enforced on call instead.
    """
    from omnivia_core_mcp.manifest import tools

    narrow = server.build_server(
        session=session(config=configuration(allowed_purposes=["workspace_inspection"]))
    )
    wide = server.build_server(session=session())
    assert narrow is not wide
    assert [tool.name for tool in tools()] == [
        entry.tool_name for entry in EXPOSURE_MANIFEST
    ]


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
        classmethod(lambda _cls, _config, **_kw: client(RecordingTransport())),
    )
    assert server.connect(configuration()).workspace_id == WORKSPACE


def test_a_default_workspace_selects_itself_out_of_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server.ServiceClient,
        "connect",
        classmethod(lambda _cls, _config, **_kw: client(RecordingTransport())),
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
            lambda _cls, _config, **_kw: client(RecordingTransport(), OTHER_WORKSPACE)
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
    expected = client(RecordingTransport())
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
    assert connected.client is expected


def test_managed_local_preserves_the_shared_client_start_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = client(RecordingTransport())
    monkeypatch.setattr(
        server,
        "connect_managed_local",
        lambda _config, **_kwargs: ManagedServiceConnection(
            client=expected, status="started"
        ),
    )

    connected = server.connect(configuration())

    assert connected.status == "started"
    assert connected.client is expected


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
    recorder = ConnectRecorder([client(RecordingTransport())])
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
