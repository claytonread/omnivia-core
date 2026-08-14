"""Joining the accepted corpus to what the harness observed (V06-8, A8-N1).

The one module that imports both halves: the answer side
(:mod:`~omnivia_core.workflow_run.conformance`, which loads what the accepted
fixture expects) and the input side
(:mod:`~omnivia_core.workflow_run.ingress`, which builds the call and derives
what happened). Keeping the join here is what lets the loader stay
execution-free and the recipes stay answer-blind.

Three things live here. :func:`compare_case` grades one observation against one
case, field by field, and returns mismatches rather than raising.
:data:`INVARIANTS` grades the boundary claims the fixture schema can only state
about itself -- an agent actor never mutates canonical knowledge, canonical
mutation needs a validated human governance principal, run identity stays out of
`RequestMetadata`, run lineage rides in payload provenance untruncated, Core
exposes no run-state operation, and a run cancellation never reaches Core job
control. And :func:`run_conformance` runs every case in corpus order and totals
the result.

It fails closed on coverage. A report that skipped a row, doubled a row, or
graded an invariant against nothing is not a pass with a gap; it is not a pass.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from omnivia_core.workflow_run.boundary import (
    CATALOGUE,
    FORBIDDEN_RUN_STATE_OPERATIONS,
    IDENTIFIER_OCTET_BOUND,
    JOB_CONTROL_OPERATIONS,
    REQUEST_METADATA_FIELDS,
    RUN_IDENTITY_FIELDS,
    RUN_LINEAGE_SOURCE_KIND,
    Disposition,
    MutationAuthority,
    PrincipalKind,
    metadata_run_identity_fields,
)
from omnivia_core.workflow_run.conformance import (
    EXPECTED_CASE_IDS,
    WorkflowRunCase,
    WorkflowRunCorpus,
    load_corpus,
)
from omnivia_core.workflow_run.ingress import (
    RECIPES_BY_CASE,
    IngressObservation,
    IngressRecipe,
    execute_recipe,
)

#: Where the accepted fixture lives, relative to the repository root.
CORPUS_RELATIVE_PATH: Final = Path(
    "docs/quality/fixtures/core-workflow-run-memory/workflow-run-cases.json"
)

AGENT_NEVER_MUTATES_CANONICAL: Final = "agent-never-mutates-canonical"
CANONICAL_NEEDS_VALIDATED_HUMAN: Final = "canonical-needs-validated-human-governance"
NO_RUN_IDENTITY_IN_METADATA: Final = "no-run-identity-in-request-metadata"
LINEAGE_IN_PAYLOAD_PROVENANCE: Final = "run-lineage-in-payload-provenance"
NO_RUN_STATE_OPERATION: Final = "no-run-state-operation"
CANCELLATION_IS_NOT_JOB_CONTROL: Final = "run-cancellation-is-not-job-control"

#: The one catalogue operation that carries run lineage in its payload.
MEMORY_CREATE_OPERATION: Final = "memory.create"


class ConformanceRunError(ValueError):
    """The runner was handed something other than the accepted corpus."""


def _render(value: object) -> str:
    if isinstance(value, Enum):
        return repr(value.value)
    return repr(value)


def compare_case(
    case: WorkflowRunCase, observation: IngressObservation
) -> tuple[str, ...]:
    """Grade one observation against one accepted case."""
    if case.case_id != observation.case_id:
        return (f"observation is for {observation.case_id}, not {case.case_id}",)
    expected = case.expected
    mismatches = [
        f"{name}: expected {_render(want)}, observed {_render(got)}"
        for name, want, got in (
            ("disposition", expected.disposition, observation.disposition),
            ("error_code", expected.error_code, observation.error_code),
            ("core_mutation", expected.core_mutation, observation.core_mutation),
            (
                "canonical_mutation",
                expected.canonical_mutation,
                observation.canonical_mutation,
            ),
            (
                "mutation_authority",
                expected.mutation_authority,
                observation.mutation_authority,
            ),
            (
                "core_audit_event",
                expected.core_audit_event,
                observation.core_audit_event,
            ),
        )
        if want != got
    ]
    want_branches = tuple(
        (
            branch.branch_id,
            branch.core_mutation,
            branch.canonical_mutation,
            branch.core_audit_event,
        )
        for branch in expected.reconciliation_branches
    )
    got_branches = tuple(
        (
            branch.branch_id,
            branch.core_mutation,
            branch.canonical_mutation,
            branch.core_audit_event,
        )
        for branch in observation.reconciliation_branches
    )
    if want_branches != got_branches:
        mismatches.append(
            f"reconciliation_branches: expected {want_branches!r},"
            f" observed {got_branches!r}"
        )
    return tuple(mismatches)


def compare_stimulus(case: WorkflowRunCase, recipe: IngressRecipe) -> tuple[str, ...]:
    """Check the recipe models the stimulus the accepted case describes.

    Inputs only -- operation, workspace binding, granted capabilities, purpose,
    identity and whether a key is carried. No expected field is read here, so
    agreement cannot smuggle an answer into the input side.
    """
    context = case.run_context
    faults = [
        f"{name}: case states {want!r}, recipe states {got!r}"
        for name, want, got in (
            ("run_id", context.run_id, recipe.run_id),
            ("task_node_id", context.task_node_id, recipe.task_node_id),
            ("agent_id", context.agent_id, recipe.agent_id),
            ("session_id", context.session_id, recipe.session_id),
            (
                "caller_principal_kind",
                context.caller_principal_kind.value,
                recipe.principal_kind.value,
            ),
            (
                "bound_workspace_id",
                context.bound_workspace_id,
                recipe.bound_workspace_id,
            ),
        )
        if want != got
    ]
    ingress = case.ingress
    if ingress is None:
        if recipe.operation is not None:
            faults.append(f"case is run-local, recipe calls {recipe.operation!r}")
        return tuple(faults)
    if recipe.operation != ingress.operation:
        faults.append(
            f"operation: case states {ingress.operation!r},"
            f" recipe states {recipe.operation!r}"
        )
        return tuple(faults)
    if recipe.requested_workspace_id != ingress.requested_workspace_id:
        faults.append(
            f"requested_workspace_id: case states"
            f" {ingress.requested_workspace_id!r},"
            f" recipe states {recipe.requested_workspace_id!r}"
        )
    if tuple(sorted(recipe.granted_capabilities)) != tuple(
        sorted(ingress.granted_capabilities)
    ):
        faults.append(
            f"granted_capabilities: case states"
            f" {sorted(ingress.granted_capabilities)!r},"
            f" recipe states {sorted(recipe.granted_capabilities)!r}"
        )
    if recipe.purpose != ingress.purpose:
        faults.append(
            f"purpose: case states {ingress.purpose!r},"
            f" recipe states {recipe.purpose!r}"
        )
    carries_key = CATALOGUE[ingress.operation].idempotency.required
    if carries_key != (ingress.idempotency_key is not None):
        faults.append("the case does not carry a key the catalogue requires")
    return tuple(faults)


# --- the harness invariants ---------------------------------------------------


def _agent_never_mutates_canonical(
    observation: IngressObservation,
) -> tuple[str, ...]:
    faults: list[str] = []
    if observation.canonical_mutation:
        faults.append("an agent-actor result mutated canonical knowledge")
    if observation.mutation_authority is not MutationAuthority.NONE:
        faults.append("an agent-actor result carried mutation authority")
    faults.extend(
        f"branch {branch.branch_id.value} mutated canonical knowledge"
        for branch in observation.reconciliation_branches
        if branch.canonical_mutation
    )
    return tuple(faults)


def _canonical_needs_validated_human(
    observation: IngressObservation,
) -> tuple[str, ...]:
    faults: list[str] = []
    if observation.principal_kind is not PrincipalKind.HUMAN:
        faults.append("canonical knowledge changed under a non-human principal")
    if (
        observation.mutation_authority
        is not MutationAuthority.VALIDATED_HUMAN_PRINCIPAL
    ):
        faults.append("canonical knowledge changed without validated human authority")
    if observation.core_mutation is not True:
        faults.append("canonical knowledge changed without a Core mutation")
    if observation.disposition is not Disposition.ACCEPTED:
        faults.append("canonical knowledge changed on a non-accepted disposition")
    return tuple(faults)


def _no_run_identity_in_metadata(
    observation: IngressObservation,
) -> tuple[str, ...]:
    faults = [
        f"request metadata carries {name!r}"
        for name in metadata_run_identity_fields(observation.request_metadata)
    ]
    faults.extend(
        f"RequestMetadata defines {name!r}"
        for name in RUN_IDENTITY_FIELDS
        if name in REQUEST_METADATA_FIELDS
    )
    return tuple(faults)


def _lineage_in_payload_provenance(
    observation: IngressObservation,
) -> tuple[str, ...]:
    faults: list[str] = []
    if len(observation.lineage_sources) != 1:
        faults.append(
            f"expected one {RUN_LINEAGE_SOURCE_KIND} source,"
            f" found {len(observation.lineage_sources)}"
        )
    for source in observation.lineage_sources:
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            faults.append("the run lineage source carries no run id")
            continue
        if not isinstance(source.get("locator"), str):
            faults.append("the run lineage source carries no task node locator")
        octets = len(source_id.encode("utf-8"))
        if octets > IDENTIFIER_OCTET_BOUND and (
            observation.disposition is not Disposition.REJECTED
        ):
            faults.append("an over-long run id was accepted as lineage")
        if octets == IDENTIFIER_OCTET_BOUND and observation.disposition is (
            Disposition.REJECTED
        ):
            faults.append("an over-long run id was truncated to the bound")
    return tuple(faults)


def _no_run_state_operation(observation: IngressObservation) -> tuple[str, ...]:
    faults = [
        f"invoked {name!r}, which the catalogue does not define"
        for name in observation.core_operations
        if name not in CATALOGUE
    ]
    faults.extend(
        f"invoked run-state operation {name!r}"
        for name in observation.core_operations
        if name in FORBIDDEN_RUN_STATE_OPERATIONS
    )
    faults.extend(
        f"the catalogue exposes run-state operation {name!r}"
        for name in sorted(FORBIDDEN_RUN_STATE_OPERATIONS & set(CATALOGUE))
    )
    return tuple(faults)


def _cancellation_is_not_job_control(
    observation: IngressObservation,
) -> tuple[str, ...]:
    return tuple(
        f"a cancelled run reached Core job control through {name!r}"
        for name in observation.core_operations
        if name in JOB_CONTROL_OPERATIONS
    )


Invariant = tuple[
    str,
    Callable[[IngressObservation], bool],
    Callable[[IngressObservation], tuple[str, ...]],
]

INVARIANTS: Final[tuple[Invariant, ...]] = (
    (
        AGENT_NEVER_MUTATES_CANONICAL,
        lambda observation: observation.principal_kind is PrincipalKind.AGENT,
        _agent_never_mutates_canonical,
    ),
    (
        CANONICAL_NEEDS_VALIDATED_HUMAN,
        lambda observation: observation.canonical_mutation,
        _canonical_needs_validated_human,
    ),
    (
        NO_RUN_IDENTITY_IN_METADATA,
        lambda observation: bool(observation.request_metadata),
        _no_run_identity_in_metadata,
    ),
    (
        LINEAGE_IN_PAYLOAD_PROVENANCE,
        # Selected by the operation, not by the lineage: keyed on the sources it
        # found, an observation that carries no lineage at all would select
        # itself out of the one check that would have caught it.
        lambda observation: MEMORY_CREATE_OPERATION in observation.core_operations,
        _lineage_in_payload_provenance,
    ),
    (NO_RUN_STATE_OPERATION, lambda observation: True, _no_run_state_operation),
    (
        CANCELLATION_IS_NOT_JOB_CONTROL,
        lambda observation: observation.cancellation_raced,
        _cancellation_is_not_job_control,
    ),
)


@dataclass(frozen=True, slots=True)
class CaseReport:
    """One case's result. `passed` is the grader's verdict, not the fixture's."""

    case_id: str
    passed: bool
    mismatches: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id:
            raise ConformanceRunError("case_id must be a non-empty string")
        if not isinstance(self.passed, bool):
            raise ConformanceRunError("passed must be a boolean")
        mismatches = tuple(self.mismatches)
        if not all(isinstance(item, str) and item for item in mismatches):
            raise ConformanceRunError("mismatches must hold non-empty strings")
        if self.passed and mismatches:
            raise ConformanceRunError(f"{self.case_id} passed with mismatches")
        object.__setattr__(self, "mismatches", mismatches)


@dataclass(frozen=True, slots=True)
class InvariantResult:
    """One invariant across every observation selected for it."""

    invariant: str
    graded: int
    faults: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.invariant, str) or not self.invariant:
            raise ConformanceRunError("invariant must be a non-empty string")
        if isinstance(self.graded, bool) or not isinstance(self.graded, int):
            raise ConformanceRunError("graded must be an integer")
        if self.graded < 0:
            raise ConformanceRunError("graded must not be negative")
        object.__setattr__(self, "faults", tuple(self.faults))

    @property
    def passed(self) -> bool:
        # Graded nothing is not a pass: an invariant with no population is an
        # invariant that would stay green against an empty harness.
        return self.graded > 0 and not self.faults


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """Every case's report and every invariant's result."""

    reports: tuple[CaseReport, ...]
    invariants: tuple[InvariantResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "reports", tuple(self.reports))
        object.__setattr__(self, "invariants", tuple(self.invariants))
        if not all(isinstance(item, CaseReport) for item in self.reports):
            raise ConformanceRunError("reports must hold CaseReport entries")
        if not all(isinstance(item, InvariantResult) for item in self.invariants):
            raise ConformanceRunError("invariants must hold InvariantResult entries")

    @property
    def covered_every_row(self) -> bool:
        return tuple(report.case_id for report in self.reports) == EXPECTED_CASE_IDS

    @property
    def covered_every_invariant(self) -> bool:
        return tuple(item.invariant for item in self.invariants) == tuple(
            name for name, _, _ in INVARIANTS
        )

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(report.case_id for report in self.reports if not report.passed)

    @property
    def passed(self) -> bool:
        return (
            self.covered_every_row
            and self.covered_every_invariant
            and not self.failed
            and all(item.passed for item in self.invariants)
        )

    @property
    def detail(self) -> str:
        parts = [
            f"{report.case_id}: {mismatch}"
            for report in self.reports
            for mismatch in report.mismatches
        ]
        parts.extend(
            f"{item.invariant}: {fault}"
            for item in self.invariants
            for fault in item.faults
        )
        parts.extend(
            f"{item.invariant}: graded no observation"
            for item in self.invariants
            if item.graded == 0
        )
        if not self.covered_every_row:
            parts.append(
                "rows: expected RUN-V-01..RUN-V-24 in order, observed "
                f"{[report.case_id for report in self.reports]}"
            )
        if not self.covered_every_invariant:
            parts.append("invariants: the six checks did not all report")
        return "; ".join(parts)


def evaluate(
    observations: Sequence[IngressObservation],
) -> tuple[InvariantResult, ...]:
    """Grade the six boundary invariants over the observations, in fixed order."""
    items = tuple(observations)
    if not all(isinstance(item, IngressObservation) for item in items):
        raise ConformanceRunError("observations must hold IngressObservation entries")
    results: list[InvariantResult] = []
    for name, applies, check in INVARIANTS:
        selected = tuple(item for item in items if applies(item))
        results.append(
            InvariantResult(
                invariant=name,
                graded=len(selected),
                faults=tuple(
                    f"{item.case_id}: {fault}"
                    for item in selected
                    for fault in check(item)
                ),
            )
        )
    return tuple(results)


def run_corpus(corpus: WorkflowRunCorpus) -> ConformanceReport:
    """Execute every case in corpus order and grade the result."""
    reports: list[CaseReport] = []
    observations: list[IngressObservation] = []
    for case in corpus.cases:
        recipe = RECIPES_BY_CASE.get(case.case_id)
        if recipe is None:
            reports.append(
                CaseReport(
                    case_id=case.case_id,
                    passed=False,
                    mismatches=("no ingress recipe models this case",),
                )
            )
            continue
        observation = execute_recipe(recipe)
        observations.append(observation)
        mismatches = compare_stimulus(case, recipe) + compare_case(case, observation)
        reports.append(
            CaseReport(
                case_id=case.case_id,
                passed=not mismatches,
                mismatches=mismatches,
            )
        )
    return ConformanceReport(reports=tuple(reports), invariants=evaluate(observations))


def run_conformance(path: Path) -> ConformanceReport:
    """Load the accepted corpus at `path`, execute it, and grade it."""
    return run_corpus(load_corpus(path))


__all__ = [
    "AGENT_NEVER_MUTATES_CANONICAL",
    "CANCELLATION_IS_NOT_JOB_CONTROL",
    "CANONICAL_NEEDS_VALIDATED_HUMAN",
    "CORPUS_RELATIVE_PATH",
    "INVARIANTS",
    "LINEAGE_IN_PAYLOAD_PROVENANCE",
    "MEMORY_CREATE_OPERATION",
    "NO_RUN_IDENTITY_IN_METADATA",
    "NO_RUN_STATE_OPERATION",
    "CaseReport",
    "ConformanceReport",
    "ConformanceRunError",
    "InvariantResult",
    "compare_case",
    "compare_stimulus",
    "evaluate",
    "run_conformance",
    "run_corpus",
]
