"""Phase 2 public package boundary contract tests."""

from __future__ import annotations

import ast
import sys
import sysconfig
from pathlib import Path

import omnivia_core.semantic_registry as registry

PHASE2_MODULES = ("evidence", "observations", "assertions", "candidates")

PHASE2_PUBLIC_NAMES = (
    "EVIDENCE_SCHEMA_VERSION",
    "Classification",
    "EvidenceExtraction",
    "EvidenceItem",
    "EvidenceLink",
    "EvidenceLocatorScheme",
    "EvidenceSource",
    "EvidenceSourceKind",
    "EvidenceSpan",
    "EvidenceSupportRole",
    "effective_classification",
    "evidence_dedup_signature",
    "evidence_extraction_digest",
    "evidence_extraction_payload",
    "evidence_item_digest",
    "evidence_item_payload",
    "evidence_link_digest",
    "evidence_link_payload",
    "OBSERVATION_SCHEMA_VERSION",
    "ObservationBundle",
    "ObservationFeature",
    "ObservationGeneration",
    "ObservationStatus",
    "ObservationValueKind",
    "SemanticObservation",
    "effective_observation_classification",
    "normalise_text",
    "observation_bundle_digest",
    "observation_bundle_payload",
    "observation_digest",
    "observation_equivalence_signature",
    "observation_feature_digest",
    "observation_feature_payload",
    "observation_payload",
    "ASSERTION_SCHEMA_VERSION",
    "AssertionEvidence",
    "AssertionRetraction",
    "AssertionSupersession",
    "KnowledgeAssertion",
    "KnowledgeObjectKind",
    "assertion_digest",
    "assertion_effective_interval",
    "assertion_evidence_digest",
    "assertion_evidence_payload",
    "assertion_history_digest",
    "assertion_history_payload",
    "assertion_payload",
    "assertion_retraction_digest",
    "assertion_retraction_payload",
    "assertion_supersession_digest",
    "assertion_supersession_payload",
    "current_assertion_id",
    "AGGREGATION_RULE_VERSION",
    "CANDIDATE_SCHEMA_VERSION",
    "NORMALIZATION_RULE_VERSION",
    "SUPPRESSION_RULE_VERSION",
    "CandidateBand",
    "CandidateContribution",
    "CandidateFeatureSummary",
    "CandidateReconsideration",
    "CandidateRiskBand",
    "CandidateState",
    "CandidateSuppression",
    "ContributionRole",
    "ReconsiderationReason",
    "SemanticCandidate",
    "SuppressionActivity",
    "aggregate_candidate_features",
    "candidate_bundle_digest",
    "candidate_bundle_payload",
    "candidate_contribution_digest",
    "candidate_contribution_payload",
    "candidate_digest",
    "candidate_equivalence_signature",
    "candidate_payload",
    "reconsideration_digest",
    "reconsideration_payload",
    "suppression_active",
    "suppression_digest",
    "suppression_payload",
)

STDLIB_MODULE_NAMES = set(sys.stdlib_module_names)


def _module_path(name: str) -> Path:
    return Path(__file__).parent.parent.parent / "src" / "omnivia_core" / "semantic_registry" / f"{name}.py"


def test_phase2_names_importable_from_barrel() -> None:
    for name in PHASE2_PUBLIC_NAMES:
        assert hasattr(registry, name), f"{name} not exported from semantic_registry barrel"


def test_phase2_names_in_all_exactly_once() -> None:
    for name in PHASE2_PUBLIC_NAMES:
        assert registry.__all__.count(name) == 1, f"{name} must appear exactly once in __all__"


def test_phase2_modules_import_only_stdlib_or_omnivia_core() -> None:
    for module_name in PHASE2_MODULES:
        tree = ast.parse(_module_path(module_name).read_text(), filename=module_name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.module is None:
                    continue
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                assert root == "omnivia_core" or root in STDLIB_MODULE_NAMES, (
                    f"{module_name}.py imports non-stdlib, non-omnivia_core module: {root}"
                )


def test_barrel_import_has_no_third_party_dependency(tmp_path: Path) -> None:
    stdlib_dir = sysconfig.get_paths()["stdlib"]
    script = tmp_path / "check_imports.py"
    script.write_text(
        "import sys\n"
        "before = set(sys.modules)\n"
        "import omnivia_core.semantic_registry\n"
        "after = set(sys.modules) - before\n"
        "for name in sorted(after):\n"
        "    mod = sys.modules[name]\n"
        "    file = getattr(mod, '__file__', None)\n"
        "    if file is None or name.startswith('omnivia_core'):\n"
        "        continue\n"
        "    if name.split('.')[0] in sys.builtin_module_names:\n"
        "        continue\n"
        f"    if not file.startswith({str(stdlib_dir)!r}):\n"
        "        print('THIRD_PARTY:' + name)\n"
    )
    import subprocess

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=True,
    )
    third_party = [line for line in result.stdout.splitlines() if line.startswith("THIRD_PARTY:")]
    assert not third_party, f"third-party modules imported: {third_party}"
