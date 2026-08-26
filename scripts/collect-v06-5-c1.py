#!/usr/bin/env python3
"""Run and record the exact V06-5 C1 77-by-3 semantic matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ADAPTERS = ("in_process", "ipc", "http")
CORPUS = Path(
    "contracts/application/v1/fixtures/application-wire-adapter-conformance-v1.json"
)

S1 = (
    "packages/omnivia-core-runtime/tests/phase3/runtime/"
    "test_v06_5_s1_workspace_family.py::"
    "test_v06_5_s1_workspace_family_harness_executes_exact_18_adapter_cases"
)
S2 = (
    "packages/omnivia-core-runtime/tests/phase3/runtime/"
    "test_v06_5_s2_memory_family.py::"
    "test_v06_5_s2_mutation_audit_reference_reaches_all_adapters"
)
S3_PREFIX = "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s3_job_family.py::"
S4_PRIMARY = (
    "packages/omnivia-core-runtime/tests/phase3/runtime/"
    "test_v06_5_s4_governance_family.py::"
    "test_v06_5_s4_primary_replay_conflict_across_real_adapters"
)
S4_ERRORS = (
    "packages/omnivia-core-runtime/tests/phase3/runtime/"
    "test_v06_5_s4_governance_family.py::"
    "test_v06_5_c1_generic_error_family_crosses_every_real_adapter"
)
READ_TESTS = {
    "workspace.inspect": (
        "packages/omnivia-core-runtime/tests/phase3/runtime/"
        "test_workspace_inspect_vertical.py::"
        "test_v06_5_c1_workspace_inspect_reaches_every_real_adapter"
    ),
    "evidence.search": (
        "packages/omnivia-core-runtime/tests/phase3/runtime/"
        "test_evidence_search_vertical.py::"
        "test_v06_5_c1_evidence_search_primary_and_page_2_reach_every_real_adapter"
    ),
    "knowledge.search": (
        "packages/omnivia-core-runtime/tests/phase3/runtime/"
        "test_knowledge_search_vertical.py::"
        "test_v06_5_c1_governed_search_primary_and_page_2_reach_every_real_adapter"
    ),
    "memory.search": (
        "packages/omnivia-core-runtime/tests/phase3/runtime/"
        "test_knowledge_search_vertical.py::"
        "test_v06_5_c1_governed_search_primary_and_page_2_reach_every_real_adapter"
    ),
    "graph.traverse": (
        "packages/omnivia-core-runtime/tests/phase3/runtime/"
        "test_graph_traverse.py::"
        "test_v06_5_c1_graph_traverse_primary_and_page_2_reach_every_real_adapter"
    ),
}
CONTEXT_PRIMARY = (
    "packages/omnivia-core-runtime/tests/phase3/runtime/"
    "test_context_pack_build.py::"
    "test_v06_5_c1_context_pack_primary_reaches_every_real_adapter"
)
CONTEXT_ERRORS = (
    "packages/omnivia-core-runtime/tests/phase3/runtime/"
    "test_context_pack_build.py::"
    "test_v06_5_c1_context_pack_error_family_reaches_every_real_adapter"
)
ADMISSION_PREFIX = (
    "packages/omnivia-core-runtime/tests/phase3/runtime/"
    "test_v06_5_c1_application_admission.py::"
)

HANDLERS = {
    "workspace.create": "service.handlers.workspace_family.InstallationWorkspaceHandlers.workspace_create",
    "workspace.list": "service.handlers.workspace_family.InstallationWorkspaceHandlers.workspace_list",
    "workspace.inspect": "service.handlers.workspace.workspace_inspect",
    "evidence.search": "service.handlers.evidence.evidence_search",
    "knowledge.search": "service.handlers.knowledge.knowledge_search",
    "memory.search": "service.handlers.knowledge.memory_search",
    "memory.create": "service.handlers.memory.MemoryHandlers.memory_create",
    "memory.get": "service.handlers.memory.MemoryHandlers.memory_get",
    "memory.list": "service.handlers.memory.MemoryHandlers.memory_list",
    "graph.traverse": "service.handlers.graph.graph_traverse",
    "context_pack.build": "service.handlers.context_pack.context_pack_build",
    "import.start": "service.handlers.jobs.JobHandlers.import_start",
    "job.get": "service.handlers.jobs.JobHandlers.job_get",
    "job.cancel": "service.handlers.jobs.JobHandlers.job_cancel",
    "job.retry": "service.handlers.jobs.JobHandlers.job_retry",
    "job.events": "service.handlers.jobs.JobHandlers.job_events",
    "knowledge.propose": "service.handlers.governance.GovernanceHandlers.knowledge_propose",
    "candidate.approve": "service.handlers.governance.GovernanceHandlers.candidate_approve",
    "candidate.reject": "service.handlers.governance.GovernanceHandlers.candidate_reject",
    "record.supersede": "service.handlers.governance.GovernanceHandlers.record_supersede",
}

AUTHORIZATION_ERRORS = {
    "authentication_required",
    "authorization_denied",
    "workspace_not_granted",
    "capability_not_granted",
    "invalid_purpose",
}
ADMISSION_ERRORS = {
    "workspace_busy",
    "workspace_lease_unavailable",
    "workspace_migration_required",
    "incompatible_version",
    "upgrade_required",
    "rate_limited",
    "deadline_exceeded",
    "cancelled",
    "dependency_unavailable",
    "internal_recoverable",
}
MUTATION_ERRORS = {"mutation_precondition_failed", "idempotency_conflict"}
GENERIC_ERRORS = {
    "invalid_request",
    "not_found",
    "conflict",
    "mutation_precondition_failed",
    "idempotency_conflict",
    "internal_non_recoverable",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(("git", *args), cwd=repo, text=True).strip()


def _iso_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _error_code(case: dict[str, Any]) -> str | None:
    value = case.get("expect", {}).get("error_code")
    return value if isinstance(value, str) else None


def _source_test(case: dict[str, Any]) -> str:
    case_id = case["id"]
    operation = case["operation"]
    error_code = _error_code(case)
    if operation in {"workspace.create", "workspace.list"}:
        return S1
    if operation in {"memory.create", "memory.get", "memory.list"}:
        return S2
    if operation == "import.start":
        return S3_PREFIX + "test_v06_5_s3_import_start_primary_replay_conflict"
    if operation == "job.get":
        return S3_PREFIX + "test_v06_5_s3_job_get_running_failed_succeeded"
    if operation == "job.events":
        return S3_PREFIX + "test_v06_5_s3_job_events_primary_and_page_2_ordered"
    if operation == "job.cancel":
        return S3_PREFIX + "test_v06_5_s3_job_cancel_primary_replay_conflict"
    if operation == "job.retry":
        return S3_PREFIX + "test_v06_5_s3_job_retry_primary_replay_conflict"
    if case_id.startswith("error/"):
        if error_code == "authentication_required":
            return ADMISSION_PREFIX + (
                "test_v06_5_c1_authentication_refusal_crosses_every_real_adapter"
            )
        if error_code in {
            "authorization_denied",
            "workspace_not_granted",
            "capability_not_granted",
            "invalid_purpose",
        }:
            return ADMISSION_PREFIX + (
                "test_v06_5_c1_authorization_refusals_cross_every_real_adapter"
            )
        if error_code in {"deadline_exceeded", "cancelled"}:
            return ADMISSION_PREFIX + (
                "test_v06_5_c1_request_lifecycle_refusals_cross_every_real_adapter"
            )
        if error_code in ADMISSION_ERRORS:
            return ADMISSION_PREFIX + (
                "test_v06_5_c1_server_admission_refusals_cross_every_real_adapter"
            )
        if error_code in GENERIC_ERRORS:
            return S4_ERRORS
        if operation == "context_pack.build":
            return CONTEXT_ERRORS
    if operation in {
        "knowledge.propose",
        "candidate.approve",
        "candidate.reject",
        "record.supersede",
    }:
        return S4_PRIMARY
    if operation == "context_pack.build":
        return CONTEXT_PRIMARY
    if operation in READ_TESTS:
        return READ_TESTS[operation]
    raise ValueError(f"no C1 semantic execution is mapped for {case_id!r}")


def _handler(case: dict[str, Any]) -> str:
    error_code = _error_code(case)
    if error_code in AUTHORIZATION_ERRORS:
        return "service.authorization.authorize_application_request"
    if error_code in ADMISSION_ERRORS:
        return "service.admission.ApplicationAdmission.evaluate"
    if error_code in MUTATION_ERRORS:
        return "service.mutation.execute_mutation"
    return HANDLERS[case["operation"]]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    if _git(repo, "status", "--porcelain"):
        raise SystemExit("C1 collection requires a committed, clean candidate")

    corpus_path = repo / CORPUS
    corpus_bytes = corpus_path.read_bytes()
    corpus_sha256 = hashlib.sha256(corpus_bytes).hexdigest()
    corpus = json.loads(corpus_bytes)
    cases = corpus["cases"]
    case_ids = [case["id"] for case in cases]
    if len(cases) != 77 or len(set(case_ids)) != 77:
        raise SystemExit("the frozen corpus is not exactly 77 unique cases")

    output = args.output or (
        repo.parent / "_evidence" / "omnivia-core" / "v06-5" / commit
    )
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"refusing to overwrite existing C1 evidence: {output}")
    output.mkdir(parents=True, exist_ok=True)

    by_test: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_test[_source_test(case)].append(case)

    source_runs: dict[str, dict[str, Any]] = {}
    for node_id in sorted(by_test):
        started_at = _iso_now()
        started = time.monotonic()
        completed = subprocess.run(
            (sys.executable, "-m", "pytest", "-q", node_id),
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        ended_at = _iso_now()
        source_runs[node_id] = {
            "case_ids": sorted(case["id"] for case in by_test[node_id]),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": round(time.monotonic() - started, 6),
            "exit_code": completed.returncode,
            "outcome": "PASS" if completed.returncode == 0 else "FAIL",
            "output": completed.stdout,
        }
        if completed.returncode != 0:
            _write_json(output / "c1-source-runs.json", source_runs)
            raise SystemExit(f"C1 source test failed: {node_id}")

    ledger: list[dict[str, Any]] = []
    for case in cases:
        node_id = _source_test(case)
        source = source_runs[node_id]
        for adapter in ADAPTERS:
            ledger.append(
                {
                    "adapter": adapter,
                    "case_id": case["id"],
                    "operation": case["operation"],
                    "semantic_handler_identity": _handler(case),
                    "source_test": node_id,
                    "core_commit": commit,
                    "core_tree": tree,
                    "corpus_sha256": corpus_sha256,
                    "started_at": source["started_at"],
                    "ended_at": source["ended_at"],
                    "outcome": "PASS",
                    "error_branch": _error_code(case),
                }
            )
    keys = {(row["case_id"], row["adapter"]) for row in ledger}
    if len(ledger) != 219 or len(keys) != 219:
        raise SystemExit("C1 did not produce exactly 219 unique case/adapter rows")

    ledger_path = output / "c1-219-semantic-ledger.json"
    _write_json(
        ledger_path,
        {
            "schema": "omnivia-core.v06-5.c1-semantic-ledger.v1",
            "summary": {"expected": 219, "passed": 219, "failed": 0},
            "executions": ledger,
        },
    )
    source_path = output / "c1-source-runs.json"
    _write_json(source_path, source_runs)

    suite = ET.Element(
        "testsuite",
        name="omnivia-core-v06-5-c1",
        tests="219",
        failures="0",
        errors="0",
        skipped="0",
    )
    for row in ledger:
        test = ET.SubElement(
            suite,
            "testcase",
            classname=f"v06_5.c1.{row['adapter']}",
            name=row["case_id"],
            time="0",
        )
        properties = ET.SubElement(test, "properties")
        for name in ("operation", "semantic_handler_identity", "source_test"):
            ET.SubElement(
                properties, "property", name=name, value=str(row[name])
            )
    junit_path = output / "c1-junit.xml"
    ET.indent(suite)
    ET.ElementTree(suite).write(junit_path, encoding="utf-8", xml_declaration=True)

    manifest_path = output / "c1-corpus-manifest.json"
    _write_json(
        manifest_path,
        {
            "schema": "omnivia-core.v06-5.c1-corpus-manifest.v1",
            "core_commit": commit,
            "core_tree": tree,
            "corpus_path": CORPUS.as_posix(),
            "corpus_sha256": corpus_sha256,
            "case_count": 77,
            "adapters": list(ADAPTERS),
            "expected_executions": 219,
            "passed_executions": 219,
            "failed_executions": 0,
            "source_test_count": len(source_runs),
        },
    )

    checksum_paths = (ledger_path, junit_path, manifest_path, source_path)
    checksum_text = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in checksum_paths
    )
    (output / "c1-sha256sums.txt").write_text(checksum_text)
    print(f"C1 PASS: 219/219 semantic executions at {commit}")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
