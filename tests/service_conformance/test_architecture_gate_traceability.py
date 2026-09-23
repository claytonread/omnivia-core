"""Permanent invariants for the C0b architecture-gate traceability ledger.

The ledger maps every frozen architecture v0.6 section-21 acceptance gate to a
stable test identifier. Format ``v1.1`` adds one state: a gate is
``pending_candidate`` unless it is ``accepted_passing``, and an accepted gate
names the tests that prove it, by node id, the first of which is the gate's own
``pending_test_id`` (the key keeps its v1 name). This module does not run that
evidence; it holds the ledger to it: the accepted set is exact, every named
test must exist as a top-level function in the named file, and no pytest skip
may reach accepted evidence. A pending gate may
record partial evidence only together with the reason it is still pending.

Operation references are links to the existing C0a operation traceability
catalogue. They identify direct operation-level relevance only; they do not
restate the MCP or CLI client-surface mappings recorded there.
"""

from __future__ import annotations

import ast
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

FIXTURE_FORMAT = "omnivia.architecture-gate-traceability.v1.1"
PENDING_STATE = "pending_candidate"
ACCEPTED_STATE = "accepted_passing"
SOURCE_METADATA = {
    "document": "omnivia-core-architecture-spec-v0.6-2026-07-29.md",
    "version": "0.6-draft",
    "status": "Accepted; architecture frozen",
    "section": 21,
    "gate_count": 34,
}
OPERATION_TRACEABILITY_REFERENCE = {
    "file": "tests/fixtures/service_conformance/operation-traceability-v1.json",
    "format": "omnivia.operation-traceability.v1.1",
}
GATE_KEYS = {
    "gate_id",
    "ordinal",
    "acceptance_gate",
    "pending_test_id",
    "state",
    "operation_traceability_refs",
}
PHASE2_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "phase2-platform.yml"
MCP_GATE_TESTS = "packages/omnivia-core-mcp/tests/test_mcp_architecture_gates.py"
CANDIDATE_BUILDER_TESTS = (
    "tests/package_qualification/test_standard_candidate_builder.py"
)
CLI_LIFECYCLE_TESTS = "packages/omnivia-core-cli/tests/test_lifecycle.py"
MANAGED_START_TESTS = (
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_managed_start.py"
)
EVIDENCE_FILES = {
    MCP_GATE_TESTS,
    CANDIDATE_BUILDER_TESTS,
    CLI_LIFECYCLE_TESTS,
    MANAGED_START_TESTS,
}
#: The accepted gates and their extra keys, by ordinal. Exact: accepting a gate,
#: or withdrawing one, has to change this table.
ACCEPTED_GATE_KEYS = {
    7: {"evidence"},
    8: {"evidence"},
    17: {"evidence"},
    20: {"evidence"},
    29: {"evidence"},
}
#: Pending gates that record partial evidence, and their extra keys, by ordinal.
#: Each must say why it is still pending.
PARTIALLY_EVIDENCED_GATES = {
    16: {"evidence", "qualification", "pending_reason"},
    22: {"evidence", "pending_reason"},
}
#: MCP gates this slice deliberately leaves pending: g18 needs Desktop and CLI
#: conformance evidence outside this repository, and g22 is partially evidenced
#: (see its ``pending_reason``).
MCP_GATES_LEFT_PENDING = {18, 22}

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

#: Every operation that declares a side effect, derived from the same fixture the rest
#: of this module reads rather than transcribed beside it.
#:
#: It used to be a hand-written tuple of ten names, and it went stale silently the first
#: time the catalogue grew: the two gates below are about *every* mutation -- fencing
#: after a takeover, and generation revalidation after a suspend -- so a mutation
#: missing from the list is a mutation those gates stopped claiming to cover, with
#: nothing failing to say so. Deriving it makes the drift impossible instead of
#: merely fixing this instance of it.
MUTATION_OPERATION_REFS = tuple(
    sorted(
        entry["contract"]["name"]
        for entry in OPERATION_TRACEABILITY["operations"]
        if entry["contract"]["scope"]["side_effect"] != "none"
    )
)
EXPECTED_OPERATION_REFS_BY_TEST_ID = {
    "test_architecture_gate_canonical_record_evidence_authority": ("evidence.search", "knowledge.search"),
    "test_architecture_gate_extraction_requires_governance": (
        "candidate.approve",
        "candidate.reject",
        "knowledge.propose",
        "record.supersede",
    ),
    "test_architecture_gate_acl_before_ranking_and_context": (
        "context_pack.build",
        "evidence.search",
        "graph.traverse",
        "knowledge.search",
        "memory.search",
    ),
    "test_architecture_gate_ingestion_projection_interruption_recovery": (
        "import.start",
        "job.cancel",
        "job.events",
        "job.get",
        "job.retry",
    ),
    "test_architecture_gate_persisted_context_pack_inputs": ("context_pack.build"),
}


def test_fixture_metadata_is_exact_and_deterministically_serialized() -> None:
    assert set(TRACEABILITY) == {
        "format",
        "source",
        "pending_state",
        "accepted_state",
        "operation_traceability",
        "gates",
    }
    assert TRACEABILITY["format"] == FIXTURE_FORMAT
    assert TRACEABILITY["source"] == SOURCE_METADATA
    assert TRACEABILITY["pending_state"] == PENDING_STATE
    assert TRACEABILITY["accepted_state"] == ACCEPTED_STATE
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


def test_only_the_named_gates_are_accepted_and_every_other_gate_is_pending() -> None:
    for gate in GATES:
        ordinal = gate["ordinal"]
        if ordinal in ACCEPTED_GATE_KEYS:
            assert gate["state"] == ACCEPTED_STATE, gate["gate_id"]
            assert set(gate) == GATE_KEYS | ACCEPTED_GATE_KEYS[ordinal], gate["gate_id"]
        elif ordinal in PARTIALLY_EVIDENCED_GATES:
            assert gate["state"] == PENDING_STATE, gate["gate_id"]
            assert set(gate) == GATE_KEYS | PARTIALLY_EVIDENCED_GATES[ordinal], (
                gate["gate_id"]
            )
            assert gate["pending_reason"].strip(), gate["gate_id"]
        else:
            assert gate["state"] == PENDING_STATE, gate["gate_id"]
            assert set(gate) == GATE_KEYS, gate["gate_id"]
    assert {gate["state"] for gate in GATES} == {PENDING_STATE, ACCEPTED_STATE}
    for ordinal in MCP_GATES_LEFT_PENDING:
        assert GATES[ordinal - 1]["state"] == PENDING_STATE


def _top_level_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_every_evidence_reference_names_an_existing_test_led_by_the_gate_id() -> None:
    """Evidence is a pytest node id, and the node has to exist.

    The first entry is the gate's own ``pending_test_id``; any further entries
    are supporting tests. Files are confined to :data:`EVIDENCE_FILES`: the MCP
    gate suite, the candidate-builder suite, the CLI lifecycle suite and the
    managed-start suite.
    """
    for gate in GATES:
        evidence = gate.get("evidence")
        if evidence is None:
            continue
        assert evidence, gate["gate_id"]
        assert len(evidence) == len(set(evidence)), gate["gate_id"]
        names = []
        for node_id in evidence:
            file, name = node_id.split("::")
            assert file in EVIDENCE_FILES, node_id
            assert name in _top_level_functions(REPO_ROOT / file), node_id
            names.append(name)
        assert names[0] == gate["pending_test_id"], gate["gate_id"]


SKIP_NAMES = {"skip", "skipif", "importorskip"}
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _skips(source: str, node_id_path: list[str]) -> bool:
    """Whether a pytest skip can reach the named node.

    Walks module scope, then each enclosing class, then the test itself. At each
    scope the non-definition statements (``pytestmark``, a module-level
    ``pytest.skip``) and the named definition's decorators count; the test's own
    body counts too. Any ``skip``/``skipif``/``importorskip`` name or attribute
    there is a skip -- conservative, so a false alarm is possible, a miss is not.
    """
    body = ast.parse(source).body
    scopes: list[ast.AST] = []
    for name in node_id_path:
        scopes += [stmt for stmt in body if not isinstance(stmt, _DEFINITIONS)]
        node = next(
            stmt for stmt in body if isinstance(stmt, _DEFINITIONS) and stmt.name == name
        )
        scopes += node.decorator_list
        body = node.body
    scopes += body
    return any(
        (isinstance(sub, ast.Attribute) and sub.attr in SKIP_NAMES)
        or (isinstance(sub, ast.Name) and sub.id in SKIP_NAMES)
        for scope in scopes
        for sub in ast.walk(scope)
    )


def test_no_accepted_evidence_can_silently_skip() -> None:
    """Accepted evidence must run: no skip marker or call may reach its node."""
    # Anti-vacuous: the detector sees every form it claims to.
    for source in (
        "import pytest\npytestmark = pytest.mark.skipif(True, reason='x')\ndef t(): pass",
        "import pytest\nclass C:\n    pytestmark = pytest.mark.skip\n    def t(self): pass",
        "import pytest\n@pytest.mark.skip\nclass C:\n    def t(self): pass",
        "import pytest\n@pytest.mark.skipif(True, reason='x')\ndef t(): pass",
        "import pytest\ndef t():\n    pytest.skip('x')",
        "from pytest import skip\ndef t():\n    skip('x')",
    ):
        path = ["C", "t"] if "class C" in source else ["t"]
        assert _skips(source, path), source
    assert not _skips("import pytest\ndef t():\n    assert True", ["t"])

    checked = 0
    for gate in GATES:
        if gate["state"] != ACCEPTED_STATE:
            continue
        for node_id in gate["evidence"]:
            file, *path = node_id.split("::")
            source = (REPO_ROOT / file).read_text(encoding="utf-8")
            assert not _skips(source, path), node_id
            checked += 1
    assert checked >= len(ACCEPTED_GATE_KEYS)


def test_every_qualification_reference_is_run_by_the_platform_matrix() -> None:
    workflow = PHASE2_WORKFLOW.read_text(encoding="utf-8")
    for gate in GATES:
        for script in gate.get("qualification", ()):
            assert (REPO_ROOT / script).is_file(), script
            assert f"python {script} " in workflow, script


def test_operation_traceability_reference_resolves_to_the_existing_c0a_ledger() -> None:
    reference_path = REPO_ROOT / TRACEABILITY["operation_traceability"]["file"]
    assert reference_path == OPERATION_TRACEABILITY_PATH
    assert reference_path.is_file()
    assert (
        OPERATION_TRACEABILITY["format"]
        == (TRACEABILITY["operation_traceability"]["format"])
    )
    assert len(C0A_OPERATION_NAMES) == 43
    assert len(set(C0A_OPERATION_NAMES)) == 43


def test_operation_references_are_exact_unique_deterministic_and_valid() -> None:
    known_operations = set(C0A_OPERATION_NAMES)
    for gate in GATES:
        refs = tuple(gate["operation_traceability_refs"])
        expected = EXPECTED_OPERATION_REFS_BY_TEST_ID.get(gate["pending_test_id"], ())
        assert refs == expected, gate["gate_id"]
        assert len(refs) == len(set(refs)), gate["gate_id"]
        assert set(refs) <= known_operations, gate["gate_id"]
        assert refs == tuple(name for name in C0A_OPERATION_NAMES if name in refs)
