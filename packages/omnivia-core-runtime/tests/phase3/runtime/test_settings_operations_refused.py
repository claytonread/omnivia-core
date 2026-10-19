"""`settings.list` and `settings.set` are published, composed into the production surface, and refused.

The Settings v2 packet publishes stored application settings as two Application Contract operations,
with CLI commands and server-owned mutation entries. The registry must hold a handler for every
catalogue operation or the service does not start, so this build serves them as typed refusals
(`dependency_unavailable`) from one authority family of their own. These tests drive the real
production surface over a real migrated workspace and pin what that does and does not mean: the
surface is complete at 88 operations, the family holds exactly these two and no other session can
reach them, a valid request is refused and a malformed one is invalid, and nothing is read, written,
audited or claimed either way.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_cli.surface import APPLICATION_COMMANDS
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service.application import (
    SETTINGS_FAMILY_PURPOSES,
    ProductionApplicationSurface,
    build_installation_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers import settings as settings_handlers
from omnivia_core_runtime.service.installation import InstallationApplicationService
from omnivia_core_runtime.service.main import _build_production_application_surface
from omnivia_core_runtime.service.mutation import MUTATION_PURPOSES, MUTATION_ROLES
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.runner import ServiceRunner

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    ResponseEnvelope,
    decode_request,
    encode_request,
    get_operation_metadata,
)

WS = m1.WORKSPACE_ID
PRINCIPAL = "local-user"
LIST = "settings.list"
SET = "settings.set"
SETTINGS = (LIST, SET)

#: A value the contract admits.
VALUE = json.dumps({"enabled": True})


class _InstallationService:
    """Construction-only shape; its bound production handlers are never invoked."""

    authority = SimpleNamespace(installation_id=s0.INSTALLATION_ID)


class Harness:
    """An owned, fully migrated workspace behind the production surface."""

    def __init__(self, holder: m1.Owned) -> None:
        self.holder = holder
        probe = Dispatcher.for_service_operations(
            Grant(
                principal=PRINCIPAL,
                workspaces=frozenset({WS}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            holder,
        )
        started = SimpleNamespace(**vars(holder), workspace_id=WS, clock=SystemClock())
        installation = build_installation_application_dispatcher(
            service=cast(InstallationApplicationService, _InstallationService()),
            principal_id=PRINCIPAL,
            fallback=probe,
        )
        self.surface: ProductionApplicationSurface = _build_production_application_surface(
            started=cast(ServiceRunner, started),
            probe=probe,
            installation=installation,
        )
        self._requests = 0

    def call(
        self,
        operation: str,
        payload: Mapping[str, object],
        *,
        wire: bool = False,
        purpose: str | None = None,
    ) -> ResponseEnvelope:
        """One request through the production surface, as the session the surface issues for it.

        Every call is traced: not one SQL statement may run on the service's connection while the
        surface answers, so that no request, valid or not, reads, writes, claims, audits or grants.
        """
        self._requests += 1
        request_id = f"req-settings-{self._requests}"
        entry = get_operation_metadata(operation)
        overrides: dict[str, str] = {
            "request_id": request_id,
            "correlation_id": f"cor-{request_id}",
            "trace_id": f"trc-{request_id}",
            "purpose": purpose or SETTINGS_FAMILY_PURPOSES[operation],
            "workspace_id": WS,
        }
        if entry.idempotency.supports_idempotency_key:
            overrides["idempotency_key"] = f"idem-{request_id}"
        envelope = s0.envelope_for(entry, operation_input=payload, **overrides)
        if wire:
            # Every transport hands a handler a decoded, read-only mapping.
            envelope = decode_request(encode_request(envelope))
        session = self.surface.session_for(operation)
        assert session is not None
        statements: list[str] = []
        self.holder.connection.set_trace_callback(statements.append)
        try:
            response = self.surface.dispatch_for_session(envelope, session)
        finally:
            self.holder.connection.set_trace_callback(None)
        assert statements == [], statements
        return response

    def code(
        self,
        operation: str,
        payload: Mapping[str, object],
        *,
        wire: bool = False,
        purpose: str | None = None,
    ) -> str:
        response = self.call(operation, payload, wire=wire, purpose=purpose)
        assert isinstance(response, ErrorResponseEnvelope), response
        return str(response.error.code)

    def count(self, table: str) -> int:
        return int(self.holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    path = tmp_path / "workspace.sqlite"
    m1.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = m1.take_ownership(path, workspace_id=WS)
    yield Harness(holder)
    holder.connection.close()


# -- composition ----------------------------------------------------------------------------


def test_the_production_surface_is_complete_with_both_operations_in_one_family(
    harness: Harness,
) -> None:
    surface = harness.surface
    surface.registry.assert_complete()
    assert len(surface.registry.operations) == 93
    families = {id(surface._routes[name]) for name in SETTINGS}
    assert len(families) == 1
    for name, handler in zip(SETTINGS, (settings_handlers.settings_list, settings_handlers.settings_set)):
        assert surface.registry.get(name) is handler
    others = surface.registry.operations - set(SETTINGS)
    assert not any(id(surface._routes[name]) in families for name in others)


def test_the_catalogue_appends_the_two_operations_last(harness: Harness) -> None:
    from omnivia_core.contracts.v1.generated import OPERATION_CATALOGUE

    names = [entry.name for entry in OPERATION_CATALOGUE]
    assert names[-7:-5] == list(SETTINGS)
    assert names[-8] == "document.original.relink"
    assert names[-5:] == [
        "code.query",
        "code.policy.register",
        "code.import",
        "code.status",
        "evidence.erase",
    ]


def test_the_family_session_grants_exactly_these_two_operations(harness: Harness) -> None:
    session = harness.surface.session_for(LIST)
    assert session is not None and session is harness.surface.session_for(SET)
    assert session.operations == frozenset(SETTINGS)
    assert session.roles == frozenset({MUTATION_ROLES[SET]})
    assert session.purposes == frozenset({"workspace_inspection", "workspace_administration"})
    assert session.purposes == frozenset(SETTINGS_FAMILY_PURPOSES.values())
    assert session.scopes == frozenset({"workspace:read", "workspace:write"})
    assert {ref.id for ref in session.capabilities} == {"workspace.read", "workspace.write"}


def test_the_family_purposes_are_the_ones_the_cli_commands_declare() -> None:
    # The seam checks a purpose against the session, not the operation, so a swap inside the
    # family's two purposes would pass it; this holds the two tables to each other.
    declared = {c.operation: c.purpose for c in APPLICATION_COMMANDS if c.operation in SETTINGS}
    assert declared == dict(SETTINGS_FAMILY_PURPOSES)
    assert SETTINGS_FAMILY_PURPOSES[SET] == MUTATION_PURPOSES[SET]


def test_no_other_family_can_be_asked_for_either_operation(harness: Harness) -> None:
    surface = harness.surface
    settings_session = surface.session_for(LIST)
    sessions = {
        id(surface.session_for(name)): surface.session_for(name)
        for name in surface.registry.operations
        if name not in SETTINGS
    }
    assert sessions and settings_session is not None
    for session in sessions.values():
        assert session is not None
        assert session is not settings_session
        assert not set(SETTINGS) & set(session.operations)


# -- the refusal ----------------------------------------------------------------------------


@pytest.mark.parametrize("wire", [False, True], ids=["in_process", "decoded_wire"])
@pytest.mark.parametrize("payload", [{}, {"prefix": "document"}, {"prefix": "document.processing"}])
def test_a_valid_read_is_refused_as_unavailable_and_reads_nothing(
    harness: Harness, payload: Mapping[str, object], wire: bool
) -> None:
    response = harness.call(LIST, payload, wire=wire)
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "dependency_unavailable"
    assert "nothing was read" in response.error.message


@pytest.mark.parametrize("wire", [False, True], ids=["in_process", "decoded_wire"])
def test_a_valid_change_is_refused_as_unavailable_and_changes_nothing(
    harness: Harness, wire: bool
) -> None:
    audit = harness.count("omnivia_application_audit_events")
    response = harness.call(SET, {"key": "document.processing", "value_json": VALUE}, wire=wire)
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "dependency_unavailable"
    assert "nothing was changed" in response.error.message
    # No stored value, no audit row: a refusal is not a governed mutation.
    assert harness.count("omnivia_app_settings") == 0
    assert harness.count("omnivia_application_audit_events") == audit


@pytest.mark.parametrize(
    "payload",
    [
        {"prefix": "Bad-Key"},  # not an OpenCode
        {"prefix": "a..b"},
        {"prefix": 3},
        {"prefix": ""},
        {"scope": "workspace"},  # a member the request does not declare
        {"prefix": "document", "key": "document"},
    ],
)
def test_a_malformed_read_is_invalid_not_unavailable(
    harness: Harness, payload: Mapping[str, object]
) -> None:
    assert harness.code(LIST, payload) == "invalid_request"
    assert harness.code(LIST, payload, wire=True) == "invalid_request"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"key": "document.processing"},  # no value
        {"value_json": VALUE},  # no key
        {"key": "Document.Processing", "value_json": VALUE},  # not an OpenCode
        {"key": "document-processing", "value_json": VALUE},  # a hyphen the contract refuses
        {"key": "", "value_json": VALUE},
        {"key": "document.processing", "value_json": ""},
        {"key": "document.processing", "value_json": "x" * 16385},  # one past SettingValueJson
        {"key": "document.processing", "value_json": 3},
        {"key": "document.processing", "value_json": VALUE, "record_version": "1"},  # no precondition
        {"key": "document.processing", "value_json": VALUE, "workspace_id": WS},  # never a request field
    ],
)
def test_a_malformed_change_is_invalid_not_unavailable(
    harness: Harness, payload: Mapping[str, object]
) -> None:
    assert harness.code(SET, payload) == "invalid_request"
    assert harness.code(SET, payload, wire=True) == "invalid_request"
    assert harness.count("omnivia_app_settings") == 0


def test_the_declared_value_bound_is_the_boundary(harness: Harness) -> None:
    # The contract states a length and nothing else about `value_json` (see the handler module
    # for what a handler that serves it still has to settle), so the boundary is all that is held.
    assert harness.code(SET, {"key": "document.processing", "value_json": "x" * 16384}) == (
        "dependency_unavailable"
    )
    assert harness.code(SET, {"key": "document.processing", "value_json": "x" * 16385}) == (
        "invalid_request"
    )


@pytest.mark.parametrize("operation", SETTINGS)
def test_a_purpose_the_family_does_not_hold_is_refused_before_the_handler(
    harness: Harness, operation: str
) -> None:
    payload = {} if operation == LIST else {"key": "document.processing", "value_json": VALUE}
    assert harness.code(operation, payload, purpose="knowledge_retrieval") == "invalid_purpose"
