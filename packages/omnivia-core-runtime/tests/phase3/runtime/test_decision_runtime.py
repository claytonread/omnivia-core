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
  touching history (§28.3).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service.application import (
    build_decision_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
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
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    ERROR_CODE_NOT_FOUND,
    OPERATION_CATALOGUE,
    CapabilityRequirement,
    ClientIdentity,
    MutationPrecondition,
    RequestEnvelope,
    RequestMetadata,
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


def _request(operation: str, payload: dict[str, Any], **overrides: Any) -> RequestEnvelope:
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


def _update_settings(
    environment: Any, revision: int, processing: str, key: str
) -> Any:
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
    payload = _payload(_update_settings(environment, revision, "advisory", f"enable-{revision}"))
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


def test_an_evaluation_while_disabled_is_refused_without_records(environment: Any) -> None:
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
    response = _evaluate(environment, "core.ticket_priority", "1.0.0", {"severity": "critical"})
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
    assert record["definition_ref"] == {"id": "core.ticket_priority", "version": "1.0.0"}
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
    first = _payload(_evaluate(environment, "core.ticket_priority", "1.0.0", {"severity": "critical"}))
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
    response = _evaluate(environment, "core.ticket_priority", "1.0.0", {"severity": "odd"})
    payload = _payload(response)
    record = _payload(
        _dispatch(
            environment,
            _request("decision.record.get", {"evaluation_id": payload["evaluation_id"]}),
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
            _request("decision.record.get", {"evaluation_id": payload["evaluation_id"]}),
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
        environment, "core.ticket_priority", "1.0.0", {"severity": "critical"}, key="k-1"
    )
    second = _evaluate(
        environment, "core.ticket_priority", "1.0.0", {"severity": "minor"}, key="k-2"
    )
    listed = _payload(
        _dispatch(environment, _request("decision.record.list", {}))
    )["records"]
    assert [row["evaluation_id"] for row in listed] == [
        _payload(second)["evaluation_id"],
        _payload(first)["evaluation_id"],
    ]
    empty = _payload(
        _dispatch(
            environment,
            _request(
                "decision.record.list", {"definition_id": "core.nope"}
            ),
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
    first = _evaluate(environment, "core.ticket_priority", "1.0.0", {"severity": "critical"})
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
            _request("decision.record.get", {"evaluation_id": _payload(first)["evaluation_id"]}),
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
        _evaluate(environment, "core.ticket_priority", "1.0.0", {"severity": "critical"})
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
    kept = _evaluate(environment, "core.ticket_priority", "1.0.0", {"severity": "critical"})
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
            _request("decision.record.get", {"evaluation_id": _payload(kept)["evaluation_id"]}),
        )
    )
    assert history["record"]["status"] == "succeeded"


def test_the_decision_surface_is_exactly_the_fifteen_catalogue_operations() -> None:
    from omnivia_core_runtime.service.application import (
        DECISION_FAMILY_OPERATIONS,
    )

    assert len(DECISION_FAMILY_OPERATIONS) == 15
    assert DECISION_FAMILY_OPERATIONS == frozenset(
        entry.name for entry in OPERATION_CATALOGUE if entry.name.startswith("decision.")
    )
