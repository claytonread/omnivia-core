"""V06-6: turning one frozen command into one call on a connected client.

`dispatch.py` owns four decisions, and each one is asserted here against a real
:class:`~omnivia_core_client.ServiceClient` holding a recording transport --
production takes exactly the object these tests build, and nothing here stands
in for `dispatch_application`, `dispatch_probe` or the client's own `call`.

- **The catalogue is the source of the claims.** Every one of the twenty
  commands is dispatched and the envelope compared against
  `get_operation_metadata(command.operation)` -- scopes by value, the
  capability requirement by *identity*, because the module's claim is that it
  passes the catalogue's own object through rather than assembling an equal one.
  The scope kind is what decides whether a workspace is named at all, and the
  contract's own `validate_operation_scope_workspace_id` is run over each
  request to show the agreement is the service's rule and not a local one.
- **An answer must be an answer to the call that was made.** Request,
  correlation and trace id are one value; a response echoing a different request
  or correlation id is refused.
- **Transport belongs to the caller.** The deadline and the token are asserted
  by identity rather than by value: "the caller's whole-call budget reached the
  wire" is a statement about the object, and a re-derived equal one would
  satisfy a value check.
- **Failures that are the peer's stay the peer's.** An error response envelope
  comes back as the identical object, and every refusal this module does own
  carries one fixed sentence with nothing of the caller's or the peer's in it.

The clock is injected, so `deadline_ms` is a fixed number rather than whatever
the test run happened to take. Nothing here opens a socket, a file or a process.
"""

from __future__ import annotations

import ast
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_cli import dispatch
from omnivia_core_cli.dispatch import (
    DispatchError,
    dispatch_application,
    dispatch_probe,
    find_application_command,
    find_probe_command,
)
from omnivia_core_cli.surface import (
    APPLICATION_COMMANDS,
    PROBE_COMMANDS,
    ApplicationCommand,
    ProbeCommand,
)
from omnivia_core_client import (
    CancellationToken,
    Deadline,
    NegotiatedEndpoint,
    ServiceClient,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    SCOPE_KIND_INSTALLATION,
    SCOPE_KIND_WORKSPACE,
    ApiError,
    CapabilityRef,
    CapabilitySet,
    CompatibilityMetadata,
    ErrorResponseEnvelope,
    GrantedAuthority,
    MutationPrecondition,
    PrincipalClaim,
    RequestEnvelope,
    ResponseEnvelope,
    ResponseMetadata,
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProbeResult,
    SuccessResponseEnvelope,
    UpgradeState,
    VersionCapabilityEnvelope,
    VersionWindow,
    get_operation_metadata,
    validate_operation_scope_workspace_id,
)

WORKSPACE = "ws-dispatch-01"
OTHER_WORKSPACE = "ws-dispatch-other-02"
PRINCIPAL = "dispatch-principal"

#: The one sentence a refusal from this module says, restated here rather than
#: imported: a test that read the module's own constant would pass unchanged if
#: that constant were replaced by a payload-quoting f-string.
DIAGNOSTIC = "the CLI did not dispatch this call"

#: The two commands the catalogue scopes to the installation, named rather than
#: derived. `validate_operation_scope_workspace_id` refuses a workspace on
#: these, so naming the descriptor's workspace unconditionally would make
#: exactly these two refusable by construction.
INSTALLATION_OPERATIONS = frozenset({"workspace.list", "workspace.create"})

#: Everything `dispatch.py` is allowed to import, by top-level module. An
#: allowlist rather than a denylist: the claim is that this module reaches the
#: public contracts and the client package and nothing else, and a denylist
#: would only refuse the badness someone thought to write down.
ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "collections",
        "typing",
        "uuid",
        "omnivia_core",
        "omnivia_core_cli",
        "omnivia_core_client",
    }
)

#: Named anyway, so a failure says *which* boundary was crossed rather than
#: only that an import was not on the list.
FORBIDDEN_IMPORTS = (
    "omnivia_core_runtime",
    "omnivia_core_mcp",
    "discovery",
    "local_ipc",
    "http_transport",
    "framing",
    "os",
    "pathlib",
    "shutil",
    "socket",
    "sqlite3",
    "subprocess",
    "tempfile",
    "threading",
    "lease",
    "lock",
    "storage",
)


# --------------------------------------------------------------------------
# The documents, and a real client over a transport that records
# --------------------------------------------------------------------------


def descriptor(workspace_id: str = WORKSPACE) -> ServiceEndpointDescriptor:
    return ServiceEndpointDescriptor(
        descriptor_version=CONTRACT_VERSION,
        workspace_id=workspace_id,
        service_instance_id="svc-dispatch",
        installation_id="inst-dispatch",
        endpoint_uri="unix:///tmp/omnivia-dispatch/s.sock",
        protocol_version="1.0",
        server_version="0.1.0",
        supported_api_versions=VersionWindow(minimum="1.0", maximum=CONTRACT_VERSION),
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
            api_version=CONTRACT_VERSION,
            server_version="0.1.0",
            workspace_format_version="1",
            compatibility=CompatibilityMetadata(
                selected_api_version=CONTRACT_VERSION,
                selected_workspace_version="1",
                supported_api_versions=VersionWindow(
                    minimum="1.0", maximum=CONTRACT_VERSION
                ),
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


def success(request: RequestEnvelope, **overrides: Any) -> SuccessResponseEnvelope:
    return SuccessResponseEnvelope(
        metadata=response_metadata(request, **overrides), result={"ok": True}
    )


def echo(request: ServiceProbeRequest, **overrides: Any) -> ServiceProbeResult:
    """The probe answer a runtime gives: the caller's own id back under `details`."""
    fields: dict[str, Any] = {
        "probe": request.probe,
        "status": "pass",
        "server_version": "0.1.0",
        "api_version": CONTRACT_VERSION,
        "observed_at": "2026-01-01T00:00:00Z",
        "details": {"request_id": request.request_id},
    }
    fields.update(overrides)
    return ServiceProbeResult(**fields)


class RecordingTransport:
    """A `ClientTransport` that records the exact objects it was handed.

    `answer` and `probe_answer` take the request, so a response that does *not*
    correlate is producible without the transport having to be replaced.
    """

    def __init__(
        self,
        *,
        answer: Callable[[RequestEnvelope], ResponseEnvelope] | None = None,
        probe_answer: Callable[[ServiceProbeRequest], ServiceProbeResult] | None = None,
    ) -> None:
        self.answer = success if answer is None else answer
        self.probe_answer = echo if probe_answer is None else probe_answer
        self.calls: list[
            tuple[RequestEnvelope, Deadline, CancellationToken | None]
        ] = []
        self.probes: list[
            tuple[ServiceProbeRequest, Deadline, CancellationToken | None]
        ] = []

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        self.calls.append((request, deadline, cancellation))
        return self.answer(request)

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        self.probes.append((request, deadline, cancellation))
        return self.probe_answer(request)


class RefusingTransport:
    """Fails the test if a refused call ever reaches the wire."""

    def call(self, request: RequestEnvelope, **_kwargs: Any) -> ResponseEnvelope:
        raise AssertionError("a refused call must not reach the client")

    def probe(self, request: Any, **_kwargs: Any) -> ServiceProbeResult:
        raise AssertionError("a refused probe must not reach the transport")


def client(transport: Any, workspace_id: str = WORKSPACE) -> ServiceClient:
    """The real shared client, assembled around a transport a test can watch."""
    return ServiceClient(
        transport=transport,
        descriptor=descriptor(workspace_id),
        negotiated=NegotiatedEndpoint(
            api_version=CONTRACT_VERSION,
            protocol_version="1.0",
            descriptor_version=CONTRACT_VERSION,
        ),
    )


def deadline(seconds: float = 2.5) -> Deadline:
    """A deadline on a stopped clock, so `deadline_ms` is a fixed number."""
    return Deadline.after(seconds, clock=lambda: 0.0)


def assert_refusal(error: BaseException) -> None:
    """The one fixed sentence, and nothing chained behind it to carry more."""
    assert str(error) == DIAGNOSTIC
    assert error.args == (DIAGNOSTIC,)
    assert error.__cause__ is None
    assert error.__context__ is None


def assert_payload_free(error: BaseException, *planted: str) -> None:
    """:func:`assert_refusal`, plus: none of these strings anywhere it renders.

    Planted values are distinctive by construction. A generic path segment like
    ``list`` or ``core`` occurs in the module paths a traceback renders and in
    the diagnostic itself -- ``call`` contains ``all`` -- so asserting on one
    would fail for a reason that says nothing about what the exception carries.
    """
    assert_refusal(error)
    rendered = "".join(traceback.TracebackException.from_exception(error).format())
    for secret in planted:
        for exposed in (str(error), repr(error.args), rendered):
            assert secret not in exposed, secret


def sent(transport: RecordingTransport) -> RequestEnvelope:
    """The one request that was carried, refusing to answer if there were others."""
    assert len(transport.calls) == 1
    return transport.calls[0][0]


# --------------------------------------------------------------------------
# 1. Lookup is exact, and everything else is refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command", APPLICATION_COMMANDS, ids=lambda c: "/".join(c.path)
)
def test_every_application_command_path_resolves_to_its_own_command(
    command: ApplicationCommand,
) -> None:
    assert find_application_command(command.path) is command


@pytest.mark.parametrize("command", PROBE_COMMANDS, ids=lambda c: "/".join(c.path))
def test_every_probe_command_path_resolves_to_its_own_command(
    command: ProbeCommand,
) -> None:
    assert find_probe_command(command.path) is command


def test_the_two_lookups_are_declared_and_cover_all_twenty_three_paths() -> None:
    """Twenty application paths and three probe paths, and no path in both."""
    application = {command.path for command in APPLICATION_COMMANDS}
    probes = {command.path for command in PROBE_COMMANDS}
    assert len(application) == 20
    assert len(probes) == 3
    assert not application & probes


UNKNOWN_PATHS = (
    (),
    ("workspace",),
    ("memory",),
    ("governance",),
    ("workspace", "list", "all"),
    ("memory", "create", "now"),
    ("workspace", "lis"),
    ("workspace", "List"),
    ("WORKSPACE", "list"),
    ("workspace.list",),
    ("list", "workspace"),
    ("not-a-command", "at-all"),
)

#: The `core.*` names v1 no longer publishes, in the shapes a path could take.
HISTORICAL_CORE_PATHS = (
    ("core", "workspace", "list"),
    ("core", "memory", "create"),
    ("core", "memory", "search"),
    ("core.memory.create",),
    ("core.workspace.list",),
    ("core", "service", "health"),
)


@pytest.mark.parametrize("path", UNKNOWN_PATHS + HISTORICAL_CORE_PATHS)
def test_an_unmatched_application_path_is_refused(path: tuple[str, ...]) -> None:
    """No prefix match, no abbreviation, no case folding, no nearest neighbour."""
    with pytest.raises(DispatchError) as raised:
        find_application_command(path)
    assert_refusal(raised.value)


@pytest.mark.parametrize(
    "path",
    UNKNOWN_PATHS
    + HISTORICAL_CORE_PATHS
    + (("service",), ("service", "health", "now"), ("service", "healthz"))
    # A probe lookup must not answer for an application path either.
    + tuple(command.path for command in APPLICATION_COMMANDS),
)
def test_an_unmatched_probe_path_is_refused(path: tuple[str, ...]) -> None:
    with pytest.raises(DispatchError) as raised:
        find_probe_command(path)
    assert_refusal(raised.value)


@pytest.mark.parametrize("path", tuple(command.path for command in PROBE_COMMANDS))
def test_an_application_lookup_does_not_answer_for_a_probe_path(
    path: tuple[str, ...],
) -> None:
    """A probe is not a catalogue operation; the two lists stay apart."""
    with pytest.raises(DispatchError) as raised:
        find_application_command(path)
    assert_refusal(raised.value)


#: A path segment that could only have come from the caller, so finding it in a
#: rendered failure proves the refusal quoted what it was given.
PLANTED_SEGMENT = "s3cret-workspace-nobody-should-see"


@pytest.mark.parametrize(
    "path",
    [
        (PLANTED_SEGMENT,),
        ("workspace", PLANTED_SEGMENT),
        ("workspace", "list", PLANTED_SEGMENT),
    ],
)
def test_a_refused_lookup_quotes_nothing_the_caller_supplied(
    path: tuple[str, ...],
) -> None:
    """Naming the path would put caller material in an exception a host may log."""
    with pytest.raises(DispatchError) as raised:
        find_application_command(path)
    assert_payload_free(raised.value, PLANTED_SEGMENT)

    with pytest.raises(DispatchError) as raised:
        find_probe_command(path)
    assert_payload_free(raised.value, PLANTED_SEGMENT)


def test_a_forged_application_command_is_refused_before_the_client_call() -> None:
    """A real path carrying an operation or purpose the surface never declared."""
    forged = ApplicationCommand(
        ("memory", "get"), "core.memory.get", "purpose-nobody-declared"
    )
    with pytest.raises(DispatchError) as raised:
        dispatch_application(
            client(RefusingTransport()),
            forged,
            payload={"memory_id": "mem-forged-nobody-should-see"},
            deadline=deadline(),
        )
    assert_payload_free(
        raised.value, "purpose-nobody-declared", "mem-forged-nobody-should-see"
    )


def test_a_forged_application_command_on_an_undeclared_path_is_refused() -> None:
    forged = ApplicationCommand(
        ("memory", "purge-everything"), "memory.get", "knowledge_retrieval"
    )
    with pytest.raises(DispatchError) as raised:
        dispatch_application(
            client(RefusingTransport()), forged, payload={}, deadline=deadline()
        )
    assert_payload_free(raised.value, "purge-everything")


def test_a_forged_probe_command_is_refused_before_the_transport() -> None:
    """A real probe path carrying a probe the surface pairs with another path."""
    forged = ProbeCommand(("service", "health"), "service.discover")
    with pytest.raises(DispatchError) as raised:
        dispatch_probe(client(RefusingTransport()), forged, deadline=deadline())
    assert_refusal(raised.value)


def test_a_forged_probe_command_on_an_undeclared_path_is_refused() -> None:
    forged = ProbeCommand(("service", "livez-nobody-should-see"), "service.health")
    with pytest.raises(DispatchError) as raised:
        dispatch_probe(client(RefusingTransport()), forged, deadline=deadline())
    assert_payload_free(raised.value, "livez-nobody-should-see")


def test_the_refusal_cannot_be_constructed_with_anything_in_it() -> None:
    """There is no way to raise one of these quoting a payload or an endpoint."""
    assert str(DispatchError()) == DIAGNOSTIC
    with pytest.raises(TypeError):
        DispatchError("unix:///tmp/omnivia/s.sock")  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# 2 & 3. What each of the twenty commands actually sends
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command", APPLICATION_COMMANDS, ids=lambda c: "/".join(c.path)
)
def test_each_command_dispatches_its_exact_catalogue_claims(
    command: ApplicationCommand,
) -> None:
    transport = RecordingTransport()
    dispatch_application(client(transport), command, payload={}, deadline=deadline())
    entry = get_operation_metadata(command.operation)
    request = sent(transport)

    assert request.operation == command.operation
    assert request.metadata.purpose == command.purpose
    assert request.metadata.scopes == tuple(entry.scope.required_scopes)
    assert request.metadata.scopes  # the catalogue names at least one, always
    # The catalogue's own object, passed through -- not an equal one rebuilt
    # here, which is what a value comparison alone would also accept.
    assert request.metadata.required_capabilities == (entry.required_capability,)
    assert request.metadata.required_capabilities[0] is entry.required_capability
    assert len(request.metadata.required_capabilities) == 1
    assert request.metadata.api_version == CONTRACT_VERSION
    assert request.metadata.client.id == "omnivia-cli"


@pytest.mark.parametrize(
    "command", APPLICATION_COMMANDS, ids=lambda c: "/".join(c.path)
)
def test_each_command_states_the_workspace_its_scope_kind_admits(
    command: ApplicationCommand,
) -> None:
    """The service's own rule, run over the request this module built."""
    transport = RecordingTransport()
    connected = client(transport)
    dispatch_application(connected, command, payload={}, deadline=deadline())
    entry = get_operation_metadata(command.operation)
    request = sent(transport)

    if command.operation in INSTALLATION_OPERATIONS:
        assert entry.scope.scope_kind == SCOPE_KIND_INSTALLATION
        assert request.metadata.workspace_id is None
    else:
        assert entry.scope.scope_kind == SCOPE_KIND_WORKSPACE
        assert request.metadata.workspace_id == connected.descriptor.workspace_id
        assert request.metadata.workspace_id == WORKSPACE

    # Would raise if the two disagreed, which is exactly what the service does.
    validate_operation_scope_workspace_id(
        entry.scope.scope_kind, request.metadata.workspace_id
    )


def test_the_workspace_is_the_connected_endpoints_and_not_one_chosen_here() -> None:
    transport = RecordingTransport()
    dispatch_application(
        client(transport, OTHER_WORKSPACE),
        find_application_command(("memory", "get")),
        payload={},
        deadline=deadline(),
    )
    assert sent(transport).metadata.workspace_id == OTHER_WORKSPACE


def test_the_two_installation_scoped_commands_are_exactly_list_and_create() -> None:
    """Stated as a set, so a catalogue that rescopes a third one fails here."""
    installation = {
        command.operation
        for command in APPLICATION_COMMANDS
        if get_operation_metadata(command.operation).scope.scope_kind
        == SCOPE_KIND_INSTALLATION
    }
    assert installation == set(INSTALLATION_OPERATIONS)


# --------------------------------------------------------------------------
# 4. The identifiers
# --------------------------------------------------------------------------

MEMORY_GET = APPLICATION_COMMANDS[4]


def test_request_correlation_and_trace_are_one_value() -> None:
    transport = RecordingTransport()
    dispatch_application(client(transport), MEMORY_GET, payload={}, deadline=deadline())
    metadata = sent(transport).metadata
    assert metadata.request_id == metadata.correlation_id == metadata.trace_id


def test_an_explicit_request_id_is_honoured() -> None:
    transport = RecordingTransport()
    dispatch_application(
        client(transport),
        MEMORY_GET,
        payload={},
        deadline=deadline(),
        request_id="caller-chosen-id-01",
    )
    metadata = sent(transport).metadata
    assert metadata.request_id == "caller-chosen-id-01"
    assert metadata.correlation_id == "caller-chosen-id-01"
    assert metadata.trace_id == "caller-chosen-id-01"


def test_a_generated_request_id_is_cli_prefixed_and_never_reused() -> None:
    """Random rather than sequential: two `omnivia` processes share no counter."""
    transport = RecordingTransport()
    connected = client(transport)
    for _ in range(8):
        dispatch_application(connected, MEMORY_GET, payload={}, deadline=deadline())
    minted = [request.metadata.request_id for request, _, _ in transport.calls]

    assert len(set(minted)) == len(minted)
    for identifier in minted:
        assert identifier.startswith("cli-")
        # Parses as a UUID, so the suffix is the random part it claims to be.
        assert uuid.UUID(identifier.removeprefix("cli-")).version == 4


def test_a_generated_probe_request_id_is_cli_prefixed_and_never_reused() -> None:
    transport = RecordingTransport()
    connected = client(transport)
    for command in PROBE_COMMANDS + PROBE_COMMANDS:
        dispatch_probe(connected, command, deadline=deadline())
    minted = [request.request_id for request, _, _ in transport.probes]

    assert len(set(minted)) == len(minted) == 6
    for identifier in minted:
        assert identifier is not None
        assert identifier.startswith("cli-")


# --------------------------------------------------------------------------
# 5. The claim, the payload, and the caller's own budget
# --------------------------------------------------------------------------


def test_a_principal_is_carried_as_a_claim_and_absence_stays_absent() -> None:
    """A claim is all it is: the service decides authority from its own grant."""
    transport = RecordingTransport()
    connected = client(transport)
    dispatch_application(connected, MEMORY_GET, payload={}, deadline=deadline())
    assert sent(transport).metadata.principal_claim is None

    transport = RecordingTransport()
    dispatch_application(
        client(transport),
        MEMORY_GET,
        payload={},
        deadline=deadline(),
        principal=PRINCIPAL,
    )
    assert sent(transport).metadata.principal_claim == PrincipalClaim(
        claimed_principal_id=PRINCIPAL
    )


def test_the_payload_is_copied_shallowly_and_never_aliased() -> None:
    """The envelope must not alias a mapping the caller can still mutate."""
    nested: dict[str, Any] = {"depth": 1}
    payload: dict[str, Any] = {"memory_id": "mem-01", "options": nested}
    transport = RecordingTransport()
    dispatch_application(
        client(transport), MEMORY_GET, payload=payload, deadline=deadline()
    )
    request = sent(transport)

    assert request.input == {"memory_id": "mem-01", "options": nested}
    assert request.input is not payload

    payload["memory_id"] = "mem-swapped-after-the-send"
    payload["added"] = True
    assert request.input["memory_id"] == "mem-01"
    assert "added" not in request.input

    # Shallow, and stated as such: the nested object is the caller's own, since
    # a deep copy would be a second traversal of a document the encoder walks.
    assert request.input["options"] is nested


def test_the_callers_own_deadline_and_token_reach_the_wire_unchanged() -> None:
    transport = RecordingTransport()
    budget = deadline(4.0)
    token = CancellationToken()
    dispatch_application(
        client(transport),
        MEMORY_GET,
        payload={},
        deadline=budget,
        cancellation=token,
    )
    _, carried_deadline, carried_token = transport.calls[0]
    assert carried_deadline is budget
    assert carried_token is token


def test_an_absent_cancellation_token_stays_absent() -> None:
    transport = RecordingTransport()
    dispatch_application(client(transport), MEMORY_GET, payload={}, deadline=deadline())
    assert transport.calls[0][2] is None


@pytest.mark.parametrize(
    ("seconds", "expected_ms"), [(2.5, 2500), (0.75, 750), (30.0, 30000)]
)
def test_what_is_left_of_the_budget_is_what_the_peer_is_told(
    seconds: float, expected_ms: int
) -> None:
    """So the peer works to the same clock the caller is waiting on."""
    transport = RecordingTransport()
    budget = deadline(seconds)
    dispatch_application(client(transport), MEMORY_GET, payload={}, deadline=budget)
    assert sent(transport).metadata.deadline_ms == expected_ms
    assert sent(transport).metadata.deadline_ms == budget.remaining_ms()


# --------------------------------------------------------------------------
# 6. Idempotency and the mutation precondition
# --------------------------------------------------------------------------

MEMORY_CREATE = APPLICATION_COMMANDS[3]


def test_an_idempotency_key_and_a_record_version_become_their_metadata() -> None:
    transport = RecordingTransport()
    dispatch_application(
        client(transport),
        MEMORY_CREATE,
        payload={},
        deadline=deadline(),
        idempotency_key="idem-0001",
        record_version="rv-0007",
    )
    metadata = sent(transport).metadata
    assert metadata.idempotency_key == "idem-0001"
    assert metadata.mutation_precondition == MutationPrecondition(
        record_version="rv-0007"
    )


def test_neither_one_is_invented_when_the_caller_states_neither() -> None:
    transport = RecordingTransport()
    dispatch_application(
        client(transport), MEMORY_CREATE, payload={}, deadline=deadline()
    )
    metadata = sent(transport).metadata
    assert metadata.idempotency_key is None
    assert metadata.mutation_precondition is None


def test_each_one_travels_without_the_other() -> None:
    transport = RecordingTransport()
    dispatch_application(
        client(transport),
        MEMORY_CREATE,
        payload={},
        deadline=deadline(),
        idempotency_key="idem-0002",
    )
    metadata = sent(transport).metadata
    assert metadata.idempotency_key == "idem-0002"
    assert metadata.mutation_precondition is None

    transport = RecordingTransport()
    dispatch_application(
        client(transport),
        MEMORY_CREATE,
        payload={},
        deadline=deadline(),
        record_version="rv-0008",
    )
    metadata = sent(transport).metadata
    assert metadata.idempotency_key is None
    assert metadata.mutation_precondition == MutationPrecondition(
        record_version="rv-0008"
    )


# --------------------------------------------------------------------------
# 7. The answer has to be an answer to this call
# --------------------------------------------------------------------------


def test_a_correlated_success_is_returned_as_the_object_the_peer_gave() -> None:
    answered: list[SuccessResponseEnvelope] = []

    def answer(request: RequestEnvelope) -> SuccessResponseEnvelope:
        envelope = success(request)
        answered.append(envelope)
        return envelope

    transport = RecordingTransport(answer=answer)
    response = dispatch_application(
        client(transport), MEMORY_GET, payload={}, deadline=deadline()
    )
    assert response is answered[0]


def test_an_application_error_envelope_is_returned_unchanged_and_not_raised() -> None:
    """A peer that answered with an application error has answered."""
    answered: list[ErrorResponseEnvelope] = []

    def answer(request: RequestEnvelope) -> ErrorResponseEnvelope:
        envelope = ErrorResponseEnvelope(
            metadata=response_metadata(request),
            error=ApiError(
                code="workspace_not_granted",
                message="not granted",
                retry_class="non_retryable",
            ),
        )
        answered.append(envelope)
        return envelope

    transport = RecordingTransport(answer=answer)
    response = dispatch_application(
        client(transport), MEMORY_GET, payload={}, deadline=deadline()
    )
    assert response is answered[0]
    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "workspace_not_granted"


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_id": "some-other-calls-id"},
        {"correlation_id": "some-other-calls-id"},
        {"request_id": "some-other-calls-id", "correlation_id": "some-other-calls-id"},
        {"request_id": ""},
        {"correlation_id": ""},
    ],
    ids=["request", "correlation", "both", "empty-request", "empty-correlation"],
)
def test_an_uncorrelated_answer_is_refused_rather_than_returned(
    overrides: dict[str, str],
) -> None:
    """A caller reading it would be reading some other call's result."""
    transport = RecordingTransport(answer=lambda request: success(request, **overrides))
    with pytest.raises(DispatchError) as raised:
        dispatch_application(
            client(transport),
            MEMORY_GET,
            payload={},
            deadline=deadline(),
            request_id="caller-chosen-id-02",
        )
    assert_payload_free(raised.value, "some-other-calls-id", "caller-chosen-id-02")


def test_an_uncorrelated_error_answer_is_refused_too() -> None:
    """The correlation check is on the answer, not on which branch it took."""

    def answer(request: RequestEnvelope) -> ErrorResponseEnvelope:
        return ErrorResponseEnvelope(
            metadata=response_metadata(request, request_id="some-other-calls-id"),
            error=ApiError(code="conflict", message="x", retry_class="retryable"),
        )

    transport = RecordingTransport(answer=answer)
    with pytest.raises(DispatchError) as raised:
        dispatch_application(
            client(transport), MEMORY_GET, payload={}, deadline=deadline()
        )
    assert_payload_free(raised.value, "some-other-calls-id", "conflict")


# --------------------------------------------------------------------------
# 8. The probes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", PROBE_COMMANDS, ids=lambda c: "/".join(c.path))
def test_each_probe_goes_to_the_connected_transport_as_a_canonical_request(
    command: ProbeCommand,
) -> None:
    transport = RecordingTransport()
    budget = deadline(2.5)
    token = CancellationToken()
    result = dispatch_probe(
        client(transport), command, deadline=budget, cancellation=token
    )

    assert len(transport.probes) == 1
    request, carried_deadline, carried_token = transport.probes[0]
    assert request == ServiceProbeRequest(
        probe=command.probe,
        request_id=request.request_id,
        deadline_ms=2500,
    )
    assert request.probe == command.probe
    # The caller's own objects, not re-derived equal ones.
    assert carried_deadline is budget
    assert carried_token is token
    # A probe is not an application call, and does not become one.
    assert transport.calls == []
    assert result.details == {"request_id": request.request_id}


@pytest.mark.parametrize("command", PROBE_COMMANDS, ids=lambda c: "/".join(c.path))
def test_a_probe_result_is_returned_exactly_as_it_arrived(
    command: ProbeCommand,
) -> None:
    """Including a degraded or unhealthy status: that is an answer."""
    answered: list[ServiceProbeResult] = []

    def probe_answer(request: ServiceProbeRequest) -> ServiceProbeResult:
        result = echo(request, status="warn")
        answered.append(result)
        return result

    transport = RecordingTransport(probe_answer=probe_answer)
    result = dispatch_probe(client(transport), command, deadline=deadline())
    assert result is answered[0]
    assert result.status == "warn"


def test_an_explicit_probe_request_id_is_honoured_and_echoed_back() -> None:
    transport = RecordingTransport()
    dispatch_probe(
        client(transport),
        find_probe_command(("service", "readiness")),
        deadline=deadline(),
        request_id="caller-chosen-probe-03",
    )
    assert transport.probes[0][0].request_id == "caller-chosen-probe-03"


@pytest.mark.parametrize(
    "overrides",
    [
        {"details": None},
        {"details": {}},
        {"details": {"request_id": "some-other-probes-id"}},
        {"details": {"request_id": None}},
        {"details": {"requestId": "caller-chosen-probe-04"}},
        {"details": {"request_id": "caller-chosen-probe-04 "}},
    ],
    ids=["absent", "empty", "wrong", "null", "wrong-key", "trailing-space"],
)
def test_a_probe_answer_without_this_calls_echo_is_refused(
    overrides: dict[str, Any],
) -> None:
    """An answer this call cannot vouch for is not one it returns."""
    transport = RecordingTransport(
        probe_answer=lambda request: echo(request, **overrides)
    )
    with pytest.raises(DispatchError) as raised:
        dispatch_probe(
            client(transport),
            find_probe_command(("service", "health")),
            deadline=deadline(),
            request_id="caller-chosen-probe-04",
        )
    assert_payload_free(raised.value, "some-other-probes-id", "caller-chosen-probe-04")
    assert transport.calls == []


def test_no_probe_reaches_the_application_call_path() -> None:
    transport = RecordingTransport()
    connected = client(transport)
    for command in PROBE_COMMANDS:
        dispatch_probe(connected, command, deadline=deadline())
    assert len(transport.probes) == 3
    assert transport.calls == []


# --------------------------------------------------------------------------
# 9. What this module is allowed to reach
# --------------------------------------------------------------------------


def imported_modules() -> list[str]:
    """Every module name `dispatch.py` imports, parsed rather than grepped."""
    tree = ast.parse(Path(dispatch.__file__).read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import belongs here"
            assert node.module is not None
            names.append(node.module)
    return names


def test_dispatch_imports_neither_the_runtime_nor_the_mcp_adapter() -> None:
    """It must be usable by a client that has installed neither."""
    modules = imported_modules()
    assert modules
    assert not [name for name in modules if name.startswith("omnivia_core_runtime")]
    assert not [name for name in modules if name.startswith("omnivia_core_mcp")]


@pytest.mark.parametrize("forbidden", FORBIDDEN_IMPORTS)
def test_dispatch_reaches_no_discovery_transport_filesystem_or_storage_api(
    forbidden: str,
) -> None:
    """No endpoint is discovered, no transport chosen, nothing on disk touched."""
    parts = {part for name in imported_modules() for part in name.split(".")}
    assert forbidden not in parts


def test_dispatch_imports_only_the_contracts_and_the_client_package() -> None:
    """The allowlist, so a new import has to be a deliberate decision."""
    roots = {name.split(".")[0] for name in imported_modules()}
    assert roots <= ALLOWED_IMPORT_ROOTS, sorted(roots - ALLOWED_IMPORT_ROOTS)


def test_nothing_concrete_is_taken_from_the_client_package() -> None:
    """Only the injected client and the two budget types -- no transport class."""
    tree = ast.parse(Path(dispatch.__file__).read_text(encoding="utf-8"))
    taken = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "omnivia_core_client"
        for alias in node.names
    }
    assert taken == {"CancellationToken", "Deadline", "ServiceClient"}
