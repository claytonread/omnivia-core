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
)
from omnivia_core_runtime.service.authorization import AuthenticatedSession

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
    response = s2._transport_call(adapter, dispatcher, _candidate_request(code))

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == code
    assert response.error.retry_class == DEFAULT_RETRY_CLASSIFICATION[code]


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize(
    "code", (ERROR_CODE_DEADLINE_EXCEEDED, ERROR_CODE_CANCELLED)
)
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
        adapter, _route_with(route, admission=admission), request
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
        adapter, dispatcher, request, http_session=transport_session
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == code
    assert response.error.retry_class == DEFAULT_RETRY_CLASSIFICATION[code]


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
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == ERROR_CODE_AUTHENTICATION_REQUIRED
