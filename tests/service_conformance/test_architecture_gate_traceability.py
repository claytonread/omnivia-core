"""Permanent invariants for the C0b architecture-gate traceability ledger.

The ledger maps every frozen architecture v0.6 section-21 acceptance gate to a
stable future test identifier. It is planning metadata only: every gate remains
``pending_candidate`` and no entry is implementation or live-evidence proof.

Operation references are links to the existing C0a operation traceability
catalogue. They identify direct operation-level relevance only; in particular,
they do not decide the still-pending MCP or CLI per-operation mappings.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_PATH = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "service_conformance"
    / "architecture-gate-traceability-v1.json"
)
OPERATION_TRACEABILITY_PATH = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "service_conformance"
    / "operation-traceability-v1.json"
)

FIXTURE_FORMAT = "omnivia.architecture-gate-traceability.v1"
PENDING_STATE = "pending_candidate"
SOURCE_METADATA = {
    "document": "omnivia-core-architecture-spec-v0.6-2026-07-29.md",
    "version": "0.6-draft",
    "status": "Accepted; architecture frozen",
    "section": 21,
    "gate_count": 34,
}
OPERATION_TRACEABILITY_REFERENCE = {
    "file": "tests/fixtures/service_conformance/operation-traceability-v1.json",
    "format": "omnivia.operation-traceability.v1",
}
GATE_KEYS = {
    "gate_id",
    "ordinal",
    "acceptance_gate",
    "pending_test_id",
    "state",
    "operation_traceability_refs",
}

EXPECTED_ACCEPTANCE_GATES = (
    "every canonical record can explain its evidence and authority;",
    "historical state reconstruction is tested;",
    "automatic extraction cannot bypass governance;",
    "ACL tests prove unauthorized content never reaches ranking or model context;",
    "ingestion and projections recover after interruption;",
    "workspace backup and restore preserve identity and history;",
    "MCP operates independently of the desktop application;",
    "MCP network mode is authenticated and loopback-safe by default;",
    (
        "sqlite-vec and Zvec have reproducible architectural comparison results "
        "at 10k, 100k and 1m where hardware permits;"
    ),
    (
        "the selected default semantic engine passes signed macOS, Windows and "
        "Linux packaging, security, recovery and performance gates;"
    ),
    (
        "every optional semantic engine included in a release passes the same "
        "applicable distribution gates;"
    ),
    (
        "an engine used only as an unshipped comparator does not require "
        "production distribution certification;"
    ),
    "projection loss can be repaired without losing canonical records;",
    "agent-run compression cannot mutate canonical knowledge;",
    "local and cloud implementations pass contract-parity tests;",
    (
        "a clean installation can run Core and connect Claude through MCP "
        "without OmniVia Desktop;"
    ),
    "the base MCP server has no dependency on OmniVia Dev;",
    (
        "Desktop, CLI and MCP clients pass the same application-contract "
        "conformance tests;"
    ),
    (
        "stopping the Desktop application does not stop a separately configured "
        "Core Service;"
    ),
    (
        "MCP managed-local and service-client modes return equivalent authorized "
        "results for the same workspace state;"
    ),
    (
        "only one authoritative Core Service can obtain a writable workspace "
        "service lease;"
    ),
    (
        "MCP, CLI, Desktop and launchers never own the authoritative workspace "
        "service lease;"
    ),
    "bootstrap mutex prevents duplicate service startup and is always released;",
    "failed managed startup cleans up safely;",
    "fencing prevents an old service from committing after takeover;",
    "generation is revalidated after sleep, resume and suspension;",
    "unreliable-lock filesystems refuse writable direct-file operation;",
    "stale-lease recovery and graceful handover are tested;",
    "stdio MCP cannot enumerate ungranted workspaces;",
    "client-supplied identity cannot expand permissions;",
    ("application API compatibility, error and job contracts pass conformance tests;"),
    "service upgrades refuse incompatible workspace formats safely;",
    (
        "persisted Context Pack snapshots retain all policy, source and "
        "projection inputs;"
    ),
    "initial semantic-engine feasibility occurs before production adapter lock-in.",
)

EXPECTED_PENDING_TEST_IDS = (
    "test_architecture_gate_canonical_record_evidence_authority",
    "test_architecture_gate_historical_state_reconstruction",
    "test_architecture_gate_extraction_requires_governance",
    "test_architecture_gate_acl_before_ranking_and_context",
    "test_architecture_gate_ingestion_projection_interruption_recovery",
    "test_architecture_gate_backup_restore_identity_history",
    "test_architecture_gate_mcp_desktop_independence",
    "test_architecture_gate_mcp_network_auth_loopback_default",
    "test_architecture_gate_semantic_engine_comparison_scales",
    "test_architecture_gate_default_semantic_engine_distribution",
    "test_architecture_gate_optional_semantic_engine_distribution",
    "test_architecture_gate_unshipped_comparator_certification_exemption",
    "test_architecture_gate_projection_repair_preserves_canonical_records",
    "test_architecture_gate_agent_compression_canonical_immutability",
    "test_architecture_gate_local_cloud_contract_parity",
    "test_architecture_gate_clean_install_mcp_without_desktop",
    "test_architecture_gate_base_mcp_no_dev_dependency",
    "test_architecture_gate_client_application_contract_conformance",
    "test_architecture_gate_desktop_stop_core_service_independence",
    "test_architecture_gate_mcp_mode_authorized_result_equivalence",
    "test_architecture_gate_single_authoritative_workspace_lease",
    "test_architecture_gate_clients_never_own_workspace_lease",
    "test_architecture_gate_bootstrap_mutex_startup_and_release",
    "test_architecture_gate_managed_startup_cleanup",
    "test_architecture_gate_fencing_rejects_old_service_after_takeover",
    "test_architecture_gate_generation_revalidation_after_suspend",
    "test_architecture_gate_unreliable_lock_filesystem_refusal",
    "test_architecture_gate_stale_lease_recovery_and_handover",
    "test_architecture_gate_stdio_mcp_workspace_grants",
    "test_architecture_gate_client_identity_cannot_expand_permissions",
    "test_architecture_gate_application_api_contract_conformance",
    "test_architecture_gate_upgrade_incompatible_workspace_refusal",
    "test_architecture_gate_persisted_context_pack_inputs",
    "test_architecture_gate_semantic_feasibility_before_lock_in",
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        document: dict[str, Any] = json.load(handle)
    return document


TRACEABILITY = _load_json(FIXTURE_PATH)
GATES: list[dict[str, Any]] = TRACEABILITY["gates"]
OPERATION_TRACEABILITY = _load_json(OPERATION_TRACEABILITY_PATH)
C0A_OPERATION_NAMES = tuple(
    entry["contract"]["name"] for entry in OPERATION_TRACEABILITY["operations"]
)

MUTATION_OPERATION_REFS = (
    "candidate.approve",
    "candidate.reject",
    "chat.command",
    "import.start",
    "job.cancel",
    "job.retry",
    "knowledge.propose",
    "memory.create",
    "record.supersede",
    "workspace.create",
)
EXPECTED_OPERATION_REFS_BY_TEST_ID = {
    EXPECTED_PENDING_TEST_IDS[0]: ("evidence.search", "knowledge.search"),
    EXPECTED_PENDING_TEST_IDS[2]: (
        "candidate.approve",
        "candidate.reject",
        "knowledge.propose",
        "record.supersede",
    ),
    EXPECTED_PENDING_TEST_IDS[3]: (
        "context_pack.build",
        "evidence.search",
        "graph.traverse",
        "knowledge.search",
        "memory.search",
    ),
    EXPECTED_PENDING_TEST_IDS[4]: (
        "import.start",
        "job.cancel",
        "job.events",
        "job.get",
        "job.retry",
    ),
    EXPECTED_PENDING_TEST_IDS[14]: C0A_OPERATION_NAMES,
    EXPECTED_PENDING_TEST_IDS[24]: MUTATION_OPERATION_REFS,
    EXPECTED_PENDING_TEST_IDS[25]: MUTATION_OPERATION_REFS,
    EXPECTED_PENDING_TEST_IDS[29]: C0A_OPERATION_NAMES,
    EXPECTED_PENDING_TEST_IDS[30]: C0A_OPERATION_NAMES,
    EXPECTED_PENDING_TEST_IDS[32]: ("context_pack.build",),
}


def test_fixture_metadata_is_exact_and_deterministically_serialized() -> None:
    assert set(TRACEABILITY) == {
        "format",
        "source",
        "pending_state",
        "operation_traceability",
        "gates",
    }
    assert TRACEABILITY["format"] == FIXTURE_FORMAT
    assert TRACEABILITY["source"] == SOURCE_METADATA
    assert TRACEABILITY["pending_state"] == PENDING_STATE
    assert TRACEABILITY["operation_traceability"] == (OPERATION_TRACEABILITY_REFERENCE)
    assert FIXTURE_PATH.read_text(encoding="utf-8") == (
        json.dumps(TRACEABILITY, indent=2, ensure_ascii=False) + "\n"
    )


def test_fixture_covers_each_frozen_section_21_gate_exactly_once_in_order() -> None:
    observed = tuple(gate["acceptance_gate"] for gate in GATES)
    assert observed == EXPECTED_ACCEPTANCE_GATES
    assert len(observed) == SOURCE_METADATA["gate_count"] == 34
    assert len(set(observed)) == 34
    assert tuple(gate["ordinal"] for gate in GATES) == tuple(range(1, 35))


def test_every_gate_has_unique_stable_named_identifiers() -> None:
    gate_ids = tuple(gate["gate_id"] for gate in GATES)
    pending_test_ids = tuple(gate["pending_test_id"] for gate in GATES)

    assert gate_ids == tuple(
        f"architecture-v0.6-s21-g{ordinal:02d}" for ordinal in range(1, 35)
    )
    assert len(set(gate_ids)) == 34
    assert pending_test_ids == EXPECTED_PENDING_TEST_IDS
    assert len(set(pending_test_ids)) == 34
    for pending_test_id in pending_test_ids:
        assert re.fullmatch(r"test_architecture_gate_[a-z0-9_]+", pending_test_id)


def test_every_gate_is_metadata_only_and_pending_candidate() -> None:
    assert all(set(gate) == GATE_KEYS for gate in GATES)
    assert {gate["state"] for gate in GATES} == {PENDING_STATE}


def test_operation_traceability_reference_resolves_to_the_existing_c0a_ledger() -> None:
    reference_path = REPO_ROOT / TRACEABILITY["operation_traceability"]["file"]
    assert reference_path == OPERATION_TRACEABILITY_PATH
    assert reference_path.is_file()
    assert (
        OPERATION_TRACEABILITY["format"]
        == (TRACEABILITY["operation_traceability"]["format"])
    )
    assert len(C0A_OPERATION_NAMES) == 23
    assert len(set(C0A_OPERATION_NAMES)) == 23


def test_operation_references_are_exact_unique_deterministic_and_valid() -> None:
    known_operations = set(C0A_OPERATION_NAMES)
    for gate in GATES:
        refs = tuple(gate["operation_traceability_refs"])
        expected = EXPECTED_OPERATION_REFS_BY_TEST_ID.get(gate["pending_test_id"], ())
        assert refs == expected, gate["gate_id"]
        assert len(refs) == len(set(refs)), gate["gate_id"]
        assert set(refs) <= known_operations, gate["gate_id"]
        assert refs == tuple(name for name in C0A_OPERATION_NAMES if name in refs)
