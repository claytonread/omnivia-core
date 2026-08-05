"""V06-2 Lane A: the first authorised vertical, end to end and in process.

`omnivia-core-cli` calling `workspace.inspect` under a real `AuthenticatedSession` is
the vertical this lane establishes the server half of. What is proved here is that
the request reaches `authorize_application_request` -- a function that had zero
callers in any `src` tree before this lane -- passes all twelve of its checks, and is
answered by a handler held in an `ApplicationOperationRegistry`, which had zero
callers too.

The single most valuable test in the file is
`test_a_request_the_probe_path_accepts_is_now_refused_by_the_seam`. Every other
positive assertion here could, in principle, be satisfied by a build that had taken
the forbidden shortcut and registered the operation against the probe `Dispatcher`.
That one cannot: it takes a request the probe path accepts and re-encodes today, and
requires it to be refused. It is the difference between an import appearing in a diff
and enforcement actually happening.

The real transport, a live service process and `omnivia workspace show` as a
subprocess are Lane B's evidence and are deliberately not stubbed here.
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.service import application as application_module
from omnivia_core_runtime.service import main as main_module
from omnivia_core_runtime.service.application import (
    CHANNEL_TRUST,
    LOCAL_TRANSPORT_ADAPTER,
    PRINCIPAL_SOURCE,
    WORKSPACE_INSPECT_OPERATION,
    WORKSPACE_INSPECTION_PURPOSE,
    ApplicationCallRecord,
    ApplicationDispatcher,
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers import workspace as handler_module
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL, _router_for
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    server_capability_snapshot,
)
from omnivia_core_runtime.service.probes import ServiceFacts
from omnivia_core_runtime.service.runner import ServiceSettings
from omnivia_core_runtime.service.versions import API_VERSION
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import create_workspace

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    CapabilityRef,
    CapabilityRequirement,
    ClientIdentity,
    ErrorResponseEnvelope,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    WorkspaceInspectResult,
    encode_request,
    encode_response,
    get_operation_metadata,
)
from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

WORKSPACE_ID = "ws-vertical-0001"
INSTALLATION_ID = "inst-vertical-0001"
CORE_VERSION = "0.1.0"
WORKSPACE_NAME = "Vertical fixture"
CREATED_AT = "2026-08-05T00:00:00+00:00"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")
ENTRY = get_operation_metadata(WORKSPACE_INSPECT_OPERATION)


class ServedWorkspace:
    """The two facts a handler reads off the service that owns the workspace.

    The same shape `ServiceRunner` presents -- a real `WorkspaceLayout` over a real
    manifest on disk, and the real `ServiceSettings` the process was launched with --
    without a lock, a lease or a database connection, none of which this read-only
    operation touches and none of which a handler may take.
    """

    def __init__(self, root: Path) -> None:
        self.layout = WorkspaceLayout(root=root)
        self.settings = ServiceSettings(
            workspace_root=root,
            installation_root=root.parent / "installation-state",
            core_version=CORE_VERSION,
            endpoint=None,
        )

    def probe_facts(self) -> ServiceFacts:  # pragma: no cover - probes are not exercised
        raise NotImplementedError


@pytest.fixture
def served(tmp_path: Path) -> ServedWorkspace:
    """A real workspace on disk, created through the production manifest writer."""
    create_workspace(
        tmp_path / "workspace",
        WorkspaceManifest(
            workspace_id=WORKSPACE_ID,
            created_at=CREATED_AT,
            name=WORKSPACE_NAME,
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version=CORE_VERSION
            ),
        ),
    )
    return ServedWorkspace(tmp_path / "workspace")


def production_path(
    served: ServedWorkspace,
    *,
    record: Any = None,
) -> ApplicationDispatcher:
    """The application path, composed exactly as `service.main.serve` composes it."""
    registry = build_application_registry()
    return ApplicationDispatcher(
        registry=registry,
        session=local_owner_session(
            principal_id=LOCAL_PRINCIPAL,
            installation_id=INSTALLATION_ID,
            workspace_id=WORKSPACE_ID,
        ),
        binding=ServiceBinding(
            installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
        ),
        supported_capabilities=server_capability_snapshot(registry),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset({WORKSPACE_ID}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            served,
        ),
        record=record,
        service=served,
    )


def request_for(**overrides: Any) -> RequestEnvelope:
    """A `workspace.inspect` request built from the frozen catalogue entry."""
    operation = overrides.pop("operation", WORKSPACE_INSPECT_OPERATION)
    fields_: dict[str, Any] = {
        "request_id": "req-vertical-1",
        "correlation_id": "corr-vertical-1",
        "trace_id": "trace-vertical-1",
        "api_version": CONTRACT_VERSION,
        "client": CLIENT,
        "workspace_id": WORKSPACE_ID,
        "scopes": tuple(ENTRY.scope.required_scopes),
        "purpose": WORKSPACE_INSPECTION_PURPOSE,
        "required_capabilities": (
            CapabilityRequirement(
                id=ENTRY.required_capability.id,
                minimum_version=ENTRY.required_capability.minimum_version,
                required=True,
            ),
        ),
        "idempotency_key": None,
        "mutation_precondition": None,
        "principal_claim": None,
    }
    fields_.update(overrides)
    return RequestEnvelope(
        operation=operation, metadata=RequestMetadata(**fields_), input={}
    )


def answered(response: ResponseEnvelope) -> SuccessResponseEnvelope:
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


# --- the authorised path, end to end ------------------------------------------


def test_the_full_authorised_path_answers_with_a_workspace_descriptor(
    served: ServedWorkspace,
) -> None:
    """A real session, a real binding, a real request, out as the contract's result.

    Falsifier: narrow any one of the session's grants -- the workspace, the operation,
    the scope, the purpose or the capability -- and this refuses. Each of those is a
    separate refusal test in the companion module.
    """
    response = answered(production_path(served).dispatch(request_for()))

    # The wire shape is the wrapper, not a bare descriptor: decoding proves it.
    result = WorkspaceInspectResult.from_wire(response.result)
    descriptor = result.workspace

    assert descriptor.workspace_id == WORKSPACE_ID
    assert descriptor.display_name == WORKSPACE_NAME
    # The literal as well as the constant: following the constant alone would agree
    # with whatever it was changed to, which is not an assertion about the wire.
    assert descriptor.status == "active" == handler_module.WORKSPACE_STATUS_ACTIVE
    assert descriptor.created_at == CREATED_AT
    assert descriptor.compatibility.workspace_format_version == "1.0"
    assert descriptor.compatibility.status == "compatible"
    # The whole response re-encodes, which is the check an in-process assertion alone
    # would miss: a private ordinal where a ContractVersion belongs encodes to nothing.
    assert "workspace" in encode_response(response)["result"]


def test_the_authorized_context_carries_the_expected_effective_authority(
    served: ServedWorkspace,
) -> None:
    """What the seam decided, asserted through the record the path emits.

    The context itself is internal to one dispatch, so the record is where its facts
    become observable -- which is also the point of the record existing.
    """
    records: list[ApplicationCallRecord] = []
    production_path(served, record=records.append).dispatch(request_for())

    assert len(records) == 1
    record = records[0]
    assert record.principal_id == LOCAL_PRINCIPAL
    assert record.workspace_id == WORKSPACE_ID
    assert record.scopes == ("workspace:read",)
    assert record.purpose == WORKSPACE_INSPECTION_PURPOSE
    assert record.capabilities == (CapabilityRef(id="workspace.read", version="1.0"),)


def test_the_session_is_exactly_the_accepted_policy() -> None:
    """The session record, field by field, so a widening is visible by inspection."""
    session = local_owner_session(
        principal_id=LOCAL_PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
    )

    assert session.principal_id == LOCAL_PRINCIPAL
    assert session.roles == frozenset()
    assert session.installations == frozenset({INSTALLATION_ID})
    assert session.workspaces == frozenset({WORKSPACE_ID})
    assert session.operations == frozenset({WORKSPACE_INSPECT_OPERATION})
    assert session.scopes == frozenset({"workspace:read"})
    # The literal as well as the constant. Every other field on this session is pinned
    # by something outside the lane -- the operation name by the frozen catalogue, the
    # scope and the capability by that entry's own metadata -- but there is no purpose
    # registry anywhere in the contract, so the owner's policy table is the only thing
    # that fixes this string and this assertion is the only thing that holds it. Read
    # through the constant alone it would agree with whatever the constant was renamed
    # to, which is the mutation that survived when this was written that way.
    assert WORKSPACE_INSPECTION_PURPOSE == "workspace_inspection"
    assert session.purposes == frozenset({"workspace_inspection"})
    assert session.capabilities == (
        CapabilityRef(id="workspace.read", version="1.0"),
    )


# --- twelve of twelve, demonstrated rather than asserted -----------------------


def test_a_request_the_probe_path_accepts_is_now_refused_by_the_seam(
    served: ServedWorkspace,
) -> None:
    """The one test the forbidden shortcut cannot satisfy.

    An out-of-domain `request_id` is a value the contract does not admit. The probe
    path never looks: it accepts the request and re-encodes the identifier straight
    back out. Only `authorize_application_request` holds a request to the contract's
    own value domains, so a refusal here proves the request reached that function and
    not `Dispatcher.dispatch`.

    Both halves are asserted together on purpose. Asserting only the refusal would
    still pass against a build that refused everything.
    """
    forged = "Bearer hunter2"
    path = production_path(served)

    refused = path.dispatch(request_for(request_id=forged))
    assert isinstance(refused, ErrorResponseEnvelope)
    assert refused.error.code == ERROR_CODE_INVALID_REQUEST

    # The same value, on the probe path, is accepted and echoed -- the tree as it
    # stands, and the reason this refusal is evidence rather than decoration.
    probe = path.probe.dispatch(
        request_for(operation="core.health", request_id=forged)
    )
    assert isinstance(probe, SuccessResponseEnvelope)
    assert probe.metadata.request_id == forged


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_version", "not-a-version"),
        ("trace_id", "trace with spaces"),
        ("correlation_id", "x" * 129),
        ("deadline_ms", -1),
    ],
)
def test_every_value_domain_the_probe_path_ignores_is_now_enforced(
    served: ServedWorkspace, field: str, value: object
) -> None:
    """Check 3 of the twelve, across four fields the probe path never validates."""
    response = production_path(served).dispatch(request_for(**{field: value}))

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == ERROR_CODE_INVALID_REQUEST


def test_the_probe_operations_still_answer_through_the_probe_seam(
    served: ServedWorkspace,
) -> None:
    """The probe path is added to, never widened, merged or retired."""
    path = production_path(served)

    for operation in SERVICE_OPERATIONS:
        response = path.dispatch(request_for(operation=operation))
        assert isinstance(response, SuccessResponseEnvelope | ErrorResponseEnvelope)
        # Answered by the probe registry's own handler, under the probe grant, with
        # the probe grant's principal on the response.
        assert response.metadata.authority.principal_id == LOCAL_PRINCIPAL

    assert path.grant.operations == frozenset(SERVICE_OPERATIONS)
    assert path.grant.principal == LOCAL_PRINCIPAL


# --- 9. the audit and correlation references ----------------------------------


def test_9_the_answer_carries_the_expected_audit_and_correlation_references(
    served: ServedWorkspace,
) -> None:
    """Correlation round-trips, and the catalogue's audit metadata is what is recorded.

    Falsifier: change the recorded category to anything but the catalogue's own and
    this fails; break the correlation round trip and `DocumentRouter` refuses the
    response outright, which the wire test below exercises.
    """
    records: list[ApplicationCallRecord] = []
    request = request_for()
    response = answered(production_path(served, record=records.append).dispatch(request))

    assert response.metadata.request_id == request.metadata.request_id
    assert response.metadata.correlation_id == request.metadata.correlation_id

    record = records[0]
    assert record.audited is ENTRY.audit.audited is True
    assert record.audit_category == ENTRY.audit.audit_category == "read"
    assert record.request_id == request.metadata.request_id
    assert record.correlation_id == request.metadata.correlation_id
    assert record.trace_id == request.metadata.trace_id


# --- the recording set --------------------------------------------------------


REQUIRED_RECORD_FIELDS = (
    "client_id",
    "client_version",
    "transport",
    "operation",
    "principal_source",
    "workspace_id",
    "capabilities",
    "purpose",
    "request_id",
    "correlation_id",
    "audited",
    "audit_category",
    "allowed",
    "error_code",
)


def test_every_required_caller_recording_field_is_present_when_allowed(
    served: ServedWorkspace,
) -> None:
    """Section 9's field list, for an allowed request."""
    records: list[ApplicationCallRecord] = []
    production_path(served, record=records.append).dispatch(request_for())
    record = records[0]

    assert {field.name for field in fields(ApplicationCallRecord)} >= set(
        REQUIRED_RECORD_FIELDS
    )
    assert record.allowed is True
    assert record.error_code is None
    assert record.retry_class is None
    assert record.transport == LOCAL_TRANSPORT_ADAPTER
    assert record.operation == WORKSPACE_INSPECT_OPERATION
    assert record.principal_source == PRINCIPAL_SOURCE
    assert record.client_id == CLIENT.id
    assert record.client_version == CLIENT.version


def test_every_required_caller_recording_field_is_present_when_denied(
    served: ServedWorkspace,
) -> None:
    """Section 9's field list again, for a denied request.

    A denial has no authorized context, so the caller-supplied fields fall back to
    what the request claimed. The server facts are unchanged, which is the property
    that makes a denial record usable: what the caller said cannot rewrite who the
    server was acting as.
    """
    records: list[ApplicationCallRecord] = []
    path = production_path(served, record=records.append)
    path.dispatch(request_for(purpose="not_allowed_here"))
    record = records[0]

    assert record.allowed is False
    assert record.error_code == "invalid_purpose"
    assert record.retry_class == "non_retryable"
    assert record.transport == LOCAL_TRANSPORT_ADAPTER
    assert record.operation == WORKSPACE_INSPECT_OPERATION
    assert record.principal_id == LOCAL_PRINCIPAL
    assert record.principal_source == PRINCIPAL_SOURCE
    assert record.audited is True
    assert record.audit_category == "read"
    assert record.client_id == CLIENT.id
    assert record.client_version == CLIENT.version
    assert record.request_id == "req-vertical-1"
    assert record.correlation_id == "corr-vertical-1"
    # The claim, recorded as a claim -- server-side only, and never returned.
    assert record.purpose == "not_allowed_here"
    assert record.capabilities == ()


def test_a_denial_record_survives_a_request_that_is_not_the_shape_it_claims(
    served: ServedWorkspace,
) -> None:
    """The recording path is total, so a malformed request cannot crash it.

    An in-process caller builds the frozen dataclasses itself and is held to nothing,
    so an integer request id constructs. The seam refuses it; the record then has to
    read the same fields without assuming any of them is the type it is annotated as,
    and a field it cannot read is recorded as claiming nothing rather than raising
    inside the recording of a refusal.
    """
    records: list[ApplicationCallRecord] = []
    response = production_path(served, record=records.append).dispatch(
        request_for(request_id=123)
    )

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == ERROR_CODE_INVALID_REQUEST
    assert records[0].allowed is False
    assert records[0].request_id is None
    assert records[0].correlation_id == "corr-vertical-1"
    assert records[0].principal_id == LOCAL_PRINCIPAL


def test_no_credential_shaped_value_reaches_a_record_or_a_refusal(
    served: ServedWorkspace,
) -> None:
    """A secret pasted into a request field comes back out of nothing."""
    secret = "sk-do-not-echo-91af"
    records: list[ApplicationCallRecord] = []
    response = production_path(served, record=records.append).dispatch(
        request_for(purpose="denied", client=ClientIdentity(id=secret, version="0.1.0"))
    )

    assert secret not in json.dumps(encode_response(response), sort_keys=True)
    assert isinstance(response, ErrorResponseEnvelope)
    # Recorded server-side, which is required, and returned to the caller, which is not.
    assert records[0].client_id == secret


# --- the peer-identity prohibition --------------------------------------------


#: Peer-credential primitives. None exists anywhere in this repository, adding one is
#: a stop condition under this packet, and the deferral that covers it is recorded.
PEER_CREDENTIAL_PRIMITIVES = ("SO_PEERCRED", "LOCAL_PEERCRED", "getpeereid", "getsockopt")

#: Words that would turn a string a caller or an auditor reads into a claim that the
#: peer was authenticated. Checked against *emitted* strings only -- refusal messages,
#: recorded field values, constants -- and never against prose, because the prose in
#: these modules necessarily quotes the claim in order to prohibit it.
FORBIDDEN_IN_EMITTED_STRINGS = ("peer", "authenticat", "verified", "os user")


@pytest.mark.parametrize(
    "module",
    [application_module, handler_module, main_module],
    ids=lambda module: module.__name__,
)
def test_no_module_on_this_path_reaches_for_a_peer_credential_primitive(
    module: Any,
) -> None:
    """The stop condition, as a test. Adding one under this packet is not permitted."""
    source = inspect.getsource(module)

    for primitive in PEER_CREDENTIAL_PRIMITIVES:
        assert primitive not in source, f"{module.__name__} reaches for {primitive}"


@pytest.mark.parametrize(
    "module",
    [application_module, handler_module, main_module],
    ids=lambda module: module.__name__,
)
def test_no_string_this_path_emits_claims_a_verified_operating_system_peer(
    module: Any,
) -> None:
    """The owner's prohibition, checked where a false claim would actually travel.

    Not a style rule, and deliberately not a prose scan: these modules say at length
    what is *not* claimed, so a text search would fire on the prohibition itself. What
    is checked is every string constant that is not a docstring -- the frozen refusal
    messages, the recorded `principal_source` and `channel_trust`, the transport label
    -- because those are the values that reach a wire error, a record and an auditor.

    A security record that asserts the peer was authenticated is worse than the gap it
    describes. The honest statement is that the caller reached a protected endpoint,
    and that is what these constants say.
    """
    tree = ast.parse(inspect.getsource(module))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }

    emitted = [
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    assert emitted, "nothing was scanned, so this proves nothing"

    for value in emitted:
        for claim in FORBIDDEN_IN_EMITTED_STRINGS:
            assert claim not in value, f"{module.__name__} emits {value!r}"


def test_the_record_states_channel_trust_and_configuration_not_authentication(
    served: ServedWorkspace,
) -> None:
    """The two load-bearing strings, pinned to the owner's own words."""
    records: list[ApplicationCallRecord] = []
    production_path(served, record=records.append).dispatch(request_for())
    record = records[0]

    assert record.principal_source == "installation-local service configuration"
    assert record.channel_trust == (
        "protected local IPC endpoint and operating-system filesystem permissions"
    )
    assert PRINCIPAL_SOURCE == record.principal_source
    assert CHANNEL_TRUST == record.channel_trust
    for value in (record.principal_source, record.channel_trust):
        assert "authenticat" not in value
        assert "peer" not in value


# --- the server capability snapshot -------------------------------------------


def test_the_response_advertises_the_effective_capability_not_an_operation_name(
    served: ServedWorkspace,
) -> None:
    """The response's authority is the seam's effective refs.

    Before this lane the response builder fabricated one `CapabilityRef` per granted
    *operation name*, at the request's own api_version. An operation name is not a
    capability id, and this vertical is the first response where the difference is
    observable.
    """
    response = answered(production_path(served).dispatch(request_for()))
    advertised = response.metadata.authority.capabilities

    assert advertised == (CapabilityRef(id="workspace.read", version="1.0"),)
    assert response.metadata.version.capabilities.effective == advertised
    assert all(ref.id != WORKSPACE_INSPECT_OPERATION for ref in advertised)
    assert all(ref.version != API_VERSION or ref.id == "workspace.read" for ref in advertised)


def test_the_snapshot_is_a_server_fact_and_the_probe_decoration_still_is_not(
    served: ServedWorkspace,
) -> None:
    """The probe path keeps its old decoration; only the application path changed."""
    probe = production_path(served).probe.dispatch(request_for(operation="core.health"))

    assert isinstance(probe, SuccessResponseEnvelope)
    assert {ref.id for ref in probe.metadata.authority.capabilities} == set(
        SERVICE_OPERATIONS
    )


# --- the whole document path --------------------------------------------------


def test_the_document_router_routes_a_real_wire_document_to_this_path(
    served: ServedWorkspace,
) -> None:
    """The router's own composition, exercised on an encoded request document.

    `DocumentRouter` picks the application branch structurally, decodes with the
    public decoder, and refuses any response whose identifiers do not answer the
    request -- so this covers the correlation guarantee from the outside as well.
    """
    router = _router_for(served, production_path(served))

    routed = router.route(encode_request(request_for()))

    assert isinstance(routed, SuccessResponseEnvelope)
    assert routed.metadata.request_id == "req-vertical-1"
    assert WorkspaceInspectResult.from_wire(routed.result).workspace.workspace_id == (
        WORKSPACE_ID
    )


# --- what calls what ----------------------------------------------------------


def test_the_application_path_is_the_caller_of_both_previously_unreached_seams() -> None:
    """The seams this lane makes reachable, named at the source that reaches them."""
    source = inspect.getsource(application_module)

    assert "authorize_application_request(" in source
    assert "ApplicationOperationRegistry()" in source
    assert "server_capability_snapshot" in inspect.getsource(main_module)
    assert "local_owner_session" in inspect.getsource(main_module)


def test_the_two_halves_of_the_wiring_cannot_act_as_different_principals(
    served: ServedWorkspace,
) -> None:
    """A wiring that stated two principals is refused at construction.

    This is what keeps the HTTP adapter's principal cross-check load-bearing now that
    the object it reads is the application path rather than the probe dispatcher: a
    dispatcher that admits sessions for one principal and runs them as another is the
    hole that check closes.
    """
    registry = build_application_registry()
    with pytest.raises(ValueError, match="different"):
        ApplicationDispatcher(
            registry=registry,
            session=local_owner_session(
                principal_id="someone-else",
                installation_id=INSTALLATION_ID,
                workspace_id=WORKSPACE_ID,
            ),
            binding=ServiceBinding(
                installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
            ),
            supported_capabilities=server_capability_snapshot(registry),
            transport=LOCAL_TRANSPORT_ADAPTER,
            probe=Dispatcher.for_service_operations(
                Grant(
                    principal=LOCAL_PRINCIPAL,
                    workspaces=frozenset({WORKSPACE_ID}),
                    operations=frozenset(SERVICE_OPERATIONS),
                ),
                served,
            ),
            record=None,
        )
