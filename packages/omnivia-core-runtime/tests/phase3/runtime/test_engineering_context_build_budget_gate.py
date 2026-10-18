"""The pre-hydration budget and preview-first gate of `engineering.context.build`.

Every supplied budget field is validated -- a positive, non-bool integer at or
below its server ceiling -- before any frontier or body read, and the admitted
candidate set is ranked from bounded projections before the selected bodies are
hydrated. Source payload lengths are checked before a payload SELECT.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Self

import pytest
import test_engineering_source_coverage as esc
from omnivia_core_runtime.service.handlers import engineering as handlers

Workspace = esc.Workspace

#: Exact payload projections. Length-only prechecks name the same columns but do
#: not return their contents to the application.
BODY_COLUMNS = ("content_json", "claim_json", "rationale_json")


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
        return [
            statement
            for statement in self.statements
            if any(column in statement for column in BODY_COLUMNS)
            and "octet_length(" not in statement
            and "length(CAST(" not in statement
        ]

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


def test_more_matches_than_the_hydration_cap_selects_without_over_hydrating(
    workspace: Workspace,
) -> None:
    assert handlers.BUDGET_DEFAULT_HYDRATIONS == 8
    _observe_many(workspace, 9)
    connection = workspace.holder.connection
    with Trace(connection) as trace:
        result = workspace.ok(
            "engineering.context.build", {"query": "item", "targets": [], "profile": "investigate"}
        )
    pack = result["pack"]
    assert pack["budget"]["hydrations"] == 8
    assert len(pack["citations"]) == 8
    assert {tuple(item.values()) for item in pack["omissions"]} >= {
        ("sections", "selection_limit")
    }
    assert len(trace.body_reads()) >= 1


def test_exactly_the_hydration_cap_still_builds(workspace: Workspace) -> None:
    ids = _observe_many(workspace, 8)
    result = workspace.ok(
        "engineering.context.build",
        {"query": "item", "targets": [], "profile": "investigate"},
    )
    pack = result["pack"]
    assert pack["budget"]["hydrations"] == 8
    assert {c["record_ref"]["record_id"] for c in pack["citations"]} == set(ids)


def test_requested_single_hydration_is_deterministic_and_discloses_omission(
    workspace: Workspace,
) -> None:
    _observe_many(workspace, 3)
    request = {
        "query": "item",
        "targets": [],
        "profile": "investigate",
        "budget": {"hydrations": 1},
    }
    first = workspace.ok("engineering.context.build", request)["pack"]
    second = workspace.ok("engineering.context.build", request)["pack"]
    assert first["budget"]["hydrations"] == 1
    assert len(first["sections"]) == 1
    assert {item["reason"] for item in first["omissions"]} == {"selection_limit"}
    assert any("bounded hydration" in item for item in first["uncertainties"])
    assert first["sections"] == second["sections"]


def test_a_denied_version_is_never_counted_toward_the_hydration_cap(
    workspace: Workspace,
) -> None:
    """A label-hidden version never enters the reader's preview candidate count."""
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

    owner = workspace.ok(
        "engineering.context.build",
        {"query": "item", "targets": [], "profile": "investigate"},
    )
    assert owner["pack"]["budget"]["hydrations"] == 8
    assert owner["pack"]["reproducibility"]["authorized_candidate_count"] == 9

    reader = esc._reader()
    result = workspace.ok(
        "engineering.context.build",
        {"query": "item", "targets": [], "profile": "investigate"},
        session=reader,
    )
    assert result["pack"]["budget"]["hydrations"] == 8
    assert result["pack"]["reproducibility"]["authorized_candidate_count"] == 8


def test_one_preview_match_among_more_than_the_cap_hydrates_only_one(
    workspace: Workspace,
) -> None:
    unrelated_ids = _observe_many(workspace, 12, prefix="Unrelated")
    selected_id = workspace.observe(
        esc._observation(None, title="Needle finding", evidence=False)
    )["record_id"]
    unrelated_assemblies = {
        str(row[0])
        for row in workspace.holder.connection.execute(
            "SELECT assembly_id FROM omnivia_governed_version_assemblies "
            "WHERE workspace_id = ? AND governed_record_id IN "
            f"({', '.join('?' for _ in unrelated_ids)})",
            (esc.WORKSPACE_ID, *unrelated_ids),
        )
    }
    with Trace(workspace.holder.connection) as trace:
        result = workspace.ok(
            "engineering.context.build",
            {"query": "needle", "targets": [], "profile": "investigate"},
        )
    pack = result["pack"]
    assert pack["budget"]["hydrations"] == 1
    assert [item["record_ref"]["record_id"] for item in pack["citations"]] == [
        selected_id
    ]
    assert not any(
        assembly_id in statement
        for assembly_id in unrelated_assemblies
        for statement in trace.body_reads()
    )


def test_selection_and_frontier_digest_are_independent_of_page_size(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observe_many(workspace, 11)
    instant = 1_800_000_000_000_000_000
    monkeypatch.setattr(handlers.time, "time_ns", lambda: instant)
    request = {"query": "item", "targets": [], "profile": "investigate"}
    normal = workspace.ok("engineering.context.build", request)["pack"]

    monkeypatch.setattr(handlers, "AUTHORIZED_FRONTIER_PAGE_SIZE", 2)
    paged = workspace.ok("engineering.context.build", request)["pack"]
    assert paged == normal


def test_record_id_pages_use_the_workspace_record_index(workspace: Workspace) -> None:
    _observe_many(workspace, 2)
    plan = workspace.holder.connection.execute(
        "EXPLAIN QUERY PLAN SELECT governed_record_id "
        "FROM omnivia_authoritative_governed_version_metadata "
        "WHERE workspace_id = ? AND recorded_at_us <= ? AND domain_scope = ? "
        "AND governed_record_id > ? GROUP BY governed_record_id "
        "ORDER BY governed_record_id LIMIT ?",
        (esc.WORKSPACE_ID, 2**63 - 1, "engineering.codebase", "", 512),
    ).fetchall()
    assert any(
        "omnivia_idx_governed_version_assemblies_record" in str(row[3])
        for row in plan
    ), plan


def test_section_cap_limits_a_32_hydration_request_to_24_sections(
    workspace: Workspace,
) -> None:
    _observe_many(workspace, 30)
    pack = workspace.ok(
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
    )["pack"]
    assert len(pack["sections"]) == 24
    assert pack["budget"]["hydrations"] == 24
    assert {item["reason"] for item in pack["omissions"]} == {"selection_limit"}


def test_tiny_evidence_budget_omits_optional_records_before_a_payload_select(
    workspace: Workspace,
) -> None:
    _observe_many(workspace, 1)
    connection = workspace.holder.connection
    with Trace(connection) as trace:
        pack = workspace.ok(
            "engineering.context.build",
            {
                "query": "item",
                "targets": [],
                "profile": "investigate",
                "budget": {"evidence_bytes": 1},
            },
        )["pack"]
    assert pack["budget"]["source_bytes_read"] == 0
    assert pack["budget"]["hydrations"] == 0
    assert pack["citations"] == []
    assert {item["reason"] for item in pack["omissions"]} == {"source_budget"}
    assert trace.body_reads() == []


def test_large_high_priority_body_is_skipped_and_a_smaller_group_is_hydrated(
    workspace: Workspace,
) -> None:
    large = esc._observation(None, title="Needle priority", evidence=False)
    large["content"]["summary"] = "needle " * 280
    large["content"]["what"] = "巨大🙂" * 600
    large_record_id = workspace.observe(large)["record_id"]

    small = esc._observation(None, title="Needle fallback", evidence=False)
    small["content"]["summary"] = "needle"
    small_record_id = workspace.observe(small)["record_id"]
    payload_rows = workspace.holder.connection.execute(
        "SELECT a.governed_record_id, a.assembly_id, "
        "length(CAST(a.content_json AS BLOB)), "
        "COALESCE(l.claim_byte_length, 0) "
        "FROM omnivia_governed_version_assemblies a "
        "LEFT JOIN omnivia_application_claim_lineage l "
        "ON l.workspace_id=a.workspace_id AND l.assembly_id=a.assembly_id "
        "WHERE a.workspace_id = ? AND a.governed_record_id IN (?, ?)",
        (esc.WORKSPACE_ID, large_record_id, small_record_id),
    ).fetchall()
    payload_by_record = {
        str(row[0]): (str(row[1]), int(row[2]) + int(row[3])) for row in payload_rows
    }
    large_assembly, large_bytes = payload_by_record[large_record_id]
    small_assembly, small_bytes = payload_by_record[small_record_id]
    assert large_bytes > small_bytes

    with Trace(workspace.holder.connection) as trace:
        pack = workspace.ok(
            "engineering.context.build",
            {
                "query": "needle",
                "targets": [],
                "profile": "investigate",
                "budget": {"hydrations": 1, "evidence_bytes": small_bytes},
            },
        )["pack"]

    assert [citation["record_ref"]["record_id"] for citation in pack["citations"]] == [
        small_record_id
    ]
    assert pack["budget"]["source_bytes_read"] == small_bytes
    assert pack["budget"]["hydrations"] == 1
    assert {item["reason"] for item in pack["omissions"]} == {
        "selection_limit",
        "source_budget",
    }
    assert any("source-read" in uncertainty for uncertainty in pack["uncertainties"])
    assert not any(large_assembly in statement for statement in trace.body_reads())
    assert any(small_assembly in statement for statement in trace.body_reads())


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
    record_ids = _observe_many(workspace, 3)
    result = workspace.ok(
        "engineering.context.build",
        {"query": "item", "targets": [], "profile": "investigate"},
    )
    budget = result["pack"]["budget"]
    assert budget["hydrations"] == 3
    placeholders = ", ".join("?" for _ in record_ids)
    content_bytes = sum(
        int(row[0])
        for row in workspace.holder.connection.execute(
            "SELECT length(CAST(content_json AS BLOB)) "
            "FROM omnivia_governed_version_assemblies "
            f"WHERE workspace_id = ? AND governed_record_id IN ({placeholders})",
            (esc.WORKSPACE_ID, *record_ids),
        )
    )
    claim_bytes = int(
        workspace.holder.connection.execute(
            "SELECT COALESCE(SUM(l.claim_byte_length), 0) "
            "FROM omnivia_application_claim_lineage l "
            "JOIN omnivia_governed_version_assemblies a "
            "ON a.workspace_id = l.workspace_id AND a.assembly_id = l.assembly_id "
            f"WHERE a.workspace_id = ? AND a.governed_record_id IN ({placeholders})",
            (esc.WORKSPACE_ID, *record_ids),
        ).fetchone()[0]
    )
    assert budget["source_bytes_read"] == content_bytes + claim_bytes


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
        {"authorized_candidates": 0},
        {"authorized_candidates": 10001},
        {"authorized_candidates": False},
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
                "authorized_candidates": 10000,
            },
        },
    )
    assert result["pack"]["budget"]["effective"] == {
        "model_tokens": 16000,
        "model_bytes": 65536,
        "hydrations": 32,
        "evidence_bytes": 1048576,
        "authorized_candidates": 10000,
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
        "authorized_candidates": handlers.BUDGET_DEFAULT_AUTHORIZED_CANDIDATES,
    }


@pytest.mark.parametrize("mode", ["diagnostic", "current_safe"])
def test_default_authorized_candidate_limit_fails_closed_before_hydration(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setattr(handlers, "BUDGET_DEFAULT_AUTHORIZED_CANDIDATES", 2)
    _observe_many(workspace, 3)
    payload: dict[str, Any] = {
        "query": "item",
        "targets": [],
        "profile": "investigate",
        "applicability_mode": mode,
    }
    if mode == "current_safe":
        workspace.record(esc._source(1, "esnap-a", esc.FILES_A))
        payload["targets"] = [
            {"repository_id": esc.REPOSITORY, "snapshot_id": "esnap-a"}
        ]

    with Trace(workspace.holder.connection) as trace:
        code, message, _retry = workspace.refused(
            "engineering.context.build", payload
        )

    assert code == "size_limit_exceeded"
    assert message == (
        "the authorized engineering frontier exceeds its bounded candidate budget"
    )
    assert trace.body_reads() == []


def test_requested_authorized_candidate_limit_is_enforced(
    workspace: Workspace,
) -> None:
    _observe_many(workspace, 3)
    with Trace(workspace.holder.connection) as trace:
        code, _message, _retry = workspace.refused(
            "engineering.context.build",
            {
                "query": "item",
                "targets": [],
                "profile": "investigate",
                "budget": {"authorized_candidates": 2},
            },
        )
    assert code == "size_limit_exceeded"
    assert trace.body_reads() == []


@pytest.mark.parametrize(
    ("counting_mode", "expected_code"),
    [(None, "token_limit_exceeded"), ("byte_only.v1", "context_budget_insufficient")],
)
def test_multiple_mandatory_uncertainties_that_cannot_fit_map_to_typed_error(
    workspace: Workspace,
    counting_mode: str | None,
    expected_code: str,
) -> None:
    _observe_many(workspace, 2)
    notice = (
        "Target applicability is not evaluated in this build; every "
        "applicability statement is `not_evaluated`."
    )
    payload: dict[str, Any] = {
        "query": "item",
        "targets": [],
        "profile": "investigate",
        "budget": {
            "model_bytes": len(f"[uncertainty] {notice}".encode()),
            "hydrations": 1,
        },
    }
    if counting_mode is not None:
        payload["counting_mode"] = counting_mode

    code, message, retry = workspace.refused("engineering.context.build", payload)

    assert code == expected_code
    assert message == "the minimum safe engineering context does not fit the effective budget"
    assert retry == "non_retryable"


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
