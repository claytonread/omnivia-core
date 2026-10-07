"""DEV-REQ-159 and DEV-REQ-008: task-context exports and outcome requests over the installed-MCP path.

The production surface is driven here the way an installed host reaches it: a bearer credential, provisioned by
`InstalledMcpAuthority.configure`, is resolved on every call by `OwnedInstalledMcp` through
`AuthenticatedApplicationDispatch`, and the request crosses a real local socket. The principal, the grant and the
workspace are the server's. These tests check what the socket adds to `test_task_context_production.py`: that an
authoring setup reaches the four operations, that a restricted setup does not, that the exporting principal is the
authenticated bearer, that a payload cannot name one, and that replays, stale fences, foreign workspaces and
tampered rows behave the same over the wire as in process.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_dev_req_081_knowledge_sharing as c16
import test_knowledge_project_authority as kpa
import test_task_context_outcomes as tc
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.service.application import TASK_CONTEXT_FAMILY_PURPOSES
from omnivia_core_runtime.service.handlers.task_context import (
    OPERATION_EXPORT,
    OPERATION_EXPORT_READ,
    OPERATION_OUTCOME_CREATE,
    OPERATION_OUTCOME_READ,
)
from omnivia_core_runtime.service.knowledge_sharing import NO_PROJECTS
from omnivia_core_runtime.storage.installation_store import McpHost, McpProfile

from omnivia_core.contracts.v1 import RequestEnvelope, get_operation_metadata

WS = c16.WS
OTHER_WORKSPACE = "ws-elsewhere"
TOKEN_BUDGET = 4_000_000
BYTE_BUDGET = 1_048_576
AUTHORING = "claude-code"
PEER = "codex"
_REQUESTS = itertools.count(1)

owned = c16.owned


def _envelope(
    operation: str, payload: Mapping[str, Any], *, workspace: str = WS, **metadata: Any
) -> RequestEnvelope:
    entry = get_operation_metadata(operation)
    request_id = f"req-tci-{next(_REQUESTS)}"
    overrides: dict[str, Any] = {
        "request_id": request_id,
        "correlation_id": f"cor-{request_id}",
        "trace_id": f"trc-{request_id}",
        "purpose": TASK_CONTEXT_FAMILY_PURPOSES[operation],
        "workspace_id": workspace,
    }
    if entry.idempotency.supports_idempotency_key:
        overrides["idempotency_key"] = metadata.pop("key", None) or f"idem-{request_id}"
    overrides.update(metadata)
    return s0.envelope_for(entry, operation_input=dict(payload), **overrides)


def _export_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "handoff": tc.handoff(),
        "token_budget": TOKEN_BUDGET,
        "byte_budget": BYTE_BUDGET,
    }
    payload.update(overrides)
    return payload


def _count(holder: Any, table: str) -> int:
    return int(holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.fixture
def installed(tmp_path: Path) -> Iterator[kpa._Installation]:
    """Two authoring installed setups, one per host, both bound to `WS`."""
    with kpa._installation_with(
        tmp_path,
        (McpHost.CLAUDE_CODE, McpProfile.AUTHORING),
        (McpHost.CODEX, McpProfile.AUTHORING),
    ) as setups:
        yield setups


def _bearer(setups: kpa._Installation, name: str) -> str:
    return setups.bearers[name]


def _principal(setups: kpa._Installation, name: str) -> str:
    return setups.principals[name]


def _call(
    endpoint: Any,
    operation: str,
    payload: Mapping[str, Any],
    *,
    credential: str,
    **metadata: Any,
) -> Any:
    return kpa._send(
        endpoint, _envelope(operation, payload, **metadata), credential=credential
    )


def _export_over(
    endpoint: Any, credential: str, payload: Mapping[str, Any] | None = None, **kw: Any
) -> dict[str, Any]:
    return kpa._result(
        _call(
            endpoint,
            OPERATION_EXPORT,
            _export_payload() if payload is None else payload,
            credential=credential,
            **kw,
        )
    )


# -- exposure and attribution -----------------------------------------------------------


def test_an_authoring_setup_exports_and_reads_over_the_socket(
    owned: m1.Owned, installed: kpa._Installation, tmp_path: Path
) -> None:
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        created = _export_over(endpoint, _bearer(installed, AUTHORING))
        read = kpa._result(
            _call(
                endpoint,
                OPERATION_EXPORT_READ,
                {"export_id": created["export_id"]},
                credential=_bearer(installed, AUTHORING),
            )
        )

    assert created["export_id"].startswith("tcx-")
    assert read == created
    assert _count(owned, "omnivia_task_context_exports") == 1


def test_the_exporting_principal_is_the_authenticated_bearer(
    owned: m1.Owned, installed: kpa._Installation
) -> None:
    """A payload cannot say who exported, and a second authoring principal reads what the first one wrote."""
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        created = _export_over(endpoint, _bearer(installed, AUTHORING))
        peer_read = kpa._result(
            _call(
                endpoint,
                OPERATION_EXPORT_READ,
                {"export_id": created["export_id"]},
                credential=_bearer(installed, PEER),
            )
        )

    assert created["exported_by"] == _principal(installed, AUTHORING)
    assert peer_read["exported_by"] == _principal(installed, AUTHORING)


def test_an_outcome_request_is_attributed_to_the_authenticated_bearer(
    owned: m1.Owned, installed: kpa._Installation
) -> None:
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        export = _export_over(endpoint, _bearer(installed, AUTHORING))
        outcome = kpa._result(
            _call(
                endpoint,
                OPERATION_OUTCOME_CREATE,
                {
                    "objective": "Summarise the open risks",
                    "export_id": export["export_id"],
                },
                credential=_bearer(installed, PEER),
            )
        )

    assert outcome["outcome_request_id"].startswith("outreq-")
    assert outcome["requested_by"] == _principal(installed, PEER)
    assert outcome["export_id"] == export["export_id"]


def test_a_restricted_setup_holds_none_of_the_four_operations(
    owned: m1.Owned, installed: kpa._Installation, tmp_path: Path
) -> None:
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        export = _export_over(endpoint, _bearer(installed, AUTHORING))
    with kpa._installation_with(
        tmp_path / "restricted", (McpHost.CODEX, McpProfile.RESTRICTED)
    ) as restricted:
        credential = _bearer(restricted, PEER)
        with kpa._socket(owned, NO_PROJECTS, restricted) as endpoint:
            refusals = [
                _call(
                    endpoint, OPERATION_EXPORT, _export_payload(), credential=credential
                ),
                _call(
                    endpoint,
                    OPERATION_EXPORT_READ,
                    {"export_id": export["export_id"]},
                    credential=credential,
                ),
                _call(
                    endpoint,
                    OPERATION_OUTCOME_CREATE,
                    {"objective": "Summarise", "export_id": export["export_id"]},
                    credential=credential,
                ),
                _call(
                    endpoint,
                    OPERATION_OUTCOME_READ,
                    {"outcome_request_id": "outreq-" + "a" * 64},
                    credential=credential,
                ),
            ]

    assert [kpa._code(refusal) for refusal in refusals] == ["authorization_denied"] * 4
    assert _count(owned, "omnivia_task_context_exports") == 1
    assert _count(owned, "omnivia_outcome_requests") == 0


# -- replay and conflict ----------------------------------------------------------------


def test_a_replay_under_the_same_key_answers_from_the_settled_outcome(
    owned: m1.Owned, installed: kpa._Installation
) -> None:
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        first = _export_over(endpoint, _bearer(installed, AUTHORING), key="tci-replay")
        second = _export_over(endpoint, _bearer(installed, AUTHORING), key="tci-replay")

    assert second == first
    assert _count(owned, "omnivia_task_context_exports") == 1


def test_the_same_key_for_a_different_handoff_is_an_idempotency_conflict(
    owned: m1.Owned, installed: kpa._Installation
) -> None:
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        _export_over(endpoint, _bearer(installed, AUTHORING), key="tci-conflict")
        other = _export_payload(handoff=tc.handoff(objective="A different objective"))
        refusal = _call(
            endpoint,
            OPERATION_EXPORT,
            other,
            credential=_bearer(installed, AUTHORING),
            key="tci-conflict",
        )

    assert kpa._code(refusal) == "idempotency_conflict"
    assert _count(owned, "omnivia_task_context_exports") == 1


def test_an_outcome_replay_and_an_objective_conflict_over_the_socket(
    owned: m1.Owned, installed: kpa._Installation
) -> None:
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        export = _export_over(endpoint, _bearer(installed, AUTHORING))
        payload = {
            "objective": "Summarise the open risks",
            "export_id": export["export_id"],
        }
        first = kpa._result(
            _call(
                endpoint,
                OPERATION_OUTCOME_CREATE,
                payload,
                credential=_bearer(installed, AUTHORING),
                key="tci-outcome",
            )
        )
        again = kpa._result(
            _call(
                endpoint,
                OPERATION_OUTCOME_CREATE,
                payload,
                credential=_bearer(installed, AUTHORING),
                key="tci-outcome",
            )
        )
        conflict = _call(
            endpoint,
            OPERATION_OUTCOME_CREATE,
            {**payload, "objective": "Something else"},
            credential=_bearer(installed, AUTHORING),
            key="tci-outcome",
        )

    assert again == first
    assert kpa._code(conflict) == "idempotency_conflict"
    assert _count(owned, "omnivia_outcome_requests") == 1


# -- unknown keys and workspace hiding --------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        pytest.param(
            OPERATION_EXPORT,
            {"principal": "owner-other"},
            id="export-principal",
        ),
        pytest.param(
            OPERATION_EXPORT,
            {"policy_digest": "sha256:" + "0" * 64},
            id="export-policy",
        ),
        pytest.param(
            OPERATION_EXPORT,
            {"fencing_generation": 99},
            id="export-fence",
        ),
        pytest.param(
            OPERATION_OUTCOME_CREATE,
            {"requested_by": "mallory"},
            id="outcome-principal",
        ),
    ],
)
def test_a_payload_naming_an_authority_field_is_refused_over_the_socket(
    owned: m1.Owned,
    installed: kpa._Installation,
    operation: str,
    payload: dict[str, Any],
) -> None:
    body = (
        _export_payload(**payload)
        if operation == OPERATION_EXPORT
        else {
            "objective": "Summarise",
            "export_id": "tcx-" + "0" * 64,
            **payload,
        }
    )
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        refusal = _call(
            endpoint, operation, body, credential=_bearer(installed, AUTHORING)
        )

    assert kpa._code(refusal) == "invalid_request"
    assert _count(owned, "omnivia_task_context_exports") == 0
    assert _count(owned, "omnivia_outcome_requests") == 0


def test_an_export_is_hidden_from_another_workspace_over_the_socket(
    owned: m1.Owned, installed: kpa._Installation
) -> None:
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        export = _export_over(endpoint, _bearer(installed, AUTHORING))
        refusal = _call(
            endpoint,
            OPERATION_EXPORT_READ,
            {"export_id": export["export_id"]},
            credential=_bearer(installed, AUTHORING),
            workspace=OTHER_WORKSPACE,
        )

    assert kpa._code(refusal) == "workspace_not_granted"
    assert export["export_id"] not in str(refusal)


def test_an_outcome_request_is_hidden_from_another_workspace_over_the_socket(
    owned: m1.Owned, installed: kpa._Installation
) -> None:
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        export = _export_over(endpoint, _bearer(installed, AUTHORING))
        outcome = kpa._result(
            _call(
                endpoint,
                OPERATION_OUTCOME_CREATE,
                {"objective": "Summarise", "export_id": export["export_id"]},
                credential=_bearer(installed, AUTHORING),
            )
        )
        refusal = _call(
            endpoint,
            OPERATION_OUTCOME_READ,
            {"outcome_request_id": outcome["outcome_request_id"]},
            credential=_bearer(installed, AUTHORING),
            workspace=OTHER_WORKSPACE,
        )

    assert kpa._code(refusal) == "workspace_not_granted"
    assert outcome["outcome_request_id"] not in str(refusal)


# -- stale fences and tampered rows -----------------------------------------------------


def test_an_outcome_for_an_export_from_an_earlier_fence_is_a_conflict_over_the_socket(
    owned: m1.Owned, installed: kpa._Installation
) -> None:
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        export = _export_over(endpoint, _bearer(installed, AUTHORING))
    owned.connection.close()
    takeover = m1.take_ownership(owned.path, workspace_id=WS)
    try:
        with kpa._socket(takeover, NO_PROJECTS, installed) as endpoint:
            refusal = _call(
                endpoint,
                OPERATION_OUTCOME_CREATE,
                {"objective": "Summarise", "export_id": export["export_id"]},
                credential=_bearer(installed, AUTHORING),
            )
            still_readable = kpa._result(
                _call(
                    endpoint,
                    OPERATION_EXPORT_READ,
                    {"export_id": export["export_id"]},
                    credential=_bearer(installed, AUTHORING),
                )
            )

        assert kpa._code(refusal) == "conflict"
        assert _count(takeover, "omnivia_outcome_requests") == 0
        assert still_readable["export_id"] == export["export_id"]
    finally:
        takeover.connection.close()


def test_an_export_row_altered_at_rest_is_an_internal_fault_over_the_socket(
    owned: m1.Owned, installed: kpa._Installation
) -> None:
    with kpa._socket(owned, NO_PROJECTS, installed) as endpoint:
        export = _export_over(endpoint, _bearer(installed, AUTHORING))
    path = owned.path
    owned.connection.close()
    raw = sqlite3.connect(path)
    try:
        raw.execute("DROP TRIGGER omnivia_guard_task_context_exports_update")
        raw.execute("UPDATE omnivia_task_context_exports SET exported_by = 'mallory'")
        raw.commit()
    finally:
        raw.close()

    takeover = m1.take_ownership(path, workspace_id=WS)
    try:
        with kpa._socket(takeover, NO_PROJECTS, installed) as endpoint:
            response = _call(
                endpoint,
                OPERATION_EXPORT_READ,
                {"export_id": export["export_id"]},
                credential=_bearer(installed, AUTHORING),
            )

        assert kpa._code(response) == "internal_non_recoverable"
        assert export["export_id"] not in str(response)
        assert "mallory" not in str(response)
    finally:
        takeover.connection.close()
