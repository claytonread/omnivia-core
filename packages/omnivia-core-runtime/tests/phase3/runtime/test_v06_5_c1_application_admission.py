"""C1 coverage for server-owned application authorization and admission refusals."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
import test_v06_5_s2_memory_family as s2
import test_v06_5_s5_integrated_registry as s5
from omnivia_core_runtime.service.admission import ApplicationAdmission
from omnivia_core_runtime.service.application import (
    MUTATION_PURPOSES,
    ApplicationDispatcher,
    ProductionApplicationSurface,
    _narrow_session,
)
from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.service.operations import ApplicationOperationRegistry

from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_AUTHENTICATION_REQUIRED,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CANCELLED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_DEADLINE_EXCEEDED,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    ERROR_CODE_INVALID_PURPOSE,
    ERROR_CODE_RATE_LIMITED,
    ERROR_CODE_UPGRADE_REQUIRED,
    ERROR_CODE_WORKSPACE_BUSY,
    ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE,
    ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    CapabilityRef,
    ErrorResponseEnvelope,
    MutationPrecondition,
    RequestEnvelope,
    ResponseEnvelope,
    get_operation_metadata,
)

ADAPTERS = ("in-process", "local-ipc", "http")


@pytest.fixture
def surface(tmp_path: Path) -> Iterator[ProductionApplicationSurface]:
    fixture = s5.surface.__wrapped__(tmp_path)  # type: ignore[attr-defined]
    yield next(fixture)
    next(fixture, None)


def _candidate_request(tag: str):
    return s0.envelope_for(
        get_operation_metadata("candidate.approve"),
        operation_input={
            "record_id": "rec-c1-admission",
            "rationale": {"reason_code": "c1_admission"},
        },
        request_id=f"req-c1-admission-{tag}",
        correlation_id=f"cor-c1-admission-{tag}",
        trace_id=f"trc-c1-admission-{tag}",
        idempotency_key=f"idem-c1-admission-{tag}",
        mutation_precondition=MutationPrecondition(record_version="v1"),
        purpose=MUTATION_PURPOSES["candidate.approve"],
        workspace_id=m1.WORKSPACE_ID,
    )


def _route_with(
    route: ApplicationDispatcher,
    *,
    session: AuthenticatedSession | None = None,
    admission: ApplicationAdmission | None = None,
) -> ApplicationDispatcher:
    return replace(
        route,
        session=route.session if session is None else session,
        admission=route.admission if admission is None else admission,
    )


@dataclass(frozen=True)
class _NoSessionRoute:
    """Select the real fail-closed application seam behind each real adapter."""

    route: ApplicationDispatcher

    @property
    def session(self) -> AuthenticatedSession:
        return self.route.session

    @property
    def registry(self) -> ApplicationOperationRegistry:
        return self.route.registry

    def dispatch(self, request: RequestEnvelope) -> ResponseEnvelope:
        return self.route.dispatch_without_session(request)

    def dispatch_for_session(
        self, request: RequestEnvelope, session: AuthenticatedSession
    ) -> ResponseEnvelope:
        del session
        return self.route.dispatch_without_session(request)


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize(
    ("code", "admission"),
    (
        (ERROR_CODE_WORKSPACE_BUSY, ApplicationAdmission(workspace="busy")),
        (
            ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE,
            ApplicationAdmission(workspace="lease_unavailable"),
        ),
        (
            ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
            ApplicationAdmission(workspace="migration_required"),
        ),
        (
            ERROR_CODE_INCOMPATIBLE_VERSION,
            ApplicationAdmission(workspace="incompatible"),
        ),
        (
            ERROR_CODE_UPGRADE_REQUIRED,
            ApplicationAdmission(workspace="upgrade_required"),
        ),
        (ERROR_CODE_RATE_LIMITED, ApplicationAdmission(capacity="rate_limited")),
        (
            ERROR_CODE_DEPENDENCY_UNAVAILABLE,
            ApplicationAdmission(dependency="unavailable"),
        ),
        (
            ERROR_CODE_INTERNAL_RECOVERABLE,
            ApplicationAdmission(dependency="recoverable_fault"),
        ),
    ),
)
def test_v06_5_c1_server_admission_refusals_cross_every_real_adapter(
    surface: ProductionApplicationSurface,
    adapter: str,
    code: str,
    admission: ApplicationAdmission,
) -> None:
    route = surface._routes["candidate.approve"]
    dispatcher = _route_with(route, admission=admission)
    response = s2._transport_call(
        adapter,
        dispatcher,
        _candidate_request(code),
        case_id=f"error/{code}",
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == code
    assert response.error.retry_class == DEFAULT_RETRY_CLASSIFICATION[code]


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("code", (ERROR_CODE_DEADLINE_EXCEEDED, ERROR_CODE_CANCELLED))
def test_v06_5_c1_request_lifecycle_refusals_cross_every_real_adapter(
    surface: ProductionApplicationSurface, adapter: str, code: str
) -> None:
    route = surface._routes["candidate.approve"]
    request = _candidate_request(code)
    if code == ERROR_CODE_DEADLINE_EXCEEDED:
        request = replace(request, metadata=replace(request.metadata, deadline_ms=0))
        admission = ApplicationAdmission()
    else:
        admission = ApplicationAdmission(
            cancelled_request_ids=frozenset({request.metadata.request_id})
        )
    response = s2._transport_call(
        adapter,
        _route_with(route, admission=admission),
        request,
        case_id=f"error/{code}",
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == code
    assert response.error.retry_class == DEFAULT_RETRY_CLASSIFICATION[code]


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize(
    "code",
    (
        ERROR_CODE_AUTHORIZATION_DENIED,
        ERROR_CODE_WORKSPACE_NOT_GRANTED,
        ERROR_CODE_CAPABILITY_NOT_GRANTED,
        ERROR_CODE_INVALID_PURPOSE,
    ),
)
def test_v06_5_c1_authorization_refusals_cross_every_real_adapter(
    surface: ProductionApplicationSurface, adapter: str, code: str
) -> None:
    route = surface._routes["candidate.approve"]
    request = _candidate_request(code)
    transport_session = route.session
    session = transport_session
    if code == ERROR_CODE_AUTHORIZATION_DENIED:
        session = replace(session, operations=frozenset())
    elif code == ERROR_CODE_WORKSPACE_NOT_GRANTED:
        session = replace(session, workspaces=frozenset())
    elif code == ERROR_CODE_CAPABILITY_NOT_GRANTED:
        session = replace(session, capabilities=())
    else:
        session = replace(session, purposes=frozenset())
    dispatcher = _route_with(route, session=session)
    response = s2._transport_call(
        adapter,
        dispatcher,
        request,
        http_session=transport_session,
        case_id=f"error/{code}",
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == code
    assert response.error.retry_class == DEFAULT_RETRY_CLASSIFICATION[code]


# --- the configured session is a ceiling, not an identity ---------------------
#
# A dedicated installed-MCP principal is never the configured local owner: the
# endpoint is wired as `local-user` and the catalogue mints `mcp-<host>-<hex>`.
# `_narrow_session` used to compare the two ids and hand back an empty session
# when they differed, which refused every authenticated MCP call
# `workspace_not_granted` before a handler saw it. The ceiling is what bounds a
# foreign caller -- every dimension intersected -- and the principal is preserved
# because the audit record has to say who the request ran as.

FOREIGN_PRINCIPAL = "mcp-claude-code-0123456789abcdef"


def _foreign(session: AuthenticatedSession, **overrides: object) -> AuthenticatedSession:
    return replace(session, principal_id=FOREIGN_PRINCIPAL, **overrides)  # type: ignore[arg-type]


def test_v06_5_c1_a_foreign_principal_is_narrowed_to_the_ceiling_and_not_emptied(
    surface: ProductionApplicationSurface,
) -> None:
    ceiling = surface._routes["candidate.approve"].session
    narrowed = _narrow_session(ceiling, _foreign(ceiling))

    assert narrowed.principal_id == FOREIGN_PRINCIPAL
    assert narrowed.workspaces == ceiling.workspaces
    assert narrowed.operations == ceiling.operations
    assert narrowed.scopes == ceiling.scopes
    assert narrowed.purposes == ceiling.purposes
    assert narrowed.roles == ceiling.roles
    assert narrowed.capabilities == ceiling.capabilities
    assert narrowed.installations == ceiling.installations
    # The ceiling itself grants something, so "unchanged" is a claim about real
    # authority rather than about two empty sets agreeing.
    assert ceiling.operations and ceiling.workspaces and ceiling.roles


def test_v06_5_c1_a_foreign_principal_cannot_widen_any_dimension(
    surface: ProductionApplicationSurface,
) -> None:
    """Claims narrow. Every one of them, whoever the caller says it is."""
    ceiling = surface._routes["candidate.approve"].session
    claimed = _foreign(
        ceiling,
        roles=ceiling.roles | {"installation_administrator"},
        installations=frozenset({"inst-somebody-elses"}),
        workspaces=ceiling.workspaces | {"ws-somebody-elses"},
        operations=ceiling.operations | {"workspace.create"},
        scopes=ceiling.scopes | {"installation:write"},
        purposes=ceiling.purposes | {"workspace_provisioning"},
        capabilities=(
            *ceiling.capabilities,
            CapabilityRef(id="installation.write", version="9.0"),
        ),
    )
    narrowed = _narrow_session(ceiling, claimed)

    assert narrowed.roles == ceiling.roles
    assert "installation_administrator" not in narrowed.roles
    # An installation the ceiling does not hold is not one a claim can add, and
    # naming only that one leaves the caller holding none.
    assert narrowed.installations == frozenset()
    assert narrowed.workspaces == ceiling.workspaces
    assert narrowed.operations == ceiling.operations
    assert narrowed.scopes == ceiling.scopes
    assert narrowed.purposes == ceiling.purposes
    assert narrowed.capabilities == ceiling.capabilities


def test_v06_5_c1_the_ceiling_still_bounds_a_foreign_principal_that_holds_less(
    surface: ProductionApplicationSurface,
) -> None:
    """Narrowing runs both ways: the lesser of the two, dimension by dimension."""
    ceiling = surface._routes["candidate.approve"].session
    restricted = _foreign(
        ceiling,
        roles=frozenset(),
        workspaces=frozenset(),
        operations=frozenset({next(iter(ceiling.operations))}),
        capabilities=(),
    )
    narrowed = _narrow_session(ceiling, restricted)

    assert narrowed.roles == frozenset()
    assert narrowed.workspaces == frozenset()
    assert narrowed.operations == restricted.operations
    assert narrowed.capabilities == ()


def test_v06_5_c1_the_same_principal_is_narrowed_exactly_as_before(
    surface: ProductionApplicationSurface,
) -> None:
    """The path that already worked is untouched by the repair."""
    ceiling = surface._routes["candidate.approve"].session
    assert _narrow_session(ceiling, ceiling) == ceiling
    lesser = replace(ceiling, operations=frozenset(), purposes=frozenset())
    assert _narrow_session(ceiling, lesser).operations == frozenset()
    assert _narrow_session(ceiling, lesser).purposes == frozenset()
    assert _narrow_session(ceiling, lesser).workspaces == ceiling.workspaces


def test_v06_5_c1_a_foreign_principal_reaches_the_seam_and_is_judged_on_its_grants(
    surface: ProductionApplicationSurface,
) -> None:
    """`dispatch_for_session` is the installed-MCP path, and this is the repair.

    The same request through the same route under three kinds of caller session.
    A foreign principal holding the ceiling's own grants gets the endpoint's own
    answer -- the same one the configured session gets, which here is the mutation
    coordinator refusing a request that carries no server-issued grant, reached
    only *after* the authorization seam admitted it. One holding less is refused
    for what it actually lacks. `dispatch_for_session` rather than a transport
    adapter because that is the method `AuthenticatedApplicationDispatch` calls
    with a resolved MCP session -- the two in-process adapters never reach it, and
    the HTTP listener refuses a session for a principal other than the endpoint's
    own before it does.
    """
    route = surface._routes["candidate.approve"]
    ceiling = route.session

    own = route.dispatch(_candidate_request("own"))
    granted = route.dispatch_for_session(
        _candidate_request("own"), _foreign(ceiling)
    )
    assert isinstance(own, ErrorResponseEnvelope), own
    assert isinstance(granted, ErrorResponseEnvelope), granted
    assert granted.error == own.error
    # And that shared answer is the handler's, not the seam's: the request got past
    # authorization and past the mutation coordinator to the domain itself.
    assert own.error.code not in (
        ERROR_CODE_WORKSPACE_NOT_GRANTED,
        ERROR_CODE_AUTHORIZATION_DENIED,
        ERROR_CODE_CAPABILITY_NOT_GRANTED,
        ERROR_CODE_INVALID_PURPOSE,
    )

    for code, overrides in (
        (ERROR_CODE_WORKSPACE_NOT_GRANTED, {"workspaces": frozenset()}),
        (ERROR_CODE_AUTHORIZATION_DENIED, {"operations": frozenset()}),
        (ERROR_CODE_CAPABILITY_NOT_GRANTED, {"capabilities": ()}),
        (ERROR_CODE_INVALID_PURPOSE, {"purposes": frozenset()}),
    ):
        response = route.dispatch_for_session(
            _candidate_request(f"foreign-{code}"),
            _foreign(ceiling, **overrides),  # type: ignore[arg-type]
        )
        assert isinstance(response, ErrorResponseEnvelope), response
        assert response.error.code == code


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_v06_5_c1_authentication_refusal_crosses_every_real_adapter(
    surface: ProductionApplicationSurface, adapter: str
) -> None:
    route = surface._routes["candidate.approve"]
    no_session = cast(ApplicationDispatcher, _NoSessionRoute(route))
    response = s2._transport_call(
        adapter,
        no_session,
        _candidate_request(ERROR_CODE_AUTHENTICATION_REQUIRED),
        http_session=route.session,
        case_id="error/authentication_required",
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == ERROR_CODE_AUTHENTICATION_REQUIRED
