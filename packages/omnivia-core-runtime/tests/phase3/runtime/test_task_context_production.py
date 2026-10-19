"""DEV-REQ-159 and DEV-REQ-008: task-context exports and outcome requests through the production surface.

Every behaviour below is driven through `ProductionApplicationSurface.dispatch_for_session`, composed by
`service.main` over a real migrated workspace, so the family session, the purposes, the grant, the fenced
write, the audit settlement and the wire results are the ones a served workspace meets. The domain rules
themselves are covered by `test_task_context_outcomes.py`; this module checks the authority and the
refusals around them.

The principal and the workspace are the session's and the envelope's, never a payload member. A payload that
names either, or a policy, is refused as an unknown key and writes nothing.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_dev_req_081_knowledge_sharing as c16
import test_task_context_outcomes as tc
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.service.application import TASK_CONTEXT_FAMILY_PURPOSES
from omnivia_core_runtime.service.handlers.task_context import (
    OPERATION_EXPORT,
    OPERATION_EXPORT_READ,
    OPERATION_OUTCOME_CREATE,
    OPERATION_OUTCOME_READ,
    TASK_CONTEXT_FAMILY_OPERATIONS,
)
from omnivia_core_runtime.service.mutation import MUTATION_PURPOSES, MUTATION_ROLES
from omnivia_core_runtime.storage.task_context import read_export

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)

WS = c16.WS
PRINCIPAL = c16.PRINCIPAL
OTHER_PRINCIPAL = "owner-other"
FOREIGN_WORKSPACE = "ws-elsewhere"
TOKEN_BUDGET = 4_000_000
BYTE_BUDGET = 1_048_576
OPERATIONS = tuple(sorted(TASK_CONTEXT_FAMILY_OPERATIONS))
_REQUESTS = iter(range(1, 10_000))


class Harness:
    """A migrated workspace behind the production surface, called as one principal at a time."""

    def __init__(self, holder: m1.Owned) -> None:
        self.holder = holder
        self.surface = c16._surface(holder, None)

    def session(self, principal: str, *operations: str) -> Any:
        """A server-shaped session for `principal`; only the principal and the grant differ."""
        base = self.surface.session_for(OPERATION_EXPORT)
        assert base is not None
        return dataclasses.replace(
            base,
            principal_id=principal,
            operations=frozenset(operations or OPERATIONS),
        )

    def call(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        principal: str = PRINCIPAL,
        key: str | None = None,
        session: Any = None,
        **metadata: Any,
    ) -> Any:
        request_id = f"req-tc-{next(_REQUESTS)}"
        entry = get_operation_metadata(operation)
        overrides: dict[str, Any] = {
            "request_id": request_id,
            "correlation_id": f"cor-{request_id}",
            "trace_id": f"trc-{request_id}",
            "purpose": TASK_CONTEXT_FAMILY_PURPOSES[operation],
            "workspace_id": WS,
        }
        if entry.idempotency.supports_idempotency_key:
            overrides["idempotency_key"] = key or f"idem-{request_id}"
        overrides.update(metadata)
        envelope = s0.envelope_for(entry, operation_input=payload, **overrides)
        return self.surface.dispatch_for_session(
            envelope, session or self.session(principal, operation)
        )

    def ok(
        self, operation: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        response = self.call(operation, payload, **kwargs)
        assert isinstance(response, SuccessResponseEnvelope), response
        return dict(response.to_wire()["result"])

    def refused(
        self, operation: str, payload: dict[str, Any], **kwargs: Any
    ) -> ErrorResponseEnvelope:
        response = self.call(operation, payload, **kwargs)
        assert isinstance(response, ErrorResponseEnvelope), response
        return response

    def code(self, operation: str, payload: dict[str, Any], **kwargs: Any) -> str:
        return str(self.refused(operation, payload, **kwargs).error.code)

    def export(
        self, handoff: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self.ok(OPERATION_EXPORT, export_payload(handoff), **kwargs)

    def outcome(
        self, export_id: str, objective: str = "Summarise the open risks", **kwargs: Any
    ) -> dict[str, Any]:
        return self.ok(
            OPERATION_OUTCOME_CREATE,
            {"objective": objective, "export_id": export_id},
            **kwargs,
        )

    def count(self, table: str) -> int:
        return int(
            self.holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
                0
            ]
        )

    def audit_events(self) -> int:
        return int(
            self.holder.connection.execute(
                "SELECT COUNT(*) FROM omnivia_application_audit_events"
            ).fetchone()[0]
        )


def export_payload(
    handoff: dict[str, Any] | None = None, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "handoff": tc.handoff() if handoff is None else handoff,
        "token_budget": TOKEN_BUDGET,
        "byte_budget": BYTE_BUDGET,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    m1.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = m1.take_ownership(path, workspace_id=WS)
    yield holder
    holder.connection.close()


@pytest.fixture
def harness(owned: m1.Owned) -> Harness:
    return Harness(owned)


def restart(holder: m1.Owned) -> m1.Owned:
    """The same workspace after a takeover: the fencing generation moves on."""
    holder.connection.close()
    return m1.take_ownership(holder.path, workspace_id=WS)


# -- the family in the production surface -------------------------------------------------


def test_the_four_operations_are_served_by_the_production_surface(
    harness: Harness,
) -> None:
    assert set(TASK_CONTEXT_FAMILY_OPERATIONS) <= set(
        harness.surface.registry.operations
    )
    assert (
        TASK_CONTEXT_FAMILY_PURPOSES[OPERATION_EXPORT]
        == MUTATION_PURPOSES[OPERATION_EXPORT]
    )
    assert (
        TASK_CONTEXT_FAMILY_PURPOSES[OPERATION_OUTCOME_CREATE]
        == MUTATION_PURPOSES[OPERATION_OUTCOME_CREATE]
    )
    assert (
        TASK_CONTEXT_FAMILY_PURPOSES[OPERATION_EXPORT_READ]
        == TASK_CONTEXT_FAMILY_PURPOSES[OPERATION_OUTCOME_READ]
    )
    assert (
        MUTATION_ROLES[OPERATION_EXPORT]
        == MUTATION_ROLES[OPERATION_OUTCOME_CREATE]
        == ("workspace_contributor")
    )


def test_a_session_without_the_operation_is_refused_before_the_handler(
    harness: Harness,
) -> None:
    read_only = harness.session(PRINCIPAL, OPERATION_EXPORT_READ)
    assert harness.code(OPERATION_EXPORT, export_payload(), session=read_only) in {
        "authorization_denied",
        "capability_not_granted",
    }
    assert harness.count("omnivia_task_context_exports") == 0


# -- exports --------------------------------------------------------------------------------


def test_an_export_is_recorded_once_and_attributed_to_the_authenticated_principal(
    harness: Harness,
) -> None:
    audit_before = harness.audit_events()
    result = harness.export(principal=OTHER_PRINCIPAL)

    assert result["export_id"].startswith("tcx-")
    assert result["exported_by"] == OTHER_PRINCIPAL
    assert result["document"]["exportedBy"] == OTHER_PRINCIPAL
    assert result["document"]["workspaceId"] == WS
    assert result["fencing_generation"] == harness.holder.generation
    assert harness.count("omnivia_task_context_exports") == 1
    assert harness.audit_events() == audit_before + 1


def test_a_replay_under_the_same_key_returns_the_stored_export_without_a_second_write(
    harness: Harness,
) -> None:
    first = harness.export(key="replay-1")
    audit_after_first = harness.audit_events()
    second = harness.export(key="replay-1")

    assert second == first
    assert harness.count("omnivia_task_context_exports") == 1
    assert harness.audit_events() == audit_after_first


def test_the_same_key_for_a_different_handoff_is_an_idempotency_conflict(
    harness: Harness,
) -> None:
    harness.export(key="reuse-1")
    other = tc.handoff(objective="A different objective")

    assert (
        harness.code(OPERATION_EXPORT, export_payload(other), key="reuse-1")
        == "idempotency_conflict"
    )
    assert harness.count("omnivia_task_context_exports") == 1


def test_identical_content_under_two_keys_names_one_export(harness: Harness) -> None:
    first = harness.export(key="content-a")
    second = harness.export(key="content-b")

    assert second["export_id"] == first["export_id"]
    assert harness.count("omnivia_task_context_exports") == 1


def test_an_export_reads_back_by_identifier_and_matches_what_was_recorded(
    harness: Harness,
) -> None:
    created = harness.export()
    read = harness.ok(OPERATION_EXPORT_READ, {"export_id": created["export_id"]})

    assert read == created


def test_an_unknown_export_identifier_is_not_found_and_echoes_nothing(
    harness: Harness,
) -> None:
    unknown = "tcx-" + "0" * 64
    response = harness.refused(OPERATION_EXPORT_READ, {"export_id": unknown})

    assert response.error.code == "not_found"
    assert unknown not in str(response.error.message)


def test_a_foreign_workspace_export_is_hidden_from_every_read(harness: Harness) -> None:
    created = harness.export()
    assert (
        read_export(
            harness.holder.connection,
            workspace_id=FOREIGN_WORKSPACE,
            export_id=created["export_id"],
        )
        is None
    )

    response = harness.refused(
        OPERATION_EXPORT_READ,
        {"export_id": created["export_id"]},
        workspace_id=FOREIGN_WORKSPACE,
    )
    assert response.error.code in {"not_found", "workspace_not_granted"}
    assert created["export_id"] not in str(response.error.message)


def test_a_budget_the_export_cannot_fit_is_size_limited(harness: Harness) -> None:
    assert (
        harness.code(OPERATION_EXPORT, export_payload(byte_budget=1))
        == "size_limit_exceeded"
    )
    assert (
        harness.code(OPERATION_EXPORT, export_payload(token_budget=0))
        == "invalid_request"
    )
    assert harness.count("omnivia_task_context_exports") == 0


def test_a_handoff_that_fails_its_own_identity_or_shape_is_an_invalid_request(
    harness: Harness,
) -> None:
    altered = tc.handoff()
    altered["objective"] = "Changed after sealing"

    assert harness.code(OPERATION_EXPORT, export_payload(altered)) == "invalid_request"
    assert (
        harness.code(OPERATION_EXPORT, {"token_budget": 1, "byte_budget": 1})
        == "invalid_request"
    )
    assert harness.count("omnivia_task_context_exports") == 0


# -- outcome requests -----------------------------------------------------------------------


def test_an_outcome_request_names_its_export_and_records_its_objective_verbatim(
    harness: Harness,
) -> None:
    export = harness.export()
    outcome = harness.outcome(export["export_id"], principal=OTHER_PRINCIPAL)

    assert outcome["outcome_request_id"].startswith("outreq-")
    assert outcome["export_id"] == export["export_id"]
    assert outcome["source_handoff_identity"] == export["source_handoff_identity"]
    assert outcome["requested_by"] == OTHER_PRINCIPAL
    assert outcome["objective"] == "Summarise the open risks"
    assert outcome["status"] == "received"
    assert harness.count("omnivia_outcome_requests") == 1


def test_an_outcome_request_replays_under_its_key_and_conflicts_under_another_objective(
    harness: Harness,
) -> None:
    export = harness.export()
    first = harness.outcome(export["export_id"], key="outcome-replay")

    assert harness.outcome(export["export_id"], key="outcome-replay") == first
    assert harness.count("omnivia_outcome_requests") == 1
    assert (
        harness.code(
            OPERATION_OUTCOME_CREATE,
            {"objective": "A different objective", "export_id": export["export_id"]},
            key="outcome-replay",
        )
        == "idempotency_conflict"
    )


def test_an_outcome_request_for_an_export_the_workspace_does_not_hold_is_not_found(
    harness: Harness,
) -> None:
    unknown = "tcx-" + "1" * 64
    response = harness.refused(
        OPERATION_OUTCOME_CREATE, {"objective": "Anything", "export_id": unknown}
    )

    assert response.error.code == "not_found"
    assert unknown not in str(response.error.message)
    assert harness.count("omnivia_outcome_requests") == 0


@pytest.mark.parametrize(
    ("objective", "code"),
    [
        pytest.param("   ", "invalid_request", id="blank"),
        pytest.param("x" * 8193, "size_limit_exceeded", id="over-the-byte-bound"),
    ],
)
def test_an_unusable_objective_is_refused_and_writes_nothing(
    harness: Harness, objective: str, code: str
) -> None:
    export = harness.export()

    assert (
        harness.code(
            OPERATION_OUTCOME_CREATE,
            {"objective": objective, "export_id": export["export_id"]},
        )
        == code
    )
    assert harness.count("omnivia_outcome_requests") == 0


def test_an_outcome_request_reads_back_by_identifier(harness: Harness) -> None:
    export = harness.export()
    created = harness.outcome(export["export_id"])

    assert (
        harness.ok(
            OPERATION_OUTCOME_READ,
            {"outcome_request_id": created["outcome_request_id"]},
        )
        == created
    )
    unknown = "outreq-" + "2" * 64
    assert (
        harness.code(OPERATION_OUTCOME_READ, {"outcome_request_id": unknown})
        == "not_found"
    )


# -- caller-selected authority -------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        pytest.param(
            OPERATION_EXPORT,
            {**export_payload(), "exported_by": "mallory"},
            id="export-principal",
        ),
        pytest.param(
            OPERATION_EXPORT,
            {**export_payload(), "workspace_id": FOREIGN_WORKSPACE},
            id="export-workspace",
        ),
        pytest.param(
            OPERATION_EXPORT,
            {**export_payload(), "policy_digest": "e" * 64},
            id="export-policy",
        ),
        pytest.param(
            OPERATION_EXPORT,
            {**export_payload(), "fencing_generation": 99},
            id="export-fence",
        ),
        pytest.param(
            OPERATION_OUTCOME_CREATE,
            {
                "objective": "Anything",
                "export_id": "tcx-" + "a" * 64,
                "requested_by": "mallory",
            },
            id="outcome-principal",
        ),
        pytest.param(
            OPERATION_OUTCOME_CREATE,
            {
                "objective": "Anything",
                "export_id": "tcx-" + "a" * 64,
                "workspace_id": FOREIGN_WORKSPACE,
            },
            id="outcome-workspace",
        ),
        pytest.param(
            OPERATION_EXPORT_READ,
            {"export_id": "tcx-" + "a" * 64, "workspace_id": FOREIGN_WORKSPACE},
            id="export-read-workspace",
        ),
        pytest.param(
            OPERATION_OUTCOME_READ,
            {"outcome_request_id": "outreq-" + "a" * 64, "requested_by": "mallory"},
            id="outcome-read-principal",
        ),
    ],
)
def test_a_payload_naming_principal_workspace_policy_or_fence_is_refused_and_writes_nothing(
    harness: Harness, operation: str, payload: dict[str, Any]
) -> None:
    assert harness.code(operation, payload) == "invalid_request"
    assert harness.count("omnivia_task_context_exports") == 0
    assert harness.count("omnivia_outcome_requests") == 0


# -- stale fences, tampering and corruption -----------------------------------------------


def test_an_outcome_request_for_an_export_from_an_earlier_fence_is_a_conflict(
    harness: Harness,
) -> None:
    export = harness.export()
    takeover = restart(harness.holder)
    try:
        after = Harness(takeover)

        assert (
            after.code(
                OPERATION_OUTCOME_CREATE,
                {"objective": "Summarise", "export_id": export["export_id"]},
            )
            == "conflict"
        )
        assert after.count("omnivia_outcome_requests") == 0
        assert (
            after.ok(OPERATION_EXPORT_READ, {"export_id": export["export_id"]})[
                "export_id"
            ]
            == export["export_id"]
        )
    finally:
        takeover.connection.close()


def test_an_export_row_altered_at_rest_reads_as_a_fault_not_a_missing_export(
    harness: Harness,
) -> None:
    export = harness.export()
    path = harness.holder.path
    harness.holder.connection.close()
    raw = sqlite3.connect(path)
    try:
        raw.execute("DROP TRIGGER omnivia_guard_task_context_exports_update")
        raw.execute("UPDATE omnivia_task_context_exports SET exported_by = 'mallory'")
        raw.commit()
    finally:
        raw.close()

    takeover = m1.take_ownership(path, workspace_id=WS)
    try:
        response = Harness(takeover).refused(
            OPERATION_EXPORT_READ, {"export_id": export["export_id"]}
        )
        assert response.error.code == "internal_non_recoverable"
        assert export["export_id"] not in str(response.error.message)
    finally:
        takeover.connection.close()
