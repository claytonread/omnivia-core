"""Tests for the Workflow Run ingress conformance layer (V06-8, A8-N1).

The layer exists so that "the A8 boundary holds" means something a machine can
refuse. That claim is worth exactly what the harness is willing to fail, so most
of this file breaks something on purpose and insists the harness notices -- and
notices for the reason the case names.

Three probing hazards are handled explicitly:

* a probe that trips an unrelated rule proves nothing about the rule it was
  written for, so each hostile mutation asserts the diagnostic it must produce;
* the accepted fixture is never edited in place -- doctored corpora are built
  from replaced value objects, and the loaded corpus is asserted unchanged; and
* a refusal that could equally be explained by a second defect is paired with a
  control showing the case passes once the one defect is removed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    FROZEN_ERROR_CODES,
    OPERATION_CATALOGUE,
    is_identifier,
)
from omnivia_core.workflow_run import boundary, conformance, ingress, runner
from omnivia_core.workflow_run.boundary import (
    CATALOGUE,
    FORBIDDEN_RUN_STATE_OPERATIONS,
    IDENTIFIER_OCTET_BOUND,
    JOB_CONTROL_OPERATIONS,
    REQUEST_METADATA_FIELDS,
    RUN_IDENTITY_FIELDS,
    RUN_LINEAGE_SOURCE_KIND,
    BranchId,
    Disposition,
    MutationAuthority,
    PrincipalKind,
)
from omnivia_core.workflow_run.conformance import (
    ACCEPTED_CORPUS_SHA256,
    EXPECTED_ACTOR_TOTALS,
    EXPECTED_CASE_COUNT,
    EXPECTED_CASE_IDS,
    EXPECTED_CATEGORY_TOTALS,
    REQUIRED_SAFETY,
    CorpusError,
    WorkflowRunCorpus,
    load_corpus,
)
from omnivia_core.workflow_run.ingress import (
    RECIPES,
    RECIPES_BY_CASE,
    EvidenceRecipe,
    IngressObservation,
    IngressRecipe,
    IngressRecipeError,
    KeyDerivation,
    ObservedBranch,
    derive_decision,
    execute_recipe,
    memory_payload,
    reconcile,
    request_envelope,
)
from omnivia_core.workflow_run.runner import (
    AGENT_NEVER_MUTATES_CANONICAL,
    CANCELLATION_IS_NOT_JOB_CONTROL,
    CANONICAL_NEEDS_VALIDATED_HUMAN,
    CORPUS_RELATIVE_PATH,
    INVARIANTS,
    LINEAGE_IN_PAYLOAD_PROVENANCE,
    NO_RUN_IDENTITY_IN_METADATA,
    NO_RUN_STATE_OPERATION,
    CaseReport,
    ConformanceReport,
    ConformanceRunError,
    compare_case,
    compare_stimulus,
    evaluate,
    run_corpus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "docs" / "quality" / "fixtures" / "core-workflow-run-memory"
CORPUS_PATH = REPO_ROOT / CORPUS_RELATIVE_PATH


@pytest.fixture
def corpus() -> WorkflowRunCorpus:
    return load_corpus(CORPUS_PATH)


def _document() -> dict[str, Any]:
    parsed = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _write(tmp_path: Path, document: Mapping[str, Any]) -> Path:
    path = tmp_path / "workflow-run-cases.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _refuses(tmp_path: Path, document: Mapping[str, Any], needle: str) -> None:
    with pytest.raises(CorpusError) as error:
        load_corpus(_write(tmp_path, document))
    assert needle in str(error.value)


# --- the accepted bytes -------------------------------------------------------


#: Every file of the accepted PM copy, digested here rather than read from the
#: fixture. SHA256SUMS and manifest.json pin the other files but not themselves,
#: so an editor who rewrote a file and re-ran the hasher would leave both self-
#: consistent; these are the digests review actually accepted.
ACCEPTED_FIXTURE_DIGESTS = {
    "README.md": "e774ceb15349cec24a16e3b420b18bb436e73b3ac0603ff8df1a7a87b90fbfea",
    "SHA256SUMS": "be184e34495591d1e978c60ed11d74c8781a1316a5871f71b6aa5e1a8d78aea6",
    "manifest.json": "21a4742654fa6b6f47208ef057b707c56341b346a5b2108815fffe6adcfb3ee1",
    "schema.json": "d630bfb898ceab00f58f00d8ada3ddb25b08c7c325d4eb3d9f4d7e6a2d58bb01",
    "workflow-run-cases.json": ACCEPTED_CORPUS_SHA256,
}


def test_the_fixture_copy_is_byte_identical_to_the_accepted_digests() -> None:
    """The copy here is the accepted corpus, not a re-render of it."""
    assert (
        ACCEPTED_FIXTURE_DIGESTS["workflow-run-cases.json"]
        == "b6f5603061580ff1fae86176b08a89df99e473b3f9d612b1b9dea038941151db"
    )
    assert sorted(p.name for p in FIXTURE_ROOT.iterdir()) == sorted(
        ACCEPTED_FIXTURE_DIGESTS
    )
    for name, digest in ACCEPTED_FIXTURE_DIGESTS.items():
        payload = (FIXTURE_ROOT / name).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == digest, name


def test_the_recorded_sums_agree_with_the_accepted_digests() -> None:
    recorded = {}
    for line in (FIXTURE_ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split()
        recorded[name] = digest
    assert set(recorded) == {"README.md", "schema.json", "workflow-run-cases.json"}
    for name, digest in recorded.items():
        assert digest == ACCEPTED_FIXTURE_DIGESTS[name], name


def test_the_manifest_agrees_with_the_files_it_pins() -> None:
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fixture_id"] == conformance.EXPECTED_FIXTURE_ID
    assert manifest["status"] == conformance.EXPECTED_STATUS
    for entry in manifest["files"]:
        payload = (FIXTURE_ROOT / entry["path"]).read_bytes()
        assert len(payload) == entry["bytes"], entry["path"]
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"], entry["path"]
        assert entry["sha256"] == ACCEPTED_FIXTURE_DIGESTS[entry["path"]], entry["path"]


def test_the_corpus_validates_against_the_schema_it_ships() -> None:
    schema = json.loads((FIXTURE_ROOT / "schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(_document()))
    assert errors == []


# --- loading ------------------------------------------------------------------


def test_the_corpus_loads_all_twenty_four_cases_in_order(
    corpus: WorkflowRunCorpus,
) -> None:
    assert len(corpus.cases) == EXPECTED_CASE_COUNT
    assert tuple(case.case_id for case in corpus.cases) == EXPECTED_CASE_IDS
    assert dict(corpus.category_totals) == dict(EXPECTED_CATEGORY_TOTALS)
    assert dict(corpus.actor_totals) == dict(EXPECTED_ACTOR_TOTALS)
    assert dict(corpus.safety) == dict(REQUIRED_SAFETY)


def test_every_loaded_case_is_deeply_immutable(corpus: WorkflowRunCorpus) -> None:
    case = corpus.case("RUN-V-15")
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.title = "rewritten"  # type: ignore[misc]
    with pytest.raises(TypeError):
        corpus.authority["pm_base_commit"] = "0" * 40  # type: ignore[index]
    assert isinstance(case.expected.reconciliation_branches, tuple)


def test_the_loader_names_only_frozen_operations_and_codes(
    corpus: WorkflowRunCorpus,
) -> None:
    """The catalogue and the taxonomy are called, not restated."""
    for case in corpus.cases:
        if case.ingress is not None:
            assert case.ingress.operation in CATALOGUE
        if case.expected.error_code is not None:
            assert case.expected.error_code in FROZEN_ERROR_CODES


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        pytest.param(
            lambda document: document["cases"].pop(),
            "RUN-V-01..RUN-V-24 in order",
            id="shortened",
        ),
        pytest.param(
            lambda document: document["cases"].reverse(),
            "RUN-V-01..RUN-V-24 in order",
            id="reordered",
        ),
        pytest.param(
            lambda document: document["cases"].append(document["cases"][0]),
            "repeats a case identifier",
            id="duplicated",
        ),
        pytest.param(
            lambda document: document.__setitem__("extra", 1),
            "unknown keys",
            id="unknown-root-key",
        ),
        pytest.param(
            lambda document: document["cases"][0].__setitem__("extra", 1),
            "unknown keys",
            id="unknown-case-key",
        ),
        pytest.param(
            lambda document: document.__setitem__("fixture_id", "something-else"),
            "not 'core-workflow-run-memory-v1'",
            id="wrong-fixture-id",
        ),
        pytest.param(
            lambda document: document["safety"].__setitem__("live_urls", True),
            "safety flag 'live_urls' is not False",
            id="flipped-safety-flag",
        ),
        pytest.param(
            lambda document: document["cases"][0]["ingress"].__setitem__(
                "operation", "run.persist"
            ),
            "outside the catalogue",
            id="invented-operation",
        ),
        pytest.param(
            lambda document: document["cases"][1]["expected"].__setitem__(
                "error_code", "run_state_denied"
            ),
            "error_code is not frozen",
            id="invented-error-code",
        ),
        pytest.param(
            lambda document: document["cases"][14]["expected"].__setitem__(
                "core_mutation", False
            ),
            "fixes a Core-effect boolean it must not fix",
            id="indeterminate-fixing-a-boolean",
        ),
        pytest.param(
            lambda document: document["cases"][0]["expected"].__setitem__(
                "canonical_mutation", True
            ),
            "lets an agent mutate canonical state",
            id="agent-mutating-canonical",
        ),
        pytest.param(
            lambda document: document["cases"][4]["expected"].__setitem__(
                "core_mutation", True
            ),
            "run-local yet claims a Core effect",
            id="run-local-claiming-a-core-effect",
        ),
        pytest.param(
            lambda document: document["cases"][1]["expected"].pop("error_code"),
            "names an error code only when it rejects",
            id="rejection-without-a-code",
        ),
        pytest.param(
            lambda document: document["cases"][14]["expected"][
                "reconciliation_branches"
            ].reverse(),
            "committed_once then not_committed",
            id="reordered-branches",
        ),
        pytest.param(
            lambda document: document["cases"][0]["run_context"].pop("session_id"),
            "is missing ['session_id']",
            id="missing-identity",
        ),
    ],
)
def test_the_loader_fails_closed_on_a_doctored_corpus(
    tmp_path: Path, mutate: Any, needle: str
) -> None:
    document = _document()
    mutate(document)
    _refuses(tmp_path, document, needle)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda document: document["cases"][0].__setitem__(
                "title", "Agent captures a claim"
            ),
            id="retitled-case",
        ),
        pytest.param(
            lambda document: document["authority"].__setitem__(
                "pm_base_commit", "0" * 40
            ),
            id="restated-authority-value",
        ),
    ],
)
def test_a_structurally_legal_edit_is_still_refused_by_the_digest(
    tmp_path: Path, mutate: Any
) -> None:
    """Neither a title nor an authority value is constrained by any other rule.

    Both edits leave a corpus every structural check accepts. The digest runs
    last, so a refusal naming it is itself the proof that nothing else objected.
    """
    document = _document()
    mutate(document)
    _refuses(tmp_path, document, "not the accepted")


def test_the_digest_gate_names_the_accepted_corpus(tmp_path: Path) -> None:
    """Byte-identical content re-serialised is still a different corpus."""
    path = tmp_path / "workflow-run-cases.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    with pytest.raises(CorpusError) as error:
        load_corpus(path)
    assert ACCEPTED_CORPUS_SHA256 in str(error.value)


def test_the_loader_refuses_a_file_it_cannot_read(tmp_path: Path) -> None:
    with pytest.raises(CorpusError):
        load_corpus(tmp_path / "absent.json")


# --- the run ------------------------------------------------------------------


@pytest.fixture
def report(corpus: WorkflowRunCorpus) -> ConformanceReport:
    return run_corpus(corpus)


def test_every_accepted_case_passes(report: ConformanceReport) -> None:
    assert report.passed, report.detail
    assert report.failed == ()


def test_the_report_covers_every_row_in_order(report: ConformanceReport) -> None:
    assert tuple(item.case_id for item in report.reports) == EXPECTED_CASE_IDS
    assert report.covered_every_row


def test_every_invariant_graded_a_population(report: ConformanceReport) -> None:
    """An invariant that graded nothing would stay green against nothing."""
    assert report.covered_every_invariant
    for result in report.invariants:
        assert result.graded > 0, result.invariant
        assert result.faults == (), result.invariant


def test_every_case_has_exactly_one_recipe(corpus: WorkflowRunCorpus) -> None:
    assert tuple(recipe.case_id for recipe in RECIPES) == EXPECTED_CASE_IDS
    assert set(RECIPES_BY_CASE) == {case.case_id for case in corpus.cases}


def test_run_local_cases_invoke_no_core_operation(
    corpus: WorkflowRunCorpus,
) -> None:
    run_local = [case.case_id for case in corpus.cases if case.ingress is None]
    assert run_local == ["RUN-V-05", "RUN-V-10", "RUN-V-24"]
    for case_id in run_local:
        observation = execute_recipe(RECIPES_BY_CASE[case_id])
        assert observation.core_operations == ()
        assert observation.disposition is Disposition.NO_OP
        assert observation.core_mutation is False
        assert observation.canonical_mutation is False


def test_ingress_cases_cross_the_fake_adapter_seam(
    corpus: WorkflowRunCorpus,
) -> None:
    for case in corpus.cases:
        if case.ingress is None:
            continue
        observation = execute_recipe(RECIPES_BY_CASE[case.case_id])
        assert observation.core_operations == (case.ingress.operation,)
        assert observation.request_metadata["request_id"] == f"req.{case.case_id}"
        # The response was observed for every case but the cancellation race.
        assert observation.response_observed is (case.case_id != "RUN-V-15")


# --- the identity boundary ----------------------------------------------------


def test_request_metadata_defines_no_run_session_or_agent_identity() -> None:
    assert set(RUN_IDENTITY_FIELDS).isdisjoint(REQUEST_METADATA_FIELDS)


def test_no_encoded_request_carries_run_identity() -> None:
    for recipe in RECIPES:
        if recipe.operation is None:
            continue
        metadata = request_envelope(recipe).to_wire()["metadata"]
        assert set(RUN_IDENTITY_FIELDS).isdisjoint(metadata)


def test_all_five_identity_roles_are_exercised(corpus: WorkflowRunCorpus) -> None:
    assert boundary.IDENTITY_ROLES == (
        "run",
        "session",
        "agent",
        "caller_principal",
        "workspace",
    )
    kinds = {recipe.principal_kind for recipe in RECIPES}
    assert kinds == {PrincipalKind.AGENT, PrincipalKind.HUMAN}
    for recipe in RECIPES:
        assert recipe.run_id and recipe.session_id and recipe.agent_id
        assert recipe.principal_id and recipe.bound_workspace_id
    assert len({recipe.run_id for recipe in RECIPES}) > 1
    assert len({recipe.session_id for recipe in RECIPES}) > 1


def test_run_lineage_rides_in_payload_provenance() -> None:
    recipe = RECIPES_BY_CASE["RUN-V-01"]
    payload = memory_payload(recipe)
    lineage = payload.sources[0]
    assert lineage.kind == RUN_LINEAGE_SOURCE_KIND
    assert lineage.source_id == recipe.run_id
    assert lineage.locator == recipe.task_node_id
    assert payload.assertion.actor_id == recipe.agent_id
    assert payload.assertion.actor_kind == recipe.principal_kind.value
    assert payload.extraction is not None
    assert payload.extraction.extractor_id == recipe.agent_id
    assert payload.extraction.model_version is not None
    assert payload.extraction.prompt_version is not None


def test_an_over_long_run_id_is_refused_without_truncation() -> None:
    """The refusal is the length, and no repair may shorten it to fit."""
    recipe = RECIPES_BY_CASE["RUN-V-22"]
    emitted = ingress.OVERLONG_RUN_ID
    assert len(emitted.encode("utf-8")) == IDENTIFIER_OCTET_BOUND + 1
    assert not is_identifier(emitted)
    observation = execute_recipe(recipe)
    assert observation.disposition is Disposition.REJECTED
    assert observation.error_code == "invalid_request"
    # The value that reached the seam is the full 129 octets, not a fit.
    assert observation.lineage_sources[0]["source_id"] == emitted
    # Control: truncating it to the bound would be accepted, which is exactly
    # why truncation is forbidden -- it would forge lineage rather than refuse.
    truncated = dataclasses.replace(
        recipe, lineage_run_id=emitted[:IDENTIFIER_OCTET_BOUND]
    )
    assert derive_decision(truncated).disposition is Disposition.ACCEPTED


# --- idempotency (RUN-P-05 stays unresolved) ----------------------------------


def test_the_derived_key_is_stable_for_the_same_inputs() -> None:
    first = KeyDerivation("run.a8-0001", "node.001", {"claim": "same"})
    second = KeyDerivation("run.a8-0001", "node.001", {"claim": "same"})
    assert first.key == second.key
    assert first.digest == second.digest
    # Key order in the content must not move the digest.
    assert ingress.content_digest({"a": 1, "b": 2}) == ingress.content_digest(
        {"b": 2, "a": 1}
    )
    other = KeyDerivation("run.a8-0001", "node.002", {"claim": "same"})
    assert other.key != first.key


def test_a_reused_key_with_a_divergent_payload_fails_closed() -> None:
    recipe = RECIPES_BY_CASE["RUN-V-09"]
    assert derive_decision(recipe).error_code == "idempotency_conflict"
    # Control: recompute the key from the content it actually carries and the
    # same request is a first use, not a conflict.
    recomputed = dataclasses.replace(recipe, stale_key_derivation=None)
    assert derive_decision(recomputed).disposition is Disposition.ACCEPTED


def test_a_replay_with_the_same_key_and_payload_is_a_no_op() -> None:
    for case_id in ("RUN-V-08", "RUN-V-13"):
        decision = derive_decision(RECIPES_BY_CASE[case_id])
        assert decision.disposition is Disposition.NO_OP
        assert decision.core_mutation is False
        assert decision.core_audit_event is True


# --- cancellation, jobs and reconciliation ------------------------------------


def test_the_cancellation_race_stays_indeterminate_until_reconciled() -> None:
    observation = execute_recipe(RECIPES_BY_CASE["RUN-V-15"])
    assert observation.disposition is Disposition.INDETERMINATE_UNTIL_RECONCILED
    assert observation.core_mutation is None
    assert observation.core_audit_event is None
    assert observation.response_observed is False
    branches = observation.reconciliation_branches
    assert tuple(branch.branch_id for branch in branches) == (
        BranchId.COMMITTED_ONCE,
        BranchId.NOT_COMMITTED,
    )
    assert [branch.core_mutation for branch in branches] == [True, False]
    assert all(not branch.canonical_mutation for branch in branches)


def test_the_reconciliation_oracle_refuses_what_it_cannot_reconcile() -> None:
    settled = RECIPES_BY_CASE["RUN-V-01"]
    with pytest.raises(IngressRecipeError, match="no cancellation race"):
        reconcile(settled)
    read = dataclasses.replace(RECIPES_BY_CASE["RUN-V-17"], cancellation_raced=True)
    with pytest.raises(IngressRecipeError, match="races a read"):
        reconcile(read)


def test_a_cancelled_run_never_reaches_core_job_control() -> None:
    raced = [recipe for recipe in RECIPES if recipe.cancellation_raced]
    assert [recipe.case_id for recipe in raced] == ["RUN-V-15"]
    for recipe in raced:
        assert recipe.operation not in JOB_CONTROL_OPERATIONS
    # A Core durable job is cancelled only by an explicit, separately
    # authorized job.cancel, which is its own lifecycle.
    explicit = RECIPES_BY_CASE["RUN-V-16"]
    assert explicit.operation == "job.cancel"
    assert not explicit.cancellation_raced
    assert derive_decision(explicit).disposition is Disposition.ACCEPTED


def test_oversized_results_are_promoted_through_import_start() -> None:
    handle = execute_recipe(RECIPES_BY_CASE["RUN-V-19"])
    assert handle.disposition is Disposition.REJECTED
    assert handle.error_code == "invalid_request"
    promotion = RECIPES_BY_CASE["RUN-V-20"]
    assert promotion.operation == "import.start"
    assert derive_decision(promotion).disposition is Disposition.ACCEPTED
    # Control: the same candidate citing Core-resolvable evidence is accepted,
    # so the refusal is the runtime handle rather than the payload's shape.
    resolvable = dataclasses.replace(
        RECIPES_BY_CASE["RUN-V-19"],
        evidence=(EvidenceRecipe(kind="document", source_id="doc.a8-0007"),),
    )
    assert derive_decision(resolvable).disposition is Disposition.ACCEPTED


def test_core_exposes_no_operation_over_run_state() -> None:
    assert FORBIDDEN_RUN_STATE_OPERATIONS.isdisjoint(CATALOGUE)
    assert len(CATALOGUE) == len(OPERATION_CATALOGUE) == 20
    scaffolding = RECIPES_BY_CASE["RUN-V-24"]
    assert scaffolding.operation is None
    with pytest.raises(IngressRecipeError, match="outside the catalogue"):
        dataclasses.replace(scaffolding, operation="workflow_run.create")


# --- the derivation is independent of the expectation -------------------------


def test_the_workspace_gate_is_live() -> None:
    denied = RECIPES_BY_CASE["RUN-V-11"]
    assert derive_decision(denied).error_code == "workspace_not_granted"
    allowed = dataclasses.replace(
        denied, requested_workspace_id=denied.bound_workspace_id
    )
    assert derive_decision(allowed).disposition is Disposition.ACCEPTED


def test_the_cross_workspace_evidence_gate_is_live() -> None:
    denied = RECIPES_BY_CASE["RUN-V-12"]
    assert derive_decision(denied).error_code == "invalid_request"
    in_workspace = dataclasses.replace(
        denied,
        evidence=(
            EvidenceRecipe(
                kind="document",
                source_id="doc.a8-beta-runbook",
                workspace_id=denied.bound_workspace_id,
            ),
        ),
    )
    assert derive_decision(in_workspace).disposition is Disposition.ACCEPTED


def test_an_agent_can_never_hold_governance_authority() -> None:
    """Granting it in the recipe changes nothing: the gate is the actor."""
    for case_id in ("RUN-V-02", "RUN-V-03", "RUN-V-06"):
        recipe = RECIPES_BY_CASE[case_id]
        assert derive_decision(recipe).error_code == "capability_not_granted"
        granted = dataclasses.replace(
            recipe,
            granted_capabilities=(*recipe.granted_capabilities, "knowledge.govern"),
        )
        decision = derive_decision(granted)
        assert decision.error_code == "capability_not_granted"
        assert decision.canonical_mutation is False


def test_canonical_mutation_needs_a_validated_human_governance_principal() -> None:
    approved = RECIPES_BY_CASE["RUN-V-07"]
    decision = derive_decision(approved)
    assert decision.canonical_mutation is True
    assert decision.mutation_authority is MutationAuthority.VALIDATED_HUMAN_PRINCIPAL
    unvalidated = dataclasses.replace(approved, principal_validated=False)
    assert derive_decision(unvalidated).error_code == "capability_not_granted"
    as_agent = dataclasses.replace(approved, principal_kind=PrincipalKind.AGENT)
    assert derive_decision(as_agent).canonical_mutation is False


def test_a_recipe_cannot_describe_a_call_a_run_local_case_never_makes() -> None:
    with pytest.raises(IngressRecipeError, match="run-local yet describes a Core call"):
        dataclasses.replace(
            RECIPES_BY_CASE["RUN-V-05"], granted_capabilities=("memory.write",)
        )


def test_the_payload_validator_is_the_frozen_one() -> None:
    """The refusal comes from the contract's own semantics, not a local copy."""
    from omnivia_core.contracts.v1.semantics import validate_memory_create_input

    broken = dataclasses.replace(
        RECIPES_BY_CASE["RUN-V-01"], lineage_run_id="not a valid identifier"
    )
    with pytest.raises(ContractSemanticError):
        validate_memory_create_input(memory_payload(broken))
    assert derive_decision(broken).error_code == "invalid_request"


# --- the harness itself fails closed ------------------------------------------


def _observation(**overrides: Any) -> IngressObservation:
    base: dict[str, Any] = {
        "case_id": "RUN-V-01",
        "principal_kind": PrincipalKind.AGENT,
        "core_operations": ("memory.create",),
        "disposition": Disposition.ACCEPTED,
        "error_code": None,
        "core_mutation": True,
        "canonical_mutation": False,
        "mutation_authority": MutationAuthority.NONE,
        "core_audit_event": True,
    }
    base.update(overrides)
    return IngressObservation(**base)


def _faults(name: str, observation: IngressObservation) -> tuple[str, ...]:
    results = {item.invariant: item for item in evaluate([observation])}
    return results[name].faults


def test_the_canonical_non_mutation_invariant_fails_closed() -> None:
    faults = _faults(
        AGENT_NEVER_MUTATES_CANONICAL, _observation(canonical_mutation=True)
    )
    assert any("mutated canonical knowledge" in fault for fault in faults)


def test_the_branch_non_mutation_invariant_fails_closed() -> None:
    doctored = _observation(
        disposition=Disposition.INDETERMINATE_UNTIL_RECONCILED,
        core_mutation=None,
        core_audit_event=None,
        reconciliation_branches=(
            ObservedBranch(BranchId.COMMITTED_ONCE, True, True, True),
            ObservedBranch(BranchId.NOT_COMMITTED, False, False, False),
        ),
    )
    faults = _faults(AGENT_NEVER_MUTATES_CANONICAL, doctored)
    assert any("branch committed_once" in fault for fault in faults)


def test_the_mutation_authority_invariant_fails_closed() -> None:
    doctored = _observation(
        principal_kind=PrincipalKind.HUMAN,
        canonical_mutation=True,
        mutation_authority=MutationAuthority.NONE,
    )
    faults = _faults(CANONICAL_NEEDS_VALIDATED_HUMAN, doctored)
    assert any("without validated human authority" in fault for fault in faults)


def test_the_metadata_boundary_invariant_fails_closed() -> None:
    doctored = _observation(
        request_metadata={"request_id": "req.RUN-V-01", "run_id": "run.a8-0001"}
    )
    faults = _faults(NO_RUN_IDENTITY_IN_METADATA, doctored)
    assert faults == ("RUN-V-01: request metadata carries 'run_id'",)


def test_the_lineage_invariant_fails_closed() -> None:
    accepted_over_long = _observation(
        lineage_sources=({"source_id": ingress.OVERLONG_RUN_ID, "locator": "node.001"},)
    )
    faults = _faults(LINEAGE_IN_PAYLOAD_PROVENANCE, accepted_over_long)
    assert any("over-long run id was accepted" in fault for fault in faults)

    truncated = _observation(
        disposition=Disposition.REJECTED,
        error_code="invalid_request",
        core_mutation=False,
        lineage_sources=(
            {
                "source_id": ingress.OVERLONG_RUN_ID[:IDENTIFIER_OCTET_BOUND],
                "locator": "node.001",
            },
        ),
    )
    faults = _faults(LINEAGE_IN_PAYLOAD_PROVENANCE, truncated)
    assert any("truncated to the bound" in fault for fault in faults)

    no_locator = _observation(lineage_sources=({"source_id": "run.a8-0001"},))
    faults = _faults(LINEAGE_IN_PAYLOAD_PROVENANCE, no_locator)
    assert any("no task node locator" in fault for fault in faults)


def test_the_lineage_invariant_grades_a_memory_create_that_lost_its_lineage() -> None:
    # Hostile population: a well-formed call beside one whose lineage is gone.
    # Selecting on the lineage itself would let the second observation opt out
    # of the check and leave the invariant green on a corpus that lost lineage.
    valid = _observation(
        lineage_sources=({"source_id": "run.a8-0001", "locator": "node.001"},)
    )
    missing = _observation(case_id="RUN-V-02", lineage_sources=())
    run_local = _observation(case_id="RUN-V-24", core_operations=())
    results = {item.invariant: item for item in evaluate([valid, missing, run_local])}
    lineage = results[LINEAGE_IN_PAYLOAD_PROVENANCE]
    assert lineage.graded == 2
    assert not lineage.passed
    assert lineage.faults == (
        f"RUN-V-02: expected one {RUN_LINEAGE_SOURCE_KIND} source, found 0",
    )


def test_the_run_state_invariant_fails_closed() -> None:
    faults = _faults(
        NO_RUN_STATE_OPERATION, _observation(core_operations=("workflow_run.get",))
    )
    assert any("run-state operation" in fault for fault in faults)


def test_the_job_separation_invariant_fails_closed() -> None:
    doctored = _observation(
        cancellation_raced=True, core_operations=("memory.create", "job.cancel")
    )
    faults = _faults(CANCELLATION_IS_NOT_JOB_CONTROL, doctored)
    assert faults == (
        "RUN-V-01: a cancelled run reached Core job control through 'job.cancel'",
    )


def test_an_invariant_that_graded_nothing_is_not_a_pass() -> None:
    results = evaluate([])
    assert tuple(item.invariant for item in results) == tuple(
        name for name, _, _ in INVARIANTS
    )
    assert all(not item.passed for item in results)


# --- the grader fails closed --------------------------------------------------


def test_a_mismatched_expectation_is_reported(corpus: WorkflowRunCorpus) -> None:
    case = corpus.case("RUN-V-01")
    doctored = dataclasses.replace(
        case,
        expected=dataclasses.replace(case.expected, core_mutation=False),
    )
    mismatches = compare_case(doctored, execute_recipe(RECIPES_BY_CASE["RUN-V-01"]))
    assert mismatches == ("core_mutation: expected False, observed True",)


def test_a_mismatched_stimulus_is_reported(corpus: WorkflowRunCorpus) -> None:
    case = corpus.case("RUN-V-01")
    assert case.ingress is not None
    doctored = dataclasses.replace(
        case,
        ingress=dataclasses.replace(case.ingress, purpose="governance.review"),
    )
    faults = compare_stimulus(doctored, RECIPES_BY_CASE["RUN-V-01"])
    assert any(fault.startswith("purpose:") for fault in faults)


def test_a_case_with_no_recipe_fails_rather_than_passing_quietly(
    corpus: WorkflowRunCorpus,
) -> None:
    orphan = dataclasses.replace(corpus.case("RUN-V-01"), case_id="RUN-V-99")
    doctored = dataclasses.replace(corpus, cases=(*corpus.cases, orphan))
    report = run_corpus(doctored)
    assert not report.passed
    assert "RUN-V-99" in report.failed
    assert "no ingress recipe models this case" in report.detail


def test_a_missing_row_is_not_a_pass(corpus: WorkflowRunCorpus) -> None:
    doctored = dataclasses.replace(corpus, cases=corpus.cases[:-1])
    report = run_corpus(doctored)
    assert not report.covered_every_row
    assert not report.passed
    assert "rows: expected RUN-V-01..RUN-V-24 in order" in report.detail


def test_a_duplicated_row_is_not_a_pass(corpus: WorkflowRunCorpus) -> None:
    doctored = dataclasses.replace(corpus, cases=(*corpus.cases, corpus.cases[0]))
    report = run_corpus(doctored)
    assert not report.covered_every_row
    assert not report.passed


def test_a_case_report_cannot_claim_a_pass_it_did_not_earn() -> None:
    with pytest.raises(ConformanceRunError, match="passed with mismatches"):
        CaseReport(case_id="RUN-V-01", passed=True, mismatches=("wrong",))


def test_the_executor_refuses_an_operation_outside_the_catalogue() -> None:
    with pytest.raises(IngressRecipeError, match="outside the catalogue"):
        IngressRecipe(
            case_id="RUN-V-XX",
            run_id="run.a8-0001",
            task_node_id="node.001",
            session_id="session.a8-0001",
            principal_kind=PrincipalKind.AGENT,
            bound_workspace_id=ingress.WORKSPACE_ALPHA,
            purpose="agent.knowledge_capture",
            operation="memory.persist_run_state",
            requested_workspace_id=ingress.WORKSPACE_ALPHA,
            granted_capabilities=("memory.write",),
        )


def test_the_executor_refuses_to_encode_a_run_local_case() -> None:
    with pytest.raises(IngressRecipeError, match="makes no Core call"):
        request_envelope(RECIPES_BY_CASE["RUN-V-24"])


def test_the_corpus_is_unchanged_after_every_probe(
    corpus: WorkflowRunCorpus,
) -> None:
    """The accepted bytes are still the accepted bytes."""
    reloaded = load_corpus(CORPUS_PATH)
    assert reloaded == corpus
    assert runner.run_corpus(reloaded).passed
