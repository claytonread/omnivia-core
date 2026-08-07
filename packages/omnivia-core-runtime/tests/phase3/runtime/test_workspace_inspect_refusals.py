"""V06-2 Lane A: the nine negative tests the owner made the acceptance bar.

Section 5.8 of the owner resolution names ten refusals the first authorised vertical
must prove. Nine of them are provable here, before any CLI exists; the tenth --
determinism through the real local adapter -- belongs to Lane B, and is deliberately
absent rather than faked with a stub.

Every test below goes through `ApplicationDispatcher`, the production path, rather
than calling `authorize_application_request` directly. That is the point: the seam's
own acceptance suite already proves the seam refuses these things, and what was
missing was any evidence that a request *reaches* it. A test that called the seam
directly would pass just as well against a build that routed the operation through
the probe `Dispatcher`, which is exactly the failure the binding clause exists to
prevent.

The free falsifier for tests 1 to 7, and the reason the shapes below are what they
are: run any of them against the probe `Dispatcher` -- the tree as it stood before
this lane -- and it fails, because that path answers `workspace.inspect` with
`core.operation_not_implemented` and never asks any of these questions.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.service import authorization
from omnivia_core_runtime.service.application import (
    EVIDENCE_SEARCH_OPERATION,
    KNOWLEDGE_RETRIEVAL_PURPOSE,
    LOCAL_TRANSPORT_ADAPTER,
    WORKSPACE_INSPECT_OPERATION,
    WORKSPACE_INSPECTION_PURPOSE,
    ApplicationDispatcher,
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import (
    ApplicationAuthorizationError,
    AuthenticatedSession,
    Grant,
    ServiceBinding,
    authorize_application_request,
)
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.operations import (
    APPLICATION_OPERATIONS,
    SERVICE_OPERATIONS,
    ApplicationOperationRegistry,
    OperationContext,
    build_service_registry,
    server_capability_snapshot,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_INVALID_PURPOSE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    OPERATION_CATALOGUE,
    CapabilityRef,
    CapabilityRequirement,
    ClientIdentity,
    ErrorResponseEnvelope,
    MutationPrecondition,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    WorkspaceInspectInput,
    encode_response,
    get_operation_metadata,
)

WORKSPACE_ID = "ws-inspect-0001"
OTHER_WORKSPACE_ID = "ws-inspect-0002"
INSTALLATION_ID = "inst-inspect-0001"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")
ENTRY = get_operation_metadata(WORKSPACE_INSPECT_OPERATION)
EVIDENCE_ENTRY = get_operation_metadata(EVIDENCE_SEARCH_OPERATION)

#: A mutating, workspace-scoped catalogue operation, chosen from the catalogue rather
#: than named here so the test still means something if the catalogue's mutation set
#: changes shape.
MUTATING_ENTRY = next(
    entry
    for entry in OPERATION_CATALOGUE
    if entry.scope.side_effect != "none"
    and entry.scope.scope_kind == ENTRY.scope.scope_kind
)

#: The Lane A production grant (§20.2): `workspace.inspect` and `evidence.search`, and
#: nothing else. Stated here as the literal `service.main.serve` states, not derived
#: from the registry -- deriving it would make the two agree by construction and this
#: file's whole job is to notice when the grant and the build disagree.
PRODUCTION_OPERATIONS = frozenset(
    {WORKSPACE_INSPECT_OPERATION, EVIDENCE_SEARCH_OPERATION}
)

_DEFAULT: Any = object()


def probe_dispatcher(service: object = None) -> Dispatcher:
    """The probe dispatcher exactly as `service.main` builds it."""
    return Dispatcher.for_service_operations(
        Grant(
            principal=LOCAL_PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        service,
    )


def production_session(**overrides: Any) -> AuthenticatedSession:
    """The session the production construction site builds, optionally widened.

    Widening is how a refusal is falsified: a test that denies something has to be
    able to show the same request succeeding once the grant it depends on is opened.

    `operations` is passed here because `service.main.serve` passes it: under the
    accepted parameterised shape the operation set is a literal in the constructor's
    caller, so a test that wants *the production session* has to state the production
    literal rather than take a default. `test_the_production_grant_is_the_lane_a_grant`
    below pins this tuple against `main.py`'s own, so the two cannot drift apart
    silently -- which is the failure mode a hand-copied literal invites.
    """
    session = local_owner_session(
        principal_id=LOCAL_PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        operations=PRODUCTION_OPERATIONS,
    )
    if not overrides:
        return session
    fields = {
        "principal_id": session.principal_id,
        "roles": session.roles,
        "installations": session.installations,
        "workspaces": session.workspaces,
        "operations": session.operations,
        "scopes": session.scopes,
        "purposes": session.purposes,
        "capabilities": session.capabilities,
    }
    fields.update(overrides)
    return AuthenticatedSession(**fields)


def dispatcher(
    *,
    session: Any = _DEFAULT,
    binding: Any = _DEFAULT,
    supported: Any = _DEFAULT,
    registry: Any = _DEFAULT,
    service: object = None,
    record: Any = None,
) -> ApplicationDispatcher:
    """The production path, wired as `service.main` wires it unless told otherwise."""
    held = build_application_registry() if registry is _DEFAULT else registry
    return ApplicationDispatcher(
        registry=held,
        session=production_session() if session is _DEFAULT else session,
        binding=(
            ServiceBinding(
                installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
            )
            if binding is _DEFAULT
            else binding
        ),
        supported_capabilities=(
            server_capability_snapshot(held) if supported is _DEFAULT else supported
        ),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=probe_dispatcher(service),
        record=record,
        service=service,
    )


def request_for(entry: Any = ENTRY, **overrides: Any) -> RequestEnvelope:
    """A request for `entry` that the catalogue validator accepts.

    Every field the catalogue decides -- the required scopes, the required capability,
    and the idempotency and precondition postures -- is read off the frozen entry, so
    a test never encodes a second opinion about what a well-formed request looks like
    and a refusal below is never a malformed request wearing a denial's clothes.
    """
    operation = overrides.pop("operation", entry.name)
    payload = overrides.pop("input", {})
    fields: dict[str, Any] = {
        "request_id": "req-inspect-1",
        "correlation_id": "corr-inspect-1",
        "trace_id": "trace-inspect-1",
        "api_version": CONTRACT_VERSION,
        "client": CLIENT,
        "workspace_id": WORKSPACE_ID,
        "scopes": tuple(entry.scope.required_scopes),
        "purpose": WORKSPACE_INSPECTION_PURPOSE,
        "required_capabilities": (
            CapabilityRequirement(
                id=entry.required_capability.id,
                minimum_version=entry.required_capability.minimum_version,
                required=True,
            ),
        ),
        "idempotency_key": (
            "idem-inspect-1" if entry.idempotency.supports_idempotency_key else None
        ),
        "mutation_precondition": (
            MutationPrecondition(record_version="rv-inspect-1")
            if entry.precondition.supports_mutation_precondition
            else None
        ),
        "principal_claim": None,
    }
    fields.update(overrides)
    return RequestEnvelope(
        operation=operation, metadata=RequestMetadata(**fields), input=payload
    )


def refusal(response: ResponseEnvelope) -> ErrorResponseEnvelope:
    """`response` as a refusal, failing loudly if the request was answered instead."""
    assert isinstance(response, ErrorResponseEnvelope), response
    return response


def message(name: str) -> str:
    """One of the seam's frozen refusal messages, read off the module by name.

    Read rather than transcribed: a copy here would drift, and the property under
    test is that a refusal *is* the frozen constant rather than a formatted string
    that currently reads like it.
    """
    constant = getattr(authorization, name)
    assert isinstance(constant, str)
    return constant


def wire_text(response: ResponseEnvelope) -> str:
    """The whole encoded response as one string, for leak assertions.

    Asserting on the message alone would miss a value that reached some other field,
    and the requirement is that the *refusal* discloses nothing -- not that one
    particular field does not.
    """
    return json.dumps(encode_response(response), sort_keys=True)


# --- 1. an ungranted workspace ------------------------------------------------


def test_1_an_ungranted_workspace_is_denied_without_disclosing_workspace_metadata() -> (
    None
):
    """Refused, and the refusal names a category and no workspace at all.

    Falsifier: widen the session's workspace grant and the same request succeeds, so
    the denial is the grant's doing rather than an accident of the request.
    """
    response = refusal(dispatcher().dispatch(request_for(workspace_id=OTHER_WORKSPACE_ID)))

    assert response.error.code == ERROR_CODE_WORKSPACE_NOT_GRANTED
    # The frozen constant itself, not a formatted string that happens to look like it.
    assert response.error.message == message("_MESSAGE_WORKSPACE_NOT_GRANTED")
    assert OTHER_WORKSPACE_ID not in wire_text(response)
    assert WORKSPACE_ID not in wire_text(response)

    # Falsifier: grant the workspace and unbind the endpoint, and the same request
    # stops being refused for *this* reason. It still fails -- no service is attached
    # to answer it -- but the authority question it failed on is now a different one,
    # which is what proves the denial above was the grant and not the request.
    widened = dispatcher(
        session=production_session(
            workspaces=frozenset({WORKSPACE_ID, OTHER_WORKSPACE_ID})
        ),
        binding=ServiceBinding(installation_id=INSTALLATION_ID),
    )
    assert (
        refusal(
            widened.dispatch(request_for(workspace_id=OTHER_WORKSPACE_ID))
        ).error.code
        != ERROR_CODE_WORKSPACE_NOT_GRANTED
    )


# --- 2. a missing capability, both halves -------------------------------------


def test_2a_a_capability_the_session_does_not_grant_is_denied() -> None:
    """The session's half of check 12."""
    response = refusal(
        dispatcher(session=production_session(capabilities=())).dispatch(request_for())
    )

    assert response.error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    assert response.error.message == message("_MESSAGE_CAPABILITY_NOT_GRANTED")


def test_2b_a_capability_this_server_does_not_support_is_denied() -> None:
    """The server's half of check 12, and the reason the snapshot is a real field.

    The two halves produce the same contract error code and *different* frozen
    messages, so this cannot be satisfied by the same code path as 2a.
    """
    response = refusal(dispatcher(supported=()).dispatch(request_for()))

    assert response.error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    assert response.error.message == message("_MESSAGE_CAPABILITY_NOT_SUPPORTED")


def test_2c_a_build_that_registers_no_handler_supports_no_capability() -> None:
    """The snapshot is derived from registration, so an empty build fails closed.

    The second assertion is the widened *exact* tuple, not a membership test. It became
    false by construction when Lane A registered a second handler -- packet §22.1's
    carve-out -- and the replacement it prescribes is the wider exact set, because a
    membership test would still pass with a handler this build never meant to ship.
    """
    assert server_capability_snapshot(ApplicationOperationRegistry()) == ()
    assert server_capability_snapshot(build_application_registry()) == (
        CapabilityRef(
            id=EVIDENCE_ENTRY.required_capability.id,
            version=EVIDENCE_ENTRY.required_capability.minimum_version,
        ),
        CapabilityRef(
            id=ENTRY.required_capability.id,
            version=ENTRY.required_capability.minimum_version,
        ),
    )


def test_2d_the_snapshot_is_not_the_per_response_capability_set() -> None:
    """The defect the packet named: a snapshot at the caller's own claimed version.

    A request claiming a higher floor than this build implements must be refused. If
    `supported_capabilities` were derived from the request -- which is exactly what
    `operations.response_metadata` does for its response decoration -- it would rise
    with the claim and this could never fail.
    """
    response = refusal(
        dispatcher(
            session=production_session(
                capabilities=(
                    CapabilityRef(id=ENTRY.required_capability.id, version="9.0"),
                )
            )
        ).dispatch(
            request_for(
                required_capabilities=(
                    CapabilityRequirement(
                        id=ENTRY.required_capability.id,
                        minimum_version="9.0",
                        required=True,
                    ),
                )
            )
        )
    )

    assert response.error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    assert response.error.message == message("_MESSAGE_CAPABILITY_NOT_SUPPORTED")


# --- 3. an invalid or ungranted purpose ---------------------------------------


@pytest.mark.parametrize("purpose", ["data_export", "workspace_mutation", "anything"])
def test_3_a_purpose_outside_the_fixed_allowlist_is_denied(purpose: str) -> None:
    """The session's `purposes` set is the allowlist, and it holds exactly one name."""
    response = refusal(dispatcher().dispatch(request_for(purpose=purpose)))

    assert response.error.code == ERROR_CODE_INVALID_PURPOSE
    assert response.error.message == message("_MESSAGE_PURPOSE_NOT_ALLOWED")
    assert purpose not in wire_text(response)


def test_3b_the_allowlist_is_exactly_the_two_accepted_purposes() -> None:
    """The widened exact set, and the literals as well as the constants.

    Two purposes from Lane A onward and no more (§20.2): `workspace.inspect` **retains**
    `workspace_inspection` rather than migrating, and one purpose,
    `knowledge_retrieval`, covers every V06-3 read and Context Pack operation. Read
    through the constants alone this would agree with whatever they were renamed to,
    and there is no purpose registry anywhere in the contract to pin them against -- the
    owner's table is the only thing that fixes these two strings, so the literals are
    asserted beside them.
    """
    assert WORKSPACE_INSPECTION_PURPOSE == "workspace_inspection"
    assert KNOWLEDGE_RETRIEVAL_PURPOSE == "knowledge_retrieval"
    assert production_session().purposes == frozenset(
        {"workspace_inspection", "knowledge_retrieval"}
    )


# --- 4. a request cannot replace or expand the authenticated principal --------


def test_4a_a_request_cannot_claim_a_different_principal() -> None:
    response = refusal(
        dispatcher().dispatch(
            request_for(
                principal_claim=PrincipalClaim(claimed_principal_id="someone-else")
            )
        )
    )

    assert response.error.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert response.error.message == message("_MESSAGE_PRINCIPAL_MISMATCH")
    assert "someone-else" not in wire_text(response)


def test_4b_a_request_cannot_claim_a_role_the_session_does_not_hold() -> None:
    """The session holds no roles at all, so every claimed role is refused."""
    assert production_session().roles == frozenset()

    response = refusal(
        dispatcher().dispatch(
            request_for(principal_claim=PrincipalClaim(claimed_roles=("admin",)))
        )
    )

    assert response.error.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert response.error.message == message("_MESSAGE_ROLES_NOT_HELD")
    assert "admin" not in wire_text(response)


def test_4c_a_matching_claim_narrows_and_the_answer_runs_as_the_session() -> None:
    """The falsifier for 4a: claiming the *authenticated* principal is accepted.

    Without this, 4a would pass against a build that refused every claim, which is a
    different rule from "claims narrow, never add".
    """
    context = authorize_application_request(
        request_for(
            principal_claim=PrincipalClaim(claimed_principal_id=LOCAL_PRINCIPAL)
        ),
        session=production_session(),
        binding=ServiceBinding(
            installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
        ),
        supported_capabilities=server_capability_snapshot(build_application_registry()),
    )

    assert context.principal_id == LOCAL_PRINCIPAL


# --- 5. a read-only session cannot be promoted into mutation authority --------


def test_5a_the_granted_operation_set_holds_exactly_the_named_read_set() -> None:
    """Not `APPLICATION_OPERATIONS`, which is all twenty and includes every mutation.

    This is the assertion packet §11.1 predicted would be got wrong. It asserted a
    single name; the Lane A grant is two, so it became false **by design**. The
    replacement is the widened exact set -- deleting it, or softening it to
    `WORKSPACE_INSPECT_OPERATION in session.operations`, is a stop condition, because a
    membership test passes just as happily against all twenty.

    The side-effect check is asked of every granted name from the catalogue, not of the
    literal list, so it is a property of what was granted rather than a restatement of
    it. `scopes` and `capabilities` are asserted as exact sets for the same reason: they
    are *derived* from the catalogue in the constructor, so pinning them here is what
    catches a derivation that started transcribing instead.
    """
    session = production_session()

    assert session.operations == frozenset(
        {WORKSPACE_INSPECT_OPERATION, EVIDENCE_SEARCH_OPERATION}
    )
    assert session.operations != APPLICATION_OPERATIONS
    for name in session.operations:
        assert get_operation_metadata(name).scope.side_effect == "none"
    assert session.scopes == frozenset({"workspace:read", "memory:read"})
    assert session.capabilities == (
        CapabilityRef(id="evidence.read", version="1.0"),
        CapabilityRef(id="workspace.read", version="1.0"),
    )


def test_the_production_grant_is_the_lane_a_grant_main_actually_wires() -> None:
    """The literal in `service.main.serve`, read from its source, is this one.

    `production_session` copies the production grant by hand, and a hand-copied literal
    that nothing compares is how a test comes to certify a session the service does not
    build. The comparison is against `main.py`'s own syntax rather than against a
    constant both import, because a shared constant would make them agree by definition
    and prove nothing about what `serve` passes.
    """
    serve = _main_function("serve")
    call = next(
        node
        for node in ast.walk(serve)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "local_owner_session"
    )
    granted = next(
        keyword.value for keyword in call.keywords if keyword.arg == "operations"
    )

    # `frozenset({A, B})` -- the names, read out of the call `serve` actually makes.
    assert isinstance(granted, ast.Call)
    assert isinstance(granted.func, ast.Name)
    assert granted.func.id == "frozenset"
    assert isinstance(granted.args[0], ast.Set)
    wired = {
        element.id
        for element in granted.args[0].elts
        if isinstance(element, ast.Name)
    }

    assert wired == {"WORKSPACE_INSPECT_OPERATION", "EVIDENCE_SEARCH_OPERATION"}
    assert PRODUCTION_OPERATIONS == frozenset(
        {WORKSPACE_INSPECT_OPERATION, EVIDENCE_SEARCH_OPERATION}
    )


def test_no_session_field_is_populated_from_the_request() -> None:
    """§20.2's widest clause, asked of the constructor's own source.

    *"No session field may be populated from the request."* Not only the operation set:
    the principal, roles, installations, workspaces, scopes, purposes and capabilities
    too. `local_owner_session` takes four parameters and none of them is a request, an
    envelope or a payload, so there is nothing in scope for a field to be read from --
    which is a stronger statement than any assertion about a particular field's value.
    """
    signature = inspect.signature(local_owner_session)

    assert set(signature.parameters) == {
        "principal_id",
        "installation_id",
        "workspace_id",
        "operations",
    }
    for parameter in signature.parameters.values():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    # The identifiers the code actually reads, not a substring scan over the text. A
    # text scan is wrong in both directions here: this function's prose is largely
    # *about* requests, and `get_operation_metadata` contains "metadata" while being
    # the frozen catalogue lookup that makes the derivation trustworthy.
    body = ast.parse(textwrap.dedent(inspect.getsource(local_owner_session))).body[0]
    assert isinstance(body, ast.FunctionDef)
    read = {node.id for node in ast.walk(body) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(body) if isinstance(node, ast.Attribute)
    }

    assert read.isdisjoint({"request", "envelope", "payload", "metadata", "input"})


def test_5b_a_mutating_operation_is_denied_under_the_production_session() -> None:
    """The seam's operation allowlist, asked directly with the production session.

    Asked directly on purpose: a mutating operation cannot reach the seam through the
    production path at all, because nothing registers a handler for one. Both facts
    are the answer to "can this be promoted", and the second is asserted below.
    """
    with pytest.raises(ApplicationAuthorizationError) as denied:
        authorize_application_request(
            # Well formed *for the mutating entry*, so the refusal below is an
            # authority decision and not a malformed request wearing a denial's
            # clothes: the seam validates metadata before it asks any authority
            # question, and a mutating operation's idempotency and precondition
            # postures differ from this one's.
            request_for(MUTATING_ENTRY),
            session=production_session(),
            binding=ServiceBinding(
                installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
            ),
            supported_capabilities=server_capability_snapshot(
                build_application_registry()
            ),
        )

    # The operation allowlist, specifically. The session grants no mutation scope
    # either, but the allowlist is checked first, so this pins the narrowest true
    # refusal rather than whichever gate happens to fire.
    assert denied.value.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert denied.value.message == message("_MESSAGE_OPERATION_NOT_GRANTED")
    assert MUTATING_ENTRY.scope.required_scopes[0] not in production_session().scopes


def test_5c_no_mutating_operation_is_registered_at_all() -> None:
    """The widened exact set again -- §22.1's second carve-out, at the registry side.

    This is the assertion that reads the *production* registry builder rather than a
    registry the test composes, so it became false the moment Lane A registered a
    second handler. A membership test here would pass with a mutating handler
    registered alongside, which is the one thing it exists to catch.
    """
    registered = build_application_registry().operations

    assert registered == frozenset(
        {WORKSPACE_INSPECT_OPERATION, EVIDENCE_SEARCH_OPERATION}
    )
    for name in registered:
        assert get_operation_metadata(name).scope.side_effect == "none"


# --- 6. no wildcard workspace access through request fields -------------------


@pytest.mark.parametrize("claimed", ["*", "", "all", OTHER_WORKSPACE_ID])
def test_6a_a_request_field_cannot_introduce_a_wildcard_workspace(claimed: str) -> None:
    """There is no wildcard to reach: membership is the only test, and `*` is a name."""
    response = refusal(dispatcher().dispatch(request_for(workspace_id=claimed)))

    assert response.error.code in {
        ERROR_CODE_WORKSPACE_NOT_GRANTED,
        ERROR_CODE_INVALID_REQUEST,
    }
    assert claimed == "" or claimed not in wire_text(response)


def test_6b_a_workspace_scoped_request_naming_no_workspace_is_refused() -> None:
    response = refusal(dispatcher().dispatch(request_for(workspace_id=None)))

    assert response.error.code == ERROR_CODE_INVALID_REQUEST


def test_6c_the_workspace_specific_binding_refuses_a_second_granted_workspace() -> None:
    """The binding is a second, independent lock, and it is armed.

    The session here *does* grant the other workspace, so the only thing left to
    refuse the request is the endpoint binding `service.main` sets.
    """
    response = refusal(
        dispatcher(
            session=production_session(
                workspaces=frozenset({WORKSPACE_ID, OTHER_WORKSPACE_ID})
            )
        ).dispatch(request_for(workspace_id=OTHER_WORKSPACE_ID))
    )

    assert response.error.code == ERROR_CODE_WORKSPACE_NOT_GRANTED
    assert response.error.message == message("_MESSAGE_WORKSPACE_ENDPOINT_MISMATCH")


def test_6d_the_input_payload_is_not_read_before_the_handler_is_reached() -> None:
    """A foreign id in the *payload* does not redirect the request.

    This half proves only that the payload cannot cause a *refusal*: the request
    is authorised, so it reaches the handler, and the handler refuses for the
    absent served workspace rather than for the workspace grant. The half that
    proves the answer is the bound workspace needs a served workspace and lives
    in the companion module as
    ``test_the_input_payload_cannot_redirect_the_answered_workspace``.

    An earlier revision put ``OTHER_WORKSPACE_ID`` in the *metadata*, so check 8
    refused it before any handler ran and the assertion was satisfied by the
    workspace mismatch. A handler trusting ``context.request.input`` passed it
    unchanged, because the handler was never invoked at all.
    """
    response = dispatcher().dispatch(
        request_for(input={"workspace_id": OTHER_WORKSPACE_ID})
    )

    refused = refusal(response)
    assert refused.error.code != ERROR_CODE_WORKSPACE_NOT_GRANTED
    assert refused.error.message == "this service instance is not serving a workspace"


# --- 7. the binding clause ----------------------------------------------------


def test_7a_the_operation_is_absent_from_every_probe_registration() -> None:
    """Absence, not presence. A build that registered in both places would pass a
    presence-only test, and would violate the clause.
    """
    assert WORKSPACE_INSPECT_OPERATION not in SERVICE_OPERATIONS
    assert WORKSPACE_INSPECT_OPERATION not in build_service_registry()
    assert WORKSPACE_INSPECT_OPERATION not in probe_dispatcher().grant.operations
    assert APPLICATION_OPERATIONS.isdisjoint(frozenset(SERVICE_OPERATIONS))


def test_7b_the_probe_dispatcher_still_refuses_the_operation_outright() -> None:
    """Reached directly, the probe path answers exactly what it did before this lane."""
    response = refusal(probe_dispatcher().dispatch(request_for()))

    assert response.error.code == "core.operation_not_implemented"


def test_7c_the_production_path_never_delegates_the_operation_to_the_probe() -> None:
    """The clause, as behaviour rather than as structure.

    A probe dispatcher that records what it was asked. `workspace.inspect` must never
    appear; a probe operation must, because the probe seam is still the thing that
    answers those and this path must not have quietly absorbed them.
    """
    asked: list[str] = []

    class Recording(Dispatcher):
        def dispatch(self, request: RequestEnvelope) -> ResponseEnvelope:
            asked.append(request.operation)
            return super().dispatch(request)

    recording = Recording.for_service_operations(
        Grant(
            principal=LOCAL_PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )
    path = ApplicationDispatcher(
        registry=build_application_registry(),
        session=production_session(),
        binding=ServiceBinding(
            installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
        ),
        supported_capabilities=server_capability_snapshot(build_application_registry()),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=recording,
        record=None,
    )

    path.dispatch(request_for())
    assert asked == []

    path.dispatch(request_for(operation="core.health"))
    assert asked == ["core.health"]


def test_7d_the_wiring_hands_the_router_the_application_path() -> None:
    """`service.main.serve` composes the application path and gives it to the router.

    Read out of the source: proving it through a live startup would need a workspace,
    a lock and a storage backend to establish a wiring fact.
    """
    serve = _main_function("serve")
    called = {
        node.func.id
        for node in ast.walk(serve)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert {
        "ApplicationDispatcher",
        "build_application_registry",
        "local_owner_session",
        "server_capability_snapshot",
        "ServiceBinding",
        "_router_for",
    } <= called

    router_call = next(
        node
        for node in ast.walk(serve)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_router_for"
    )
    passed = router_call.args[1]
    assert isinstance(passed, ast.Name)
    assert passed.id == "application"

    # The endpoint binding is workspace-specific, which is what arms the seam's
    # second, independent workspace check. Dropping the keyword leaves a binding that
    # accepts any granted workspace at an endpoint that fronts exactly one, and every
    # other test in this file would stay green.
    binding_call = next(
        node
        for node in ast.walk(serve)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ServiceBinding"
    )
    named = {keyword.arg for keyword in binding_call.keywords}
    assert named == {"installation_id", "workspace_id"}


def _main_function(name: str) -> ast.FunctionDef:
    """The AST of one function in `service.main`."""
    import inspect as inspect_module

    from omnivia_core_runtime.service import main as module

    tree = ast.parse(inspect_module.getsource(module))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


# --- 8. authoritative storage stays with the service --------------------------


def test_8a_the_handler_opens_no_authoritative_storage() -> None:
    """The runtime half of "the CLI cannot open authoritative workspace storage".

    The CLI half is guarded from the other end -- `check-package-boundaries.py` and
    the phase 2 adapter tests both refuse a CLI that imports the runtime, imports
    `sqlite3` or calls a lease or lock primitive -- and Lane B adds the files those
    guards then have to hold for. What Lane A can break is this end: a handler that
    opened storage of its own would make the service's ownership of it a convention
    rather than a fact.
    """
    from omnivia_core_runtime.service.handlers import workspace as handler_module

    source = Path(handler_module.__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("storage" in name for name in imported), imported
    assert "sqlite3" not in imported

    called = {
        node.func.id
        if isinstance(node.func, ast.Name)
        else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in (
        "acquire_lease",
        "flock",
        "open_guard",
        "create_lock",
        "open_database",
        "connect",
    ):
        assert forbidden not in called


def test_8b_a_handler_is_given_no_connection_and_no_path() -> None:
    """The handler contract itself carries nothing a handler could open.

    `OperationContext` has five fields and none of them is a connection, a lease or a
    filesystem path; `service` is the workspace-owning service, which is what makes it
    the owner rather than the handler.
    """
    fields = {field for field in OperationContext.__dataclass_fields__}

    assert fields == {
        "request",
        "principal",
        "workspace_id",
        "granted_operations",
        "service",
    }


def test_8c_this_lane_adds_no_caller_supplied_path() -> None:
    """`workspace.inspect` takes `{}`, so the containment clause governs successors.

    Asserted rather than assumed, because the first operation that *does* take a path
    must resolve it through `workspace/filesystem.py`'s canonical functions --
    `resolve_within` and `assert_no_unsafe_symlink`, both still uncalled by any
    production code after this lane -- rather than discover that nobody had
    established the rule.
    """
    assert ENTRY.input_schema_ref.endswith("WorkspaceInspectInput")
    assert dataclasses.fields(WorkspaceInspectInput) == ()
