"""Tests for the declared HTTP, MCP, and CLI surface inventories."""

from __future__ import annotations

import copy
import dataclasses

import pytest

from baseline.determinism import dump_json, load_json
from baseline.slices import REQUIRED_SLICE_IDS
from baseline.surfaces import (
    CAPTURE_KIND_REVIEWED_SOURCE,
    DECLARED_ENTRIES,
    EVIDENCE_GAPS_PATH,
    SURFACES,
    build_surface_inventory,
    excluded_by,
    require_declared_slices,
    verify_external_capture,
    verify_surface_inventories,
)

#: Routes the neutral Platform inventory must never contain.
MODULE_ROUTES = ("/local/memory/search", "/local/graph/preview", "/dev/tools", "/dev/evidence")

#: Dev-only MCP tools the base inventory must never contain.
DEV_ONLY_TOOLS = (
    "pattern_detect",
    "pattern_apply",
    "consolidate_knowledge",
    "get_knowledge_unit",
    "list_knowledge_units",
    "detect_conflicts",
    "get_consolidated_view",
    "resolve_conflict",
    "codebase_intelligence_search",
    "codebase_intelligence_map",
)

#: Dev-only CLI commands the base inventory must never contain. The
#: observability and evidence seams are named as a group in the Dev review
#: rather than enumerated, so these exercise the rule, not a known command list.
DEV_ONLY_COMMANDS = (
    "codebase-intelligence search",
    "codebase-intelligence map",
    "codebase-intelligence context",
    "observability trace",
    "evidence explain",
)


def test_tracked_surface_inventories_are_consistent() -> None:
    assert verify_surface_inventories() == []


def test_every_declared_slice_is_part_of_the_frozen_slice() -> None:
    require_declared_slices()

    declared = {
        entry.baseline_slice for entries in DECLARED_ENTRIES.values() for entry in entries
    }
    assert declared <= REQUIRED_SLICE_IDS


def test_every_surface_covers_the_slices_it_owns() -> None:
    for surface_id, spec in SURFACES.items():
        covered = {entry.baseline_slice for entry in DECLARED_ENTRIES[surface_id]}
        assert set(spec.required_slices) <= covered, surface_id


def test_recorded_descriptors_carry_reviewed_evidence_and_claim_no_live_capture() -> None:
    """Descriptors come from reviewed source; nothing here is a live response."""
    for surface_id in SURFACES:
        for operation in build_surface_inventory(surface_id)["operations"]:
            evidence = operation["evidence"]
            if operation["descriptor"] is None:
                assert evidence["captured"] is False, operation["operation_id"]
                assert evidence["capture_command"], operation["operation_id"]
            else:
                assert evidence["captured"] is True, operation["operation_id"]
                assert evidence["capture_kind"] == CAPTURE_KIND_REVIEWED_SOURCE
                assert evidence["live_response_captured"] is False


def test_every_required_operation_has_a_descriptor_or_an_open_gap() -> None:
    """Only readiness lacks a descriptor, and it must still carry an open gap."""
    gaps = {gap["id"]: gap for gap in load_json(EVIDENCE_GAPS_PATH)["gaps"]}
    uncaptured = []
    for surface_id in SURFACES:
        for operation in build_surface_inventory(surface_id)["operations"]:
            if operation["descriptor"] is not None:
                continue
            uncaptured.append(operation["operation_id"])
            assert gaps[operation["evidence"]["gap_id"]]["status"] == "open"

    assert uncaptured == ["health.readiness"]


def test_every_recorded_descriptor_is_a_member_of_the_frozen_inventory() -> None:
    for surface_id, spec in SURFACES.items():
        document = build_surface_inventory(surface_id)
        members = {_key(entry, spec) for entry in document["inventory"]["entries"]}
        for operation in document["operations"]:
            if operation["descriptor"] is None:
                continue
            assert _key(operation["descriptor"], spec) in members, operation["operation_id"]


def test_the_platform_inventory_holds_the_whole_neutral_route_table() -> None:
    document = build_surface_inventory("platform_http")
    routes = {(entry["method"], entry["path"]) for entry in document["inventory"]["entries"]}

    # Routes the slice does not require, recorded so the neutral surface is
    # frozen in full rather than only where Core happens to touch it.
    assert ("GET", "/workspaces/{id}/ingestion/queue") in routes
    assert ("GET", "/ingestion/runs") in routes
    assert ("GET", "/memory/source") in routes
    assert ("GET", "/memory/evidence/explain") in routes
    assert document["response_envelope"]["error"]


def test_the_context_routes_freeze_the_post_form_over_the_deprecated_get() -> None:
    document = build_surface_inventory("platform_http")
    context_routes = [
        entry for entry in document["inventory"]["entries"] if "/agent/context/" in entry["path"]
    ]

    assert {entry["method"] for entry in context_routes} == {"POST"}
    assert all("deprecated" in entry["note"] for entry in context_routes)


def test_local_and_dev_routes_are_not_in_the_neutral_inventory() -> None:
    entries = build_surface_inventory("platform_http")["inventory"]["entries"]

    assert entries
    for entry in entries:
        assert not entry["path"].startswith(("/local/", "/dev/")), entry
        assert excluded_by("platform_http", entry["path"]) is None, entry
    for path in MODULE_ROUTES:
        assert excluded_by("platform_http", path) is not None, path


def test_dev_only_tools_are_not_in_the_base_mcp_inventory() -> None:
    entries = build_surface_inventory("mcp_tools")["inventory"]["entries"]
    names = {entry["tool_name"] for entry in entries}

    for entry in entries:
        assert excluded_by("mcp_tools", entry["tool_name"]) is None, entry
    for tool in DEV_ONLY_TOOLS:
        assert tool not in names, tool
        assert excluded_by("mcp_tools", tool) is not None, tool


def test_dev_only_commands_are_not_in_the_base_cli_inventory() -> None:
    entries = build_surface_inventory("cli_commands")["inventory"]["entries"]
    names = {entry["command"] for entry in entries}

    for entry in entries:
        assert excluded_by("cli_commands", entry["command"]) is None, entry
    for command in DEV_ONLY_COMMANDS:
        assert command not in names, command
        assert excluded_by("cli_commands", command) is not None, command


@pytest.mark.parametrize("path", MODULE_ROUTES)
def test_verification_rejects_a_module_route_in_the_neutral_inventory(
    tmp_path, monkeypatch, path
) -> None:
    """A /local or /dev route must fail the tracked inventory, not slip in."""
    document = build_surface_inventory("platform_http")
    document["inventory"]["entries"].append({"kind": "operation", "method": "GET", "path": path})
    problems = _verify_tampered("platform_http", document, tmp_path, monkeypatch)

    assert any(path in problem and "matches exclusion" in problem for problem in problems)


@pytest.mark.parametrize("tool", DEV_ONLY_TOOLS)
def test_verification_rejects_a_dev_only_tool_in_the_base_inventory(
    tmp_path, monkeypatch, tool
) -> None:
    document = build_surface_inventory("mcp_tools")
    document["inventory"]["entries"].append({"kind": "operation", "tool_name": tool})
    problems = _verify_tampered("mcp_tools", document, tmp_path, monkeypatch)

    assert any(tool in problem and "matches exclusion" in problem for problem in problems)


@pytest.mark.parametrize("command", DEV_ONLY_COMMANDS)
def test_verification_rejects_a_dev_only_command_in_the_base_inventory(
    tmp_path, monkeypatch, command
) -> None:
    document = build_surface_inventory("cli_commands")
    document["inventory"]["entries"].append({"kind": "operation", "command": command})
    problems = _verify_tampered("cli_commands", document, tmp_path, monkeypatch)

    assert any(command in problem and "matches exclusion" in problem for problem in problems)


def test_a_guessed_descriptor_fails_verification(tmp_path, monkeypatch) -> None:
    """A descriptor without evidence must be rejected, not tolerated."""
    document = build_surface_inventory("platform_http")
    operation = _uncaptured_operation(document)
    operation["descriptor"] = {"method": "GET", "path": "/health"}

    problems = _verify_tampered("platform_http", document, tmp_path, monkeypatch)

    assert any("descriptor is filled in but evidence.captured is false" in p for p in problems)


def test_a_descriptor_outside_the_frozen_inventory_fails_verification(
    tmp_path, monkeypatch
) -> None:
    document = build_surface_inventory("platform_http")
    document["operations"][0]["descriptor"] = {"method": "GET", "path": "/healthz"}

    problems = _verify_tampered("platform_http", document, tmp_path, monkeypatch)

    assert any("is not in the frozen neutral_http_routes inventory" in p for p in problems)


def test_an_uncaptured_operation_may_not_point_at_a_closed_gap(tmp_path, monkeypatch) -> None:
    """Closing a gap must not leave an operation with nothing tracking it."""
    document = build_surface_inventory("platform_http")
    operation = _uncaptured_operation(document)
    operation["evidence"]["gap_id"] = "GAP-001"

    problems = _verify_tampered("platform_http", document, tmp_path, monkeypatch)

    assert any("is closed" in problem for problem in problems)


def test_every_referenced_gap_exists_and_is_owned_outside_core() -> None:
    register = load_json(EVIDENCE_GAPS_PATH)
    gaps = {gap["id"]: gap for gap in register["gaps"]}

    for entries in DECLARED_ENTRIES.values():
        for entry in entries:
            assert entry.gap_id in gaps, entry.operation_id
            assert gaps[entry.gap_id]["owner_repo"] != "omnivia-core"


def test_closed_gaps_say_what_closed_them_and_hand_over_the_remainder() -> None:
    gaps = {gap["id"]: gap for gap in load_json(EVIDENCE_GAPS_PATH)["gaps"]}

    for gap_id in ("GAP-001", "GAP-002", "GAP-003", "GAP-006"):
        assert gaps[gap_id]["status"] == "closed", gap_id
        assert gaps[gap_id]["closed_by"], gap_id
    # The reviewed source closed the descriptor gaps; live responses did not.
    handovers = (("GAP-001", "GAP-007"), ("GAP-002", "GAP-008"), ("GAP-003", "GAP-008"))
    for gap_id, residual in handovers:
        assert gaps[gap_id]["residual_gap_id"] == residual
        assert gaps[residual]["status"] == "open"
    assert gaps["GAP-006"]["residual_gap_id"] is None


def test_external_capture_accepts_a_complete_artifact(tmp_path) -> None:
    artifact = tmp_path / "mcp.json"
    artifact.write_text(dump_json(_complete_capture("mcp_tools")), encoding="utf-8")

    assert verify_external_capture("mcp_tools", artifact) == []


def test_external_capture_reports_a_missing_operation(tmp_path) -> None:
    document = _complete_capture("mcp_tools")
    dropped = document["operations"].pop()
    artifact = tmp_path / "mcp.json"
    artifact.write_text(dump_json(document), encoding="utf-8")

    problems = verify_external_capture("mcp_tools", artifact)

    assert any(dropped["operation_id"] in problem and "missing" in problem for problem in problems)


def test_external_capture_reports_an_undeclared_operation(tmp_path) -> None:
    document = _complete_capture("cli_commands")
    document["operations"].append(
        {
            "operation_id": "cli.delete",
            "descriptor": {"command": "delete", "operation": "delete"},
            "evidence": {"captured": True, "captured_from": "omnivia-dev@abc123"},
        }
    )
    artifact = tmp_path / "cli.json"
    artifact.write_text(dump_json(document), encoding="utf-8")

    problems = verify_external_capture("cli_commands", artifact)

    assert any("cli.delete" in problem and "not declared" in problem for problem in problems)


@pytest.mark.parametrize("path", MODULE_ROUTES)
def test_external_capture_rejects_a_module_route(tmp_path, path) -> None:
    """A live Platform capture may not smuggle a /local or /dev route in either."""
    document = _complete_capture("platform_http")
    document["operations"][0]["descriptor"] = {"method": "GET", "path": path}
    artifact = tmp_path / "http.json"
    artifact.write_text(dump_json(document), encoding="utf-8")

    problems = verify_external_capture("platform_http", artifact)

    assert any(path in problem and "matches exclusion" in problem for problem in problems)


@pytest.mark.parametrize("tool", DEV_ONLY_TOOLS)
def test_external_capture_rejects_a_dev_only_tool(tmp_path, tool) -> None:
    document = _complete_capture("mcp_tools")
    document["operations"][0]["descriptor"] = {"tool_name": tool, "operation": "discovery"}
    artifact = tmp_path / "mcp.json"
    artifact.write_text(dump_json(document), encoding="utf-8")

    problems = verify_external_capture("mcp_tools", artifact)

    assert any(tool in problem and "matches exclusion" in problem for problem in problems)


@pytest.mark.parametrize("command", DEV_ONLY_COMMANDS)
def test_external_capture_rejects_a_dev_only_command(tmp_path, command) -> None:
    document = _complete_capture("cli_commands")
    document["operations"][0]["descriptor"] = {"command": command, "operation": "read"}
    artifact = tmp_path / "cli.json"
    artifact.write_text(dump_json(document), encoding="utf-8")

    problems = verify_external_capture("cli_commands", artifact)

    assert any(command in problem and "matches exclusion" in problem for problem in problems)


def test_external_capture_rejects_a_tool_outside_the_base_inventory(tmp_path) -> None:
    document = _complete_capture("mcp_tools")
    document["operations"][0]["descriptor"] = {"tool_name": "memory_recall", "operation": "read"}
    artifact = tmp_path / "mcp.json"
    artifact.write_text(dump_json(document), encoding="utf-8")

    problems = verify_external_capture("mcp_tools", artifact)

    assert any("is not in the frozen base_mcp_tools inventory" in problem for problem in problems)


def test_external_capture_rejects_uncaptured_evidence(tmp_path) -> None:
    document = _complete_capture("cli_commands")
    document["operations"][0]["evidence"]["captured"] = False
    artifact = tmp_path / "cli.json"
    artifact.write_text(dump_json(document), encoding="utf-8")

    problems = verify_external_capture("cli_commands", artifact)

    assert any("evidence.captured must be true" in problem for problem in problems)


def test_external_capture_rejects_an_incomplete_descriptor(tmp_path) -> None:
    document = _complete_capture("platform_http")
    document["operations"][0]["descriptor"] = {"method": "GET"}
    artifact = tmp_path / "http.json"
    artifact.write_text(dump_json(document), encoding="utf-8")

    problems = verify_external_capture("platform_http", artifact)

    assert any("descriptor is missing ['path']" in problem for problem in problems)


def test_external_capture_rejects_an_operation_without_an_id(tmp_path) -> None:
    """An unidentifiable entry must be reported, not silently compared as None."""
    document = _complete_capture("cli_commands")
    document["operations"][0].pop("operation_id")
    artifact = tmp_path / "cli.json"
    artifact.write_text(dump_json(document), encoding="utf-8")

    problems = verify_external_capture("cli_commands", artifact)

    assert any("operation_id must be a non-empty string" in problem for problem in problems)


def test_external_capture_rejects_an_unknown_surface(tmp_path) -> None:
    problems = verify_external_capture("grpc", tmp_path / "anything.json")

    assert any("unknown surface" in problem for problem in problems)


def _complete_capture(surface_id: str) -> dict:
    """Build the artifact an owning repo would hand back, with descriptors filled."""
    spec = SURFACES[surface_id]
    document = copy.deepcopy(build_surface_inventory(surface_id))
    fallback = document["inventory"]["entries"][0]
    for operation in document["operations"]:
        if operation["descriptor"] is None:
            # Only readiness reaches this branch. A live capture would carry the
            # real route; the test only needs a member of the frozen inventory.
            operation["descriptor"] = {name: fallback[name] for name in spec.descriptor_fields}
        operation["evidence"] = {
            "captured": True,
            "captured_from": f"{spec.owner_repo}@0000000",
        }
    return document


def _verify_tampered(surface_id: str, document: dict, tmp_path, monkeypatch) -> list[str]:
    """Point one surface at a tampered artifact and run the tracked verification."""
    tampered = tmp_path / f"{surface_id}.json"
    tampered.write_text(dump_json(document), encoding="utf-8")
    monkeypatch.setitem(
        SURFACES,
        surface_id,
        dataclasses.replace(SURFACES[surface_id], artifact=tampered),
    )
    return verify_surface_inventories()


def _uncaptured_operation(document: dict) -> dict:
    return next(item for item in document["operations"] if item["descriptor"] is None)


def _key(values: dict, spec) -> tuple[str, ...]:
    return tuple(str(values.get(name) or "") for name in spec.match_fields)
