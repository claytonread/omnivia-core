"""The Decision Runtime records slice, end to end against real storage.

ADR-042, plan PR-3. The path under test is the production one: a fully
migrated workspace, the owned fenced connection, and the decision family's
`ApplicationDispatcher` built exactly as `service.main.serve` builds it --
envelopes in, the §8 lifecycle out, through the same authorization seam and the
same mutation coordinator every other family uses.

The claims below are the plan's exit criterion, made concrete:

- Local Decisions is off by default and says so (§28.2); enabling is a
  compare-and-swap settings write, and a stale revision is a conflict;
- a published definition is immutable and content-digested (§7.1), and
  evaluate against it runs the deterministic route (§12.1), committing the
  evaluation, its attempt, its terminal result and its outbox event in one
  transaction (§14.5);
- replaying an identical request returns the same evaluation, and replaying the
  key with a different request is a conflict (§15.1, AT-42/43) -- the
  coordinator's decisions, not this family's;
- an unconclusive evaluation abstains (§11.4) and a model route fails closed
  with `model_not_installed` (AT-36); neither fabricates an answer;
- outcomes are append-only with provenance, and a correction can supersede
  exactly one earlier outcome of its own evaluation (§14.4, AT-53);
- disabling the definition or the capability stops new admissions without
  touching history (§28.3);
- the result-use gate answers the bare catalogue result at the handler's one
  clock reading, refuses with distinct fixed-message typed codes, and lets a
  programmer error surface rather than passing it off as a request refusal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from jsonschema import Draft202012Validator
from omnivia_core_runtime.ownership.identity import FakeClock, SystemClock
from omnivia_core_runtime.service.application import (
    build_decision_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers import decisions
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from referencing import Registry, Resource
from test_application_audit_idempotency_migration import (
    bootstrap_and_migrate,
    materialise_phase0_baseline,
    take_ownership,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_UNSUPPORTED_MINOR_VERSION,
    OPERATION_CATALOGUE,
    CapabilityRequirement,
    ClientIdentity,
    ErrorResponseEnvelope,
    MutationPrecondition,
    RequestEnvelope,
    RequestMetadata,
    decode_request,
    encode_request,
    encode_response,
)

WORKSPACE_ID = m1.WORKSPACE_ID
PRINCIPAL = "local-user"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")

_DEFINITION: dict[str, Any] = {
    "id": "core.ticket_priority",
    "version": "1.0.0",
    "title": "Ticket priority",
    "purpose": "decision_evaluation",
    "kind": "choice",
    "options": [
        {"id": "low", "label": "Low", "description": "low"},
        {"id": "high", "label": "High", "description": "high"},
    ],
    "recipe": {
        "mode": "deterministic",
        "rules": [
            {"when": {"key": "severity", "equals": "critical"}, "option": "high"},
            {"when": {"key": "severity", "equals": "minor"}, "option": "low"},
        ],
    },
    "required_sources": 0,
    "min_source_count": 0,
}

_MODEL_DEFINITION: dict[str, Any] = {
    "id": "core.sentiment_model",
    "version": "1.0.0",
    "title": "Sentiment (model)",
    "purpose": "decision_evaluation",
    "kind": "choice",
    "options": [
        {"id": "positive", "label": "Positive", "description": "positive"},
        {"id": "negative", "label": "Negative", "description": "negative"},
    ],
    "recipe": {"mode": "model", "rules": []},
    "required_sources": 0,
    "min_source_count": 0,
}

_ENTRY = {entry.name: entry for entry in OPERATION_CATALOGUE}


def _purpose(operation: str) -> str:
    from omnivia_core_runtime.service.application import DECISION_FAMILY_PURPOSES

    return DECISION_FAMILY_PURPOSES[operation]


def _request(
    operation: str, payload: dict[str, Any], **overrides: Any
) -> RequestEnvelope:
    entry = _ENTRY[operation]
    fields: dict[str, Any] = {
        "request_id": "req-decision-1",
        "correlation_id": "corr-decision-1",
        "trace_id": "trace-decision-1",
        "api_version": CONTRACT_VERSION,
        "client": CLIENT,
        "workspace_id": WORKSPACE_ID,
        "scopes": tuple(entry.scope.required_scopes),
        "purpose": _purpose(operation),
        "required_capabilities": (
            CapabilityRequirement(
                id=entry.required_capability.id,
                minimum_version=entry.required_capability.minimum_version,
                required=True,
            ),
        ),
        "idempotency_key": (
            "idem-decision-1" if entry.idempotency.supports_idempotency_key else None
        ),
        "mutation_precondition": (
            MutationPrecondition(record_version="rv-decision-1")
            if entry.precondition.supports_mutation_precondition
            else None
        ),
        "principal_claim": None,
    }
    fields.update(overrides)
    return RequestEnvelope(
        operation=operation,
        metadata=RequestMetadata(**fields),
        input=payload,
    )


@pytest.fixture
def environment(tmp_path: Path) -> Any:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    owned = take_ownership(path, workspace_id=WORKSPACE_ID)
    probe = Dispatcher.for_service_operations(
        Grant(
            principal=PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        owned,
    )
    started = SimpleNamespace(
        **vars(owned), workspace_id=WORKSPACE_ID, clock=SystemClock()
    )
    dispatcher = build_decision_application_dispatcher(
        service=started,
        principal_id=PRINCIPAL,
        installation_id="inst-decision-test-01",
        workspace_id=WORKSPACE_ID,
        fallback=probe,
    )
    yield SimpleNamespace(
        owned=owned,
        dispatcher=dispatcher,
        connection=owned.connection,
        started=started,
        probe=probe,
    )
    owned.connection.close()


def _dispatch(environment: Any, envelope: RequestEnvelope) -> Any:
    return environment.dispatcher.dispatch(envelope)


def _payload(response: Any) -> dict[str, Any]:
    from omnivia_core.contracts.v1 import SuccessResponseEnvelope

    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result is not None
    return dict(response.result)


def _error(response: Any) -> tuple[str, str]:
    from omnivia_core.contracts.v1 import ErrorResponseEnvelope

    assert isinstance(response, ErrorResponseEnvelope), response
    return str(response.error.code), str(response.error.message)


def _update_settings(environment: Any, revision: int, processing: str, key: str) -> Any:
    return _dispatch(
        environment,
        _request(
            "decision.settings.update",
            {
                "revision": revision,
                "processing": processing,
            },
            idempotency_key=key,
            mutation_precondition=MutationPrecondition(record_version=str(revision)),
        ),
    )


def _enable(environment: Any, revision: int = 0) -> int:
    payload = _payload(
        _update_settings(environment, revision, "advisory", f"enable-{revision}")
    )
    return int(payload["settings"]["revision"])


def _publish(environment: Any, document: dict[str, Any]) -> str:
    response = _dispatch(
        environment,
        _request(
            "decision.definition.publish",
            {"definition": document},
            idempotency_key=f"publish-{document['id']}-{document['version']}",
        ),
    )
    return str(_payload(response)["digest"])


def _evaluate(
    environment: Any,
    definition_id: str,
    version: str,
    state: dict[str, Any] | None,
    key: str = "idem-decision-1",
) -> Any:
    return _dispatch(
        environment,
        _request(
            "decision.evaluate",
            {
                "schema_version": "decision.1",
                "definition_ref": {"id": definition_id, "version": version},
                "subject_refs": [{"id": "document:1", "revision": "r1"}],
                "input": {
                    "source_refs": [],
                    "inline_state": state,
                },
                "execution": {
                    "mode": "advisory",
                    "privacy": "local_only",
                    "deadline_ms": 5000,
                    "maximum_provider_attempts": 1,
                },
            },
            idempotency_key=key,
        ),
    )


# --- the default state ---------------------------------------------------------


def test_the_default_capability_is_off_and_status_says_so(environment: Any) -> None:
    response = _dispatch(environment, _request("decision.status", {}))
    payload = _payload(response)
    assert payload["enabled"] is False
    assert payload["installed_profiles"] == 0
    assert payload["active_subscriptions"] == 0
    assert payload["host_engine_available"] is True


def test_an_evaluation_while_disabled_is_refused_without_records(
    environment: Any,
) -> None:
    response = _evaluate(environment, "core.ticket_priority", "1.0.0", {})
    code, _message = _error(response)
    assert code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    row = environment.connection.execute(
        "SELECT COUNT(*) FROM omnivia_decision_evaluations"
    ).fetchone()[0]
    assert row == 0


def test_settings_update_is_a_compare_and_swap(environment: Any) -> None:
    stale = _update_settings(environment, 7, "advisory", "enable-stale")
    code, _message = _error(stale)
    assert code == ERROR_CODE_MUTATION_PRECONDITION_FAILED
    revision = _enable(environment)
    assert revision == 1
    again = _dispatch(environment, _request("decision.settings.get", {}))
    assert _payload(again)["settings"]["processing"] == "advisory"


# --- the deterministic route -----------------------------------------------------


@pytest.fixture
def enabled_with_definition(environment: Any) -> Any:
    _enable(environment)
    digest = _publish(environment, _DEFINITION)
    return SimpleNamespace(environment=environment, digest=digest)


def test_a_deterministic_evaluation_commits_its_records_and_event(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    response = _evaluate(
        environment, "core.ticket_priority", "1.0.0", {"severity": "critical"}
    )
    payload = _payload(response)
    evaluation_id = payload["evaluation_id"]
    assert payload["schema_version"] == "decision.1"
    assert payload["job"]["identity"]["originating_operation"] == "decision.evaluate"

    record = _payload(
        _dispatch(
            environment,
            _request("decision.record.get", {"evaluation_id": evaluation_id}),
        )
    )["record"]
    assert record["status"] == "succeeded"
    assert record["definition_ref"] == {
        "id": "core.ticket_priority",
        "version": "1.0.0",
    }
    assert record["prediction"]["selected_option_id"] == "high"
    assert record["prediction"]["probability_semantics"] == "deterministic_rule"
    assert record["disposition"]["code"] == "advisory_only"
    assert record["disposition"]["authorises_action"] is False
    assert record["execution"]["provider_forward_passes"] == 0

    events = environment.connection.execute(
        "SELECT event_kind FROM omnivia_decision_outbox WHERE workspace_id = ? "
        "AND aggregate_id = ? ORDER BY sequence",
        (WORKSPACE_ID, evaluation_id),
    ).fetchall()
    assert [str(row[0]) for row in events] == ["decision.completed.v1"]

    job_state = environment.connection.execute(
        "SELECT state FROM omnivia_durable_jobs WHERE job_id = ?",
        (payload["job"]["identity"]["job_id"],),
    ).fetchone()[0]
    assert job_state == "succeeded"


def test_an_idempotent_replay_returns_the_same_evaluation(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    first = _payload(
        _evaluate(
            environment, "core.ticket_priority", "1.0.0", {"severity": "critical"}
        )
    )
    second = _payload(
        _evaluate(
            environment,
            "core.ticket_priority",
            "1.0.0",
            {"severity": "critical"},
            key="idem-decision-1",
        )
    )
    assert second["evaluation_id"] == first["evaluation_id"]
    count = environment.connection.execute(
        "SELECT COUNT(*) FROM omnivia_decision_evaluations WHERE workspace_id = ?",
        (WORKSPACE_ID,),
    ).fetchone()[0]
    assert count == 1


def test_a_reused_key_with_a_changed_request_is_a_conflict(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    _evaluate(environment, "core.ticket_priority", "1.0.0", {"severity": "critical"})
    conflict = _evaluate(
        environment,
        "core.ticket_priority",
        "1.0.0",
        {"severity": "minor"},
        key="idem-decision-1",
    )
    code, _message = _error(conflict)
    assert code in {ERROR_CODE_IDEMPOTENCY_CONFLICT, ERROR_CODE_CONFLICT}


def test_an_unmatched_state_abstains_and_does_not_guess(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    response = _evaluate(
        environment, "core.ticket_priority", "1.0.0", {"severity": "odd"}
    )
    payload = _payload(response)
    record = _payload(
        _dispatch(
            environment,
            _request(
                "decision.record.get", {"evaluation_id": payload["evaluation_id"]}
            ),
        )
    )["record"]
    assert record["status"] == "abstained"
    assert record["abstention_reasons"] == ["EVIDENCE_INCOMPLETE"]
    assert "prediction" not in record
    events = environment.connection.execute(
        "SELECT event_kind FROM omnivia_decision_outbox WHERE workspace_id = ? "
        "AND aggregate_id = ?",
        (WORKSPACE_ID, payload["evaluation_id"]),
    ).fetchall()
    assert [str(row[0]) for row in events] == ["decision.abstained.v1"]


def test_a_model_route_fails_closed_and_records_the_attempt(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    _publish(environment, _MODEL_DEFINITION)
    response = _evaluate(environment, "core.sentiment_model", "1.0.0", {"text": "x"})
    payload = _payload(response)
    record = _payload(
        _dispatch(
            environment,
            _request(
                "decision.record.get", {"evaluation_id": payload["evaluation_id"]}
            ),
        )
    )["record"]
    assert record["status"] == "failed"
    assert record["disposition"]["reason_codes"] == ["MODEL_NOT_INSTALLED"]
    assert record["disposition"]["authorises_action"] is False
    attempt = environment.connection.execute(
        "SELECT route, status, failure_code FROM omnivia_decision_attempts "
        "WHERE workspace_id = ? AND evaluation_id = ?",
        (WORKSPACE_ID, payload["evaluation_id"]),
    ).fetchone()
    assert tuple(attempt) == ("local_model", "failed", "model_not_installed")


def test_record_list_reports_the_newest_first_and_filters(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    first = _evaluate(
        environment,
        "core.ticket_priority",
        "1.0.0",
        {"severity": "critical"},
        key="k-1",
    )
    second = _evaluate(
        environment, "core.ticket_priority", "1.0.0", {"severity": "minor"}, key="k-2"
    )
    listed = _payload(_dispatch(environment, _request("decision.record.list", {})))[
        "records"
    ]
    assert [row["evaluation_id"] for row in listed] == [
        _payload(second)["evaluation_id"],
        _payload(first)["evaluation_id"],
    ]
    empty = _payload(
        _dispatch(
            environment,
            _request("decision.record.list", {"definition_id": "core.nope"}),
        )
    )
    assert empty["records"] == []


def test_an_unknown_record_is_refused_without_disclosure(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    response = _dispatch(
        environment,
        _request("decision.record.get", {"evaluation_id": "deval-nope"}),
    )
    code, _message = _error(response)
    assert code == ERROR_CODE_NOT_FOUND


# --- definitions -----------------------------------------------------------------


def test_a_definition_version_is_immutable(enabled_with_definition: Any) -> None:
    environment = enabled_with_definition.environment
    response = _dispatch(
        environment,
        _request(
            "decision.definition.publish",
            {"definition": dict(_DEFINITION, title="Changed")},
            idempotency_key="publish-republish",
        ),
    )
    code, _message = _error(response)
    assert code == ERROR_CODE_CONFLICT
    fetched = _payload(
        _dispatch(
            environment,
            _request(
                "decision.definition.get",
                {
                    "definition_ref": {
                        "id": "core.ticket_priority",
                        "version": "1.0.0",
                    }
                },
            ),
        )
    )
    assert fetched["definition"]["title"] == "Ticket priority"


def test_disabling_a_definition_stops_new_admissions_only(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    first = _evaluate(
        environment, "core.ticket_priority", "1.0.0", {"severity": "critical"}
    )
    disabled = _dispatch(
        environment,
        _request(
            "decision.definition.disable",
            {
                "definition_ref": {
                    "id": "core.ticket_priority",
                    "version": "1.0.0",
                }
            },
            idempotency_key="disable-1",
        ),
    )
    assert _payload(disabled)["enabled"] is False
    history = _payload(
        _dispatch(
            environment,
            _request(
                "decision.record.get",
                {"evaluation_id": _payload(first)["evaluation_id"]},
            ),
        )
    )
    assert history["record"]["status"] == "succeeded"
    refused = _evaluate(
        environment,
        "core.ticket_priority",
        "1.0.0",
        {"severity": "minor"},
        key="k-after-disable",
    )
    code, _message = _error(refused)
    assert code == ERROR_CODE_NOT_FOUND


# --- outcomes ----------------------------------------------------------------------


def test_an_outcome_is_appended_with_provenance_and_can_supersede(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    evaluation = _payload(
        _evaluate(
            environment, "core.ticket_priority", "1.0.0", {"severity": "critical"}
        )
    )["evaluation_id"]
    first = _payload(
        _dispatch(
            environment,
            _request(
                "decision.outcome.submit",
                {"evaluation_id": evaluation, "outcome": "confirmed"},
                idempotency_key="outcome-1",
            ),
        )
    )
    second = _payload(
        _dispatch(
            environment,
            _request(
                "decision.outcome.submit",
                {
                    "evaluation_id": evaluation,
                    "outcome": "corrected",
                    "corrected_option_id": "low",
                    "note": "downgraded after review",
                },
                idempotency_key="outcome-2",
            ),
        )
    )
    assert first["evaluation_id"] == evaluation
    assert second["evaluation_id"] == evaluation
    rows = environment.connection.execute(
        "SELECT outcome_id, superseded_outcome_id FROM omnivia_decision_outcomes "
        "WHERE workspace_id = ? ORDER BY recorded_at_us",
        (WORKSPACE_ID,),
    ).fetchall()
    assert rows[1][1] is None or rows[1][1] != rows[0][0]
    events = environment.connection.execute(
        "SELECT event_kind FROM omnivia_decision_outbox WHERE workspace_id = ? "
        "AND aggregate_id = ? ORDER BY sequence",
        (WORKSPACE_ID, evaluation),
    ).fetchall()
    kinds = [str(row[0]) for row in events]
    assert "decision.outcome_recorded.v1" in kinds


def test_an_outcome_for_an_unknown_evaluation_is_refused(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    response = _dispatch(
        environment,
        _request(
            "decision.outcome.submit",
            {"evaluation_id": "deval-nope", "outcome": "confirmed"},
            idempotency_key="outcome-nope",
        ),
    )
    code, _message = _error(response)
    assert code == ERROR_CODE_NOT_FOUND


# --- the kill switch ----------------------------------------------------------------


def test_disabling_the_capability_stops_admissions_and_keeps_history(
    enabled_with_definition: Any,
) -> None:
    environment = enabled_with_definition.environment
    kept = _evaluate(
        environment, "core.ticket_priority", "1.0.0", {"severity": "critical"}
    )
    current = _dispatch(environment, _request("decision.settings.get", {}))
    revision = int(_payload(current)["settings"]["revision"])
    _update_settings(environment, revision, "off", "disable-capability")
    refused = _evaluate(
        environment,
        "core.ticket_priority",
        "1.0.0",
        {"severity": "minor"},
        key="k-after-off",
    )
    code, _message = _error(refused)
    assert code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    history = _payload(
        _dispatch(
            environment,
            _request(
                "decision.record.get",
                {"evaluation_id": _payload(kept)["evaluation_id"]},
            ),
        )
    )
    assert history["record"]["status"] == "succeeded"


def test_the_decision_surface_is_exactly_the_sixteen_catalogue_operations() -> None:
    from omnivia_core_runtime.service.application import (
        DECISION_FAMILY_OPERATIONS,
    )

    assert len(DECISION_FAMILY_OPERATIONS) == 16
    assert DECISION_FAMILY_OPERATIONS == frozenset(
        entry.name
        for entry in OPERATION_CATALOGUE
        if entry.name.startswith("decision.")
    )


# --- the result-use gate --------------------------------------------------------------

_RESULT_USE = "decision.result_use.evaluate"
_SCHEMA_DIR = (
    Path(__file__).resolve().parents[5] / "contracts" / "application" / "v1" / "schemas"
)
_RESULT_USE_RESULT = Draft202012Validator(
    {"$ref": _ENTRY[_RESULT_USE].result_schema_ref},
    registry=Registry().with_resources(
        (document["$id"], Resource.from_contents(document))
        for document in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(_SCHEMA_DIR.glob("*.schema.json"))
        )
    ),
    format_checker=Draft202012Validator.FORMAT_CHECKER,
)
#: The injected wall time: ten hours east of UTC, with microseconds.
_WALL = datetime(2026, 10, 4, 21, 30, 15, 123456, tzinfo=timezone(timedelta(hours=10)))
_WALL_WIRE = "2026-10-04T11:30:15.123456Z"
_INVALID = "the result-use request payload is invalid"


class _CountingClock(FakeClock):
    """A fixed wall clock that counts its readings."""

    def __init__(self, wall: datetime) -> None:
        super().__init__(wall=wall)
        self.reads = 0

    def wall_time(self) -> datetime:
        self.reads += 1
        return super().wall_time()


def _gate(environment: Any, clock: Any) -> Any:
    """The decision family's dispatcher, composed as `serve` composes it, on `clock`."""
    return build_decision_application_dispatcher(
        service=environment.started,
        principal_id=PRINCIPAL,
        installation_id="inst-decision-test-01",
        workspace_id=WORKSPACE_ID,
        fallback=environment.probe,
        clock=clock,
    )


def _result_use_input(**overrides: Any) -> dict[str, Any]:
    return {
        "request_version": "1.0",
        "use_class": "current_publication",
        "subject_digest": "result-digest-1",
        "completeness": "complete",
        "continuity": "verified",
        "freshness_ok": True,
        "schema_compatible": True,
        "evidence_available": True,
        "policy_permits_partial_or_stale": False,
        "authority_epoch": "epoch-1",
        **overrides,
    }


def test_result_use_answers_the_bare_result_at_the_injected_instant(
    environment: Any,
) -> None:
    clock = _CountingClock(_WALL)
    writes = environment.connection.total_changes
    response = _gate(environment, clock).dispatch(
        _request(_RESULT_USE, _result_use_input())
    )
    payload = _payload(response)
    assert payload == {
        "outcome": "allow",
        "reasons": [],
        "subject_digest": "result-digest-1",
        "authority_epoch": "epoch-1",
        "valid_until": _WALL_WIRE,
    }
    assert "decision" not in payload
    assert not list(_RESULT_USE_RESULT.iter_errors(payload))
    assert not list(_RESULT_USE_RESULT.iter_errors(encode_response(response)["result"]))
    # One reading, and that exact instant -- microseconds included -- serialized.
    assert clock.reads == 1
    assert datetime.fromisoformat(payload["valid_until"]) == _WALL
    # Evaluation grants and records nothing.
    assert environment.connection.total_changes == writes


def test_result_use_accepts_the_read_only_input_a_wire_transport_delivers(
    environment: Any,
) -> None:
    envelope = decode_request(
        encode_request(
            _request(_RESULT_USE, _result_use_input(use_class="exploration"))
        )
    )
    assert isinstance(envelope.input, MappingProxyType)
    payload = _payload(_gate(environment, FakeClock(wall=_WALL)).dispatch(envelope))
    assert (payload["outcome"], payload["reasons"]) == (
        "allow_with_warning",
        ["exploration_non_certifying"],
    )
    assert not list(_RESULT_USE_RESULT.iter_errors(payload))


@pytest.mark.parametrize(
    ("version", "code", "message"),
    [
        ("1.0.0", ERROR_CODE_INVALID_REQUEST, _INVALID),
        (
            "2.0",
            ERROR_CODE_INCOMPATIBLE_VERSION,
            "the result-use request payload major version is incompatible",
        ),
        (
            "1.7",
            ERROR_CODE_UNSUPPORTED_MINOR_VERSION,
            "the result-use request payload minor version is unsupported",
        ),
    ],
    ids=["malformed", "incompatible-major", "unsupported-minor"],
)
def test_result_use_version_refusals_stay_distinct_and_non_retryable(
    environment: Any, version: str, code: str, message: str
) -> None:
    clock = _CountingClock(_WALL)
    response = _gate(environment, clock).dispatch(
        _request(_RESULT_USE, _result_use_input(request_version=version))
    )
    assert isinstance(response, ErrorResponseEnvelope)
    assert (
        response.error.code,
        response.error.message,
        response.error.retry_class,
    ) == (code, message, "non_retryable")
    assert clock.reads == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"freshness_ok": "false"},
        {"schema_compatible": "true"},
        {"evidence_available": 1},
        {"policy_permits_partial_or_stale": 0},
        {"freshness_ok": None},
        {"use_class": "marker-use-class"},
        {"completeness": "marker-completeness"},
        {"continuity": ["marker-continuity"]},
    ],
)
def test_result_use_invalid_booleans_and_enums_are_fixed_message_invalid_request(
    environment: Any, overrides: dict[str, Any]
) -> None:
    response = _gate(environment, FakeClock(wall=_WALL)).dispatch(
        _request(_RESULT_USE, _result_use_input(**overrides))
    )
    assert isinstance(response, ErrorResponseEnvelope)
    assert (
        response.error.code,
        response.error.message,
        response.error.retry_class,
    ) == (ERROR_CODE_INVALID_REQUEST, _INVALID, "non_retryable")
    assert "marker" not in json.dumps(encode_response(response))


def test_result_use_naive_clock_is_a_visible_wiring_defect(environment: Any) -> None:
    gate = _gate(
        environment,
        FakeClock(wall=datetime(2026, 10, 4, 11, 30)),  # noqa: DTZ001 - naive is the case under test
    )
    for payload in (
        _result_use_input(),
        _result_use_input(request_version="1.7"),
        _result_use_input(freshness_ok="false"),
    ):
        with pytest.raises(TypeError):
            gate.dispatch(_request(_RESULT_USE, payload))


@pytest.mark.parametrize("defect", [ValueError, TypeError, RuntimeError])
def test_result_use_evaluator_defects_are_not_request_refusals(
    environment: Any, monkeypatch: pytest.MonkeyPatch, defect: type[Exception]
) -> None:
    def broken(document: object, *, evaluation_instant: datetime) -> dict[str, Any]:
        raise defect("evaluator defect")

    monkeypatch.setattr(decisions, "evaluate_result_use", broken)
    with pytest.raises(defect, match="evaluator defect") as raised:
        _gate(environment, FakeClock(wall=_WALL)).dispatch(
            _request(_RESULT_USE, _result_use_input())
        )
    assert type(raised.value) is defect


def test_result_use_handlers_with_different_clocks_stay_isolated(
    environment: Any,
) -> None:
    early = _CountingClock(datetime(2026, 1, 1, tzinfo=UTC))
    late = _CountingClock(datetime(2027, 6, 30, 23, 59, 59, 999999, tzinfo=UTC))
    first, second = _gate(environment, early), _gate(environment, late)
    request = _request(_RESULT_USE, _result_use_input())
    observed = [
        _payload(gate.dispatch(request))["valid_until"]
        for gate in (first, second, first)
    ]
    assert observed == [
        "2026-01-01T00:00:00Z",
        "2027-06-30T23:59:59.999999Z",
        "2026-01-01T00:00:00Z",
    ]
    assert (early.reads, late.reads) == (2, 1)
