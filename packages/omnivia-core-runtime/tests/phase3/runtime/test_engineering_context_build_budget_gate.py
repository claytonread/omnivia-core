"""The pre-hydration budget and body-free frontier gate of `engineering.context.build`.

Every supplied budget field is validated -- a positive, non-bool integer at or
below its server ceiling -- before any frontier or body read, and the admitted
candidate count is checked against the effective hydration cap before any body
is hydrated: a build past that cap refuses as `size_limit_exceeded` and reads
no body column at all.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Self

import pytest
import test_engineering_source_coverage as esc
from omnivia_core_runtime.service.handlers import engineering as handlers

Workspace = esc.Workspace

#: The columns a body read touches; a statement naming one of these ran against
#: content or claim payload, never against the body-free frontier metadata.
BODY_COLUMNS = ("content_json", "claim_json")


class Trace:
    """Every SQL statement a block runs on a connection, as SQLite executes it."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.statements: list[str] = []

    def __enter__(self) -> Self:
        self.connection.set_trace_callback(self.statements.append)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.connection.set_trace_callback(None)

    def body_reads(self) -> list[str]:
        return [s for s in self.statements if any(m in s for m in BODY_COLUMNS)]

    def frontier_reads(self) -> list[str]:
        return [
            s
            for s in self.statements
            if "omnivia_authoritative_governed_version_metadata" in s
        ]


@pytest.fixture
def workspace(tmp_path: Any) -> Any:
    opened = Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _observe_many(workspace: Workspace, count: int, prefix: str = "Item") -> list[str]:
    return [
        workspace.observe(esc._observation(None, title=f"{prefix} {index}", evidence=False))[
            "record_id"
        ]
        for index in range(count)
    ]


def _build(workspace: Workspace, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "query": "item",
        "targets": [],
        "profile": "investigate",
    }
    payload.update(overrides)
    return workspace.call("engineering.context.build", payload)


# --- the hydration-cap gate ---------------------------------------------------------


def test_admitted_bodies_past_the_hydration_cap_refuse_before_any_body_query(
    workspace: Workspace,
) -> None:
    assert handlers.BUDGET_DEFAULT_HYDRATIONS == 8
    _observe_many(workspace, 9)
    connection = workspace.holder.connection
    with Trace(connection) as trace:
        code, _message, _retry = workspace.refused(
            "engineering.context.build", {"query": "item", "targets": [], "profile": "investigate"}
        )
    assert code == "size_limit_exceeded"
    assert trace.body_reads() == []


def test_exactly_the_hydration_cap_still_builds(workspace: Workspace) -> None:
    ids = _observe_many(workspace, 8)
    result = workspace.ok(
        "engineering.context.build",
        {"query": "item", "targets": [], "profile": "investigate"},
    )
    pack = result["pack"]
    assert pack["budget"]["hydrations"] == 8
    assert {c["record_ref"]["record_id"] for c in pack["citations"]} == set(ids)


def test_a_denied_version_is_never_counted_toward_the_hydration_cap(
    workspace: Workspace,
) -> None:
    """Nine records exist, one behind a label only the owner holds: the owner's
    own count exceeds the cap and refuses, but the label-blind reader admits
    only the other eight and builds cleanly."""
    import test_blobs_staged_sources_and_evidence_migration as m2

    m2.write(workspace.holder, m2.EVIDENCE, evidence_id="evd-open", source_native_id="doc-open")
    open_source = {**esc.EVIDENCE_SOURCE, "source_id": "doc-open"}
    for index in range(8):
        workspace.observe(
            esc._observation(
                None, title=f"Open item {index}", evidence=True, source=open_source
            )
        )
    workspace.observe(esc._observation(None, title="Hidden item", evidence=True))

    owner_code, _msg, _retry = workspace.refused(
        "engineering.context.build",
        {"query": "item", "targets": [], "profile": "investigate"},
    )
    assert owner_code == "size_limit_exceeded"

    reader = esc._reader()
    result = workspace.ok(
        "engineering.context.build",
        {"query": "item", "targets": [], "profile": "investigate"},
        session=reader,
    )
    assert result["pack"]["budget"]["hydrations"] == 8


def test_a_non_engineering_domain_body_is_never_hydrated_or_counted(
    workspace: Workspace,
) -> None:
    fact = {
        **esc._observation(None, title="A note", evidence=False),
        "record_type": "memory.fact",
        "domain_scope": "workspace.notes",
    }
    fact["content"] = {"fact": "a note lives in the wiki"}
    workspace.observe(fact)
    ids = _observe_many(workspace, 8, prefix="Item")

    result = workspace.ok(
        "engineering.context.build",
        {"query": "item", "targets": [], "profile": "investigate"},
    )
    pack = result["pack"]
    assert pack["budget"]["hydrations"] == 8
    assert {c["record_ref"]["record_id"] for c in pack["citations"]} == set(ids)


def test_a_successful_small_build_reports_the_exact_hydration_count(
    workspace: Workspace,
) -> None:
    _observe_many(workspace, 3)
    result = workspace.ok(
        "engineering.context.build",
        {"query": "item", "targets": [], "profile": "investigate"},
    )
    assert result["pack"]["budget"]["hydrations"] == 3


# --- the budget validation gate -----------------------------------------------------


@pytest.mark.parametrize(
    "budget",
    [
        {"model_tokens": 0},
        {"model_tokens": -1},
        {"model_tokens": True},
        {"model_tokens": 16001},
        {"model_bytes": 0},
        {"model_bytes": 65537},
        {"hydrations": 0},
        {"hydrations": 33},
        {"hydrations": False},
        {"evidence_bytes": 0},
        {"evidence_bytes": 1048577},
    ],
)
def test_an_invalid_budget_field_refuses_before_any_frontier_read(
    workspace: Workspace, budget: dict[str, Any]
) -> None:
    _observe_many(workspace, 1)
    connection = workspace.holder.connection
    with Trace(connection) as trace:
        code, _message, _retry = workspace.refused(
            "engineering.context.build",
            {"query": "item", "targets": [], "profile": "investigate", "budget": budget},
        )
    assert code == "invalid_request"
    assert trace.frontier_reads() == []
    assert trace.body_reads() == []


def test_valid_budgets_at_their_ceiling_are_accepted(workspace: Workspace) -> None:
    _observe_many(workspace, 1)
    result = workspace.ok(
        "engineering.context.build",
        {
            "query": "item",
            "targets": [],
            "profile": "investigate",
            "budget": {
                "model_tokens": 16000,
                "model_bytes": 65536,
                "hydrations": 32,
                "evidence_bytes": 1048576,
            },
        },
    )
    assert result["pack"]["budget"]["effective"] == {
        "model_tokens": 16000,
        "model_bytes": 65536,
        "hydrations": 32,
        "evidence_bytes": 1048576,
    }


def test_default_effective_budget_fields_are_reported(workspace: Workspace) -> None:
    _observe_many(workspace, 1)
    result = workspace.ok(
        "engineering.context.build",
        {"query": "item", "targets": [], "profile": "investigate"},
    )
    assert result["pack"]["budget"]["effective"] == {
        "model_tokens": handlers.BUDGET_DEFAULT_TOKENS,
        "model_bytes": handlers.BUDGET_DEFAULT_BYTES,
        "hydrations": handlers.BUDGET_DEFAULT_HYDRATIONS,
        "evidence_bytes": handlers.BUDGET_DEFAULT_EVIDENCE_BYTES,
    }


# --- current_safe coverage still refuses before the frontier ------------------------


def test_current_safe_coverage_refusal_still_precedes_the_frontier(
    workspace: Workspace,
) -> None:
    _observe_many(workspace, 1)
    connection = workspace.holder.connection
    with Trace(connection) as trace:
        code, _message, _retry = workspace.refused(
            "engineering.context.build",
            {
                "query": "item",
                "profile": "investigate",
                "applicability_mode": "current_safe",
                "targets": [{"snapshot_id": "esnap-missing", "repository_id": esc.REPOSITORY}],
            },
        )
    assert code == "dependency_unavailable"
    assert trace.frontier_reads() == []
    assert trace.body_reads() == []
