"""Tests for the golden fixtures captured from real Core code paths."""

from __future__ import annotations

import importlib

import pytest

from baseline.determinism import dump_json, find_absolute_paths, load_json
from baseline.scenarios import (
    CAPTURE_KIND_CORE_CODE_PATH,
    CAPTURE_KIND_NOT_CAPTURED,
    CORE_EQUIVALENT_NOTE,
    CORE_SURFACE,
    SCENARIOS,
    SCENARIOS_BY_SLICE,
    STATUS_CAPTURED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    build_fixtures,
    verify_fixtures,
)
from baseline.slices import REQUIRED_SLICE_IDS
from baseline.surfaces import EVIDENCE_GAPS_PATH

CAPTURED_SCENARIOS = [item for item in SCENARIOS if item.capture is not None]
PENDING_SCENARIOS = [item for item in SCENARIOS if item.capture is None]


@pytest.fixture(scope="module")
def fixtures() -> dict[str, dict]:
    return build_fixtures()


def test_tracked_fixtures_match_a_fresh_capture() -> None:
    assert verify_fixtures() == []


def test_every_required_slice_has_a_fixture() -> None:
    assert set(SCENARIOS_BY_SLICE) == REQUIRED_SLICE_IDS


def test_capture_is_reproducible() -> None:
    """Two independent sessions must produce identical artifacts."""
    first = {key: dump_json(value) for key, value in build_fixtures().items()}
    second = {key: dump_json(value) for key, value in build_fixtures().items()}

    assert first == second


@pytest.mark.parametrize(
    "scenario", CAPTURED_SCENARIOS, ids=[item.slice_id for item in CAPTURED_SCENARIOS]
)
def test_captured_fixtures_carry_no_machine_specific_paths(scenario, fixtures) -> None:
    document = fixtures[scenario.slice_id]

    leaks = find_absolute_paths(
        {"request": document["request"], "response": document["response"]}
    )

    assert leaks == []


@pytest.mark.parametrize(
    "scenario", CAPTURED_SCENARIOS, ids=[item.slice_id for item in CAPTURED_SCENARIOS]
)
def test_captured_fixtures_name_entrypoints_that_resolve(scenario) -> None:
    for dotted in scenario.entrypoints:
        module_name, _, attribute = dotted.rpartition(".")
        target = _resolve(module_name, attribute)
        assert target is not None, dotted


@pytest.mark.parametrize(
    "scenario", PENDING_SCENARIOS, ids=[item.slice_id for item in PENDING_SCENARIOS]
)
def test_pending_fixtures_record_a_gap_instead_of_inventing_output(scenario, fixtures) -> None:
    """A slice Core cannot execute must be blank and attributed, never guessed."""
    document = fixtures[scenario.slice_id]

    assert document["status"] == STATUS_PENDING
    assert document["capture_kind"] == CAPTURE_KIND_NOT_CAPTURED
    assert document["response"] is None
    assert document["request"] is None
    assert document["evidence_gap"], scenario.slice_id
    assert document["surface"] != CORE_SURFACE


def test_captured_fixtures_do_not_claim_to_be_external_response_captures(fixtures) -> None:
    """A Core-side equivalent must never read as a Platform, MCP, or CLI capture."""
    for scenario in CAPTURED_SCENARIOS:
        document = fixtures[scenario.slice_id]

        assert document["capture_kind"] == CAPTURE_KIND_CORE_CODE_PATH, scenario.slice_id
        assert document["surface"] == CORE_SURFACE, scenario.slice_id
        assert CORE_EQUIVALENT_NOTE in document["notes"], scenario.slice_id


def test_every_fixture_gap_is_still_open(fixtures) -> None:
    """A fixture may only defer to a gap that is actually still open."""
    gaps = {gap["id"]: gap for gap in load_json(EVIDENCE_GAPS_PATH)["gaps"]}

    for scenario in SCENARIOS:
        gap_id = fixtures[scenario.slice_id]["evidence_gap"]
        if gap_id is None:
            continue
        assert gaps[gap_id]["status"] == "open", (scenario.slice_id, gap_id)


def test_partial_fixtures_declare_which_half_is_frozen(fixtures) -> None:
    document = fixtures["context.pack"]

    assert document["status"] == STATUS_PARTIAL
    assert document["evidence_gap"] == "GAP-005"
    assert "storage_schema" in document["response"]


def test_workspace_fixture_never_uses_the_home_storage_default(fixtures) -> None:
    """WorkspaceCreate defaults storage to ~/.omnivia; the baseline must not."""
    document = fixtures["workspace.create"]
    payload = dump_json({"request": document["request"], "response": document["response"]})

    assert document["response"]["storage_path"] == "<workspace-storage>"
    assert "<home>" not in payload


def test_ingestion_fixture_records_the_whole_import_flow(fixtures) -> None:
    response = fixtures["ingestion.import_directory"]["response"]

    assert response["summary"]["files_seen"] == 3
    assert response["summary"]["errors"] == []
    assert response["workspace_after_import"]["index_status"] == "indexed"
    assert len(response["sources"]) == 3
    assert all(entry["chunks"] for entry in response["sources"])


def test_graph_preview_fixture_keeps_missing_evidence_visible(fixtures) -> None:
    """A fact with no evidence must warn rather than vanish."""
    response = fixtures["graph.preview"]["response"]

    assert any("no source evidence" in warning for warning in response["warnings"])


def test_graph_traversal_fixture_records_the_full_path(fixtures) -> None:
    response = fixtures["graph.traversal"]["response"]

    assert response["path_found"] is True
    assert [entity["name"] for entity in response["path"]] == [
        "OmniVia Core",
        "Portable Contracts",
        "Platform Runtime",
        "Desktop Shell",
    ]
    assert {entity["approval_status"] for entity in response["path"]} == {"proposed"}


def test_context_search_fixture_pairs_memories_with_excerpts(fixtures) -> None:
    response = fixtures["context.search"]["response"]

    assert response["count"] > 0
    assert all(entry["excerpt"] for entry in response["results"])


def test_captured_fixtures_declare_their_status_and_normalization(fixtures) -> None:
    for scenario in CAPTURED_SCENARIOS:
        document = fixtures[scenario.slice_id]
        assert document["status"] in {STATUS_CAPTURED, STATUS_PARTIAL}
        assert "redactions" in document["normalization"]
        assert document["task"] == "T-0627"


def test_fixture_files_on_disk_are_canonical_json() -> None:
    for scenario in SCENARIOS:
        raw = scenario.path.read_text(encoding="utf-8")
        assert raw == dump_json(load_json(scenario.path)), scenario.filename


def _resolve(module_name: str, attribute: str):
    while module_name:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            module_name, _, head = module_name.rpartition(".")
            attribute = f"{head}.{attribute}"
            continue
        target = module
        for part in attribute.split("."):
            target = getattr(target, part, None)
            if target is None:
                return None
        return target
    return None
