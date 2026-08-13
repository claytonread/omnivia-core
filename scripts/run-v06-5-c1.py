#!/usr/bin/env python3
"""Run and attest the exact V06-5 73-by-three semantic matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
RUNTIME_TEST_ROOT: Final = (
    REPO_ROOT / "packages/omnivia-core-runtime/tests/phase3/runtime"
)
SOURCE_ROOTS: Final = tuple(
    REPO_ROOT / part
    for part in (
        "src",
        "services/omnivia-memory/src",
        "packages/omnivia-core-runtime/src",
        "packages/omnivia-core-cli/src",
        "packages/omnivia-core-mcp/src",
        "packages/omnivia-core-client/src",
        "packages/omnivia-core-runtime/tests",
    )
) + (RUNTIME_TEST_ROOT,)
CORPUS: Final = (
    REPO_ROOT
    / "contracts/application/v1/fixtures/application-wire-adapter-conformance-v1.json"
)
ADAPTERS: Final = ("in_process", "ipc", "http")
SELECTORS: Final = (
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s1_workspace_family.py::test_v06_5_s1_workspace_family_harness_executes_exact_18_adapter_cases",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s2_memory_family.py::test_v06_5_s2_mutation_audit_reference_reaches_all_adapters",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s3_job_family.py::test_v06_5_s3_import_start_primary_replay_conflict",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s3_job_family.py::test_v06_5_s3_job_get_running_failed_succeeded",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s3_job_family.py::test_v06_5_s3_job_events_primary_and_page_2_ordered",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s3_job_family.py::test_v06_5_s3_job_cancel_primary_replay_conflict",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s3_job_family.py::test_v06_5_s3_job_retry_primary_replay_conflict",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s4_governance_family.py::test_v06_5_s4_primary_replay_conflict_across_real_adapters",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s4_governance_family.py::test_v06_5_c1_generic_error_family_crosses_every_real_adapter",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_c1_application_admission.py",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_context_pack_build.py::test_v06_5_c1_context_pack_primary_reaches_every_real_adapter",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_context_pack_build.py::test_v06_5_c1_context_pack_error_family_reaches_every_real_adapter",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_search_vertical.py::test_v06_5_c1_evidence_search_primary_and_page_2_reach_every_real_adapter",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_graph_traverse.py::test_v06_5_c1_graph_traverse_primary_and_page_2_reach_every_real_adapter",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_knowledge_search_vertical.py::test_v06_5_c1_governed_search_primary_and_page_2_reach_every_real_adapter",
    "packages/omnivia-core-runtime/tests/phase3/runtime/test_workspace_inspect_vertical.py::test_v06_5_c1_workspace_inspect_reaches_every_real_adapter",
)


def _git(*arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _default_output(head: str) -> Path:
    instant = datetime.now(UTC).strftime("c1-%Y%m%dT%H%M%SZ")
    return REPO_ROOT.parent / "_evidence/omnivia-core/v06-5" / head / instant


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="new directory for the four C1 artifacts (must not already exist)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if _git("status", "--porcelain"):
        raise SystemExit("C1 evidence requires a clean committed candidate tree")
    head = _git("rev-parse", "HEAD")
    tree = _git("rev-parse", "HEAD^{tree}")
    output = (args.output or _default_output(head)).resolve()
    output.mkdir(parents=True, exist_ok=False)

    document: dict[str, Any] = json.loads(CORPUS.read_text(encoding="utf-8"))
    corpus_sha256 = _sha256(CORPUS)
    expected = {
        (case["id"], adapter) for case in document["cases"] for adapter in ADAPTERS
    }
    manifest_path = output / "c1-corpus-manifest.json"
    ledger_path = output / "c1-219-semantic-ledger.json"
    junit_path = output / "c1-junit.xml"
    sums_path = output / "c1-sha256sums.txt"
    _write_json(
        manifest_path,
        {
            "adapters": ADAPTERS,
            "case_count": len(document["cases"]),
            "core_commit": head,
            "core_tree": tree,
            "corpus": str(CORPUS.relative_to(REPO_ROOT)),
            "corpus_sha256": corpus_sha256,
            "execution_count": len(expected),
            "format": "omnivia.v06-5.c1-corpus-manifest.v1",
            "keys": [
                {"adapter": adapter, "case_id": case_id}
                for case_id, adapter in sorted(expected)
            ],
        },
    )

    for source_root in reversed(SOURCE_ROOTS):
        sys.path.insert(0, str(source_root))
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [*(str(path) for path in SOURCE_ROOTS), os.environ.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    os.environ["OMNIVIA_V06_5_C1_LEDGER"] = "1"
    result = pytest.main([*SELECTORS, "-q", f"--junitxml={junit_path}"])

    from v06_5_c1_evidence import observations

    captured = observations()
    counts = Counter((item["case_id"], item["adapter"]) for item in captured)
    observed = set(counts)
    duplicates = sorted(key for key, count in counts.items() if count != 1)
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    passed = int(result) == 0 and not duplicates and not missing and not unexpected
    executions = [
        {
            **item,
            "core_commit": head,
            "core_tree": tree,
            "corpus_sha256": corpus_sha256,
            "outcome": "PASS" if passed else "OBSERVED",
        }
        for item in sorted(
            captured, key=lambda item: (item["case_id"], item["adapter"])
        )
    ]
    _write_json(
        ledger_path,
        {
            "core_commit": head,
            "core_tree": tree,
            "corpus_sha256": corpus_sha256,
            "duplicate_keys": [
                {"adapter": adapter, "case_id": case_id}
                for case_id, adapter in duplicates
            ],
            "executions": executions,
            "expected_executions": len(expected),
            "format": "omnivia.v06-5.c1-semantic-ledger.v1",
            "missing_keys": [
                {"adapter": adapter, "case_id": case_id} for case_id, adapter in missing
            ],
            "observed_executions": len(captured),
            "outcome": "PASS" if passed else "FAIL",
            "pytest_exit_code": int(result),
            "unexpected_keys": [
                {"adapter": adapter, "case_id": case_id}
                for case_id, adapter in unexpected
            ],
        },
    )
    sums_path.write_text(
        "".join(
            f"{_sha256(path)}  {path.name}\n"
            for path in (ledger_path, junit_path, manifest_path)
        ),
        encoding="utf-8",
    )
    if not passed:
        print(
            "C1 FAIL: "
            f"pytest={int(result)} observed={len(captured)} "
            f"missing={len(missing)} duplicate={len(duplicates)} "
            f"unexpected={len(unexpected)}"
        )
        return 1
    print(f"C1 PASS: 219/219 semantic executions; artifacts: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
