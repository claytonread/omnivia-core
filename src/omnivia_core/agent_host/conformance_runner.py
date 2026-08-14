"""Executing the provider-SPI corpus against a provider, and grading it (A9-P3).

Standard library only. This is the one module that imports both halves of the
lane: the answer side (:mod:`omnivia_core.agent_host.conformance`, which loads
what the corpus fixes) and the input side
(:mod:`omnivia_core.agent_host.vector_inputs`, which builds the call to make).
Keeping the join here is what lets `conformance.py` stay execution-free and
`vector_inputs.py` stay answer-blind: neither of them can see the other, and a
recipe therefore cannot be written towards an answer.

Three things live here and nothing else. :func:`compare_case` grades one
observed :class:`~omnivia_core.agent_host.spi.HookOutcome` against one case,
field by field, and returns a mismatch list rather than raising.
:func:`execute_case` prepares a case's scenario and makes its one observed call.
:func:`run_corpus` does that for every case in corpus order and totals the
result.

**Nothing here branches on a case identifier or on an expected disposition.**
There is no answer table, no per-case special case, and no path by which the
expectation reaches the derivation of what was observed: :data:`OBSERVED_FLAGS`
takes a :class:`~omnivia_core.agent_host.spi.HookOutcome` and nothing else. A
grader that peeked would be green against a provider that answered at random.

**The structural falsehoods.** Five of the ten flags the corpus fixes are false
for every case, and they are false *structurally* rather than by policy, which
is why they are derived here rather than asserted. A `HookOutcome` records
exactly two channels by which a hook reached Core -- ``composed_operations`` and
``nested_envelopes`` -- and it has no field at all for a canonical mutation, a
host source patch, a direct store access, persisted Core run state, or a
recorded Core governance decision. The provider owns no canonical writer, no
host tree, no store, no run-state store and no governance writer to reach, so
there is nothing for such a field to record; and the requests that ask for one
of those effects are refused, while `HookOutcome` itself enforces that a refused
hook composes nothing. Each of the five therefore reads the outcome and finds
nothing recorded, which is what :func:`_nothing_recorded` says once.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Final

from omnivia_core.agent_host.conformance import (
    EXPECT_FLAGS,
    EXPECTED_CASE_IDS,
    EXPECTED_DISPOSITION_TOTALS,
    EXPECTED_FIXTURE_ID,
    EXPECTED_FIXTURE_VERSION,
    EXPECTED_HOOKS,
    CaseReport,
    ConformanceCase,
    ConformanceCorpus,
    CorpusReport,
    load_corpus,
)
from omnivia_core.agent_host.spi import HookOutcome
from omnivia_core.agent_host.vector_inputs import prepare_vector


class ConformanceRunError(ValueError):
    """The runner was handed something other than the accepted corpus.

    Distinct from an ordinary mismatch, which is reported rather than raised: a
    case that fails is a result, whereas a corpus that is not the accepted one
    is a caller defect and produces no result at all.
    """


def _nothing_recorded(outcome: HookOutcome) -> bool:
    """False: this effect has no field on :class:`HookOutcome` to be recorded in.

    See the module docstring. The two channels an outcome does record --
    composed operations and nested envelopes -- carry no canonical mutation, no
    host source patch, no store access, no run state and no governance
    decision, because the provider has none of those to reach.
    """
    del outcome
    return False


#: How each flag the corpus fixes is read off what the hook actually produced.
#: One mapping, covering all ten, so there is a single place to check that no
#: observation is an assertion in disguise. Nothing here takes the case.
OBSERVED_FLAGS: Final[Mapping[str, Callable[[HookOutcome], bool]]] = MappingProxyType(
    {
        "audit_record_required": lambda outcome: outcome.audit_record_required,
        "canonical_mutation_applied": _nothing_recorded,
        "host_source_patched": _nothing_recorded,
        "direct_storage_access": _nothing_recorded,
        "capability_expanded": lambda outcome: outcome.capability_expanded,
        "core_run_state_persisted": _nothing_recorded,
        "core_governance_decision_recorded": _nothing_recorded,
        "implies_core_job_cancel": lambda outcome: outcome.implies_core_job_cancel,
        "implies_core_job_retry": lambda outcome: outcome.implies_core_job_retry,
        "host_identity_in_request_metadata": (
            lambda outcome: outcome.host_identity_in_request_metadata
        ),
    }
)

#: The scalars compared before the flags, in the order a mismatch detail names
#: them. `hook` is the case's own, not the expectation's: a provider answering
#: about a different hook has not answered this case.
COMPARED_SCALARS: Final[tuple[str, ...]] = (
    "hook",
    "disposition",
    "error_code",
    "retry_class",
    "compatibility_status",
)


def _render(value: object) -> str:
    """A stable rendering. Enum members render as their value, not their repr."""
    if isinstance(value, Enum):
        return repr(value.value)
    return repr(value)


@dataclass(frozen=True, slots=True)
class Mismatch:
    """One field on which the observed outcome differed from the corpus."""

    field: str
    expected: str
    observed: str

    def __post_init__(self) -> None:
        for name in ("field", "expected", "observed"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")

    def __str__(self) -> str:
        return f"{self.field}: expected {self.expected}, observed {self.observed}"


@dataclass(frozen=True, slots=True)
class CaseComparison:
    """The graded result for one case: what was observed, and what differed."""

    case_id: str
    observed: HookOutcome
    mismatches: tuple[Mismatch, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str):
            raise TypeError("case_id must be a string")
        if not isinstance(self.observed, HookOutcome):
            raise TypeError("observed must be a HookOutcome")
        if isinstance(self.mismatches, str) or not isinstance(
            self.mismatches, Sequence
        ):
            raise TypeError("mismatches must be a sequence")
        entries = tuple(self.mismatches)
        if not all(isinstance(entry, Mismatch) for entry in entries):
            raise TypeError("mismatches must hold only Mismatch entries")
        object.__setattr__(self, "mismatches", entries)

    @property
    def passed(self) -> bool:
        return not self.mismatches

    @property
    def detail(self) -> str:
        """Every differing field, in the fixed comparison order."""
        return "; ".join(str(entry) for entry in self.mismatches)

    def report(self) -> CaseReport:
        """The existing per-case report DTO, composed from this comparison."""
        return CaseReport(
            case_id=self.case_id,
            passed=self.passed,
            observed=self.observed.disposition,
            detail=self.detail,
        )


def compare_case(case: ConformanceCase, outcome: HookOutcome) -> CaseComparison:
    """Grade one observed outcome against one case. Never raises on a mismatch."""
    if not isinstance(case, ConformanceCase):
        raise ConformanceRunError("case must be a ConformanceCase")
    if not isinstance(outcome, HookOutcome):
        raise ConformanceRunError("outcome must be a HookOutcome")
    expect = case.expect
    mismatches: list[Mismatch] = []
    for name in COMPARED_SCALARS:
        expected = case.hook if name == "hook" else getattr(expect, name)
        observed = getattr(outcome, name)
        if expected != observed:
            mismatches.append(
                Mismatch(
                    field=name, expected=_render(expected), observed=_render(observed)
                )
            )
    for flag in EXPECT_FLAGS:
        expected_flag = expect.flags[flag]
        observed_flag = OBSERVED_FLAGS[flag](outcome)
        if expected_flag != observed_flag:
            mismatches.append(
                Mismatch(
                    field=flag,
                    expected=_render(expected_flag),
                    observed=_render(observed_flag),
                )
            )
    return CaseComparison(
        case_id=case.id, observed=outcome, mismatches=tuple(mismatches)
    )


def execute_case(case: ConformanceCase) -> CaseReport:
    """Prepare one case's scenario, make its one observed call, and grade it."""
    if not isinstance(case, ConformanceCase):
        raise ConformanceRunError("case must be a ConformanceCase")
    scenario = prepare_vector(case)
    outcome = scenario.provider.handle(scenario.request)
    return compare_case(case, outcome).report()


def run_corpus(corpus: ConformanceCorpus) -> CorpusReport:
    """Execute every case in corpus order and total the result.

    The identifiers must be exactly the accepted ones, in order: a corpus that
    dropped, repeated or reordered a case is a different corpus, and a report
    over it would carry totals that mean something other than what they say.
    """
    if not isinstance(corpus, ConformanceCorpus):
        raise ConformanceRunError("corpus must be a ConformanceCorpus")
    if (
        corpus.fixture_id != EXPECTED_FIXTURE_ID
        or corpus.fixture_version != EXPECTED_FIXTURE_VERSION
    ):
        raise ConformanceRunError("the corpus does not carry the accepted identity")
    if corpus.hooks != EXPECTED_HOOKS:
        raise ConformanceRunError(
            "the corpus does not carry the accepted hooks, in order"
        )
    identifiers = tuple(case.id for case in corpus.cases)
    if identifiers != EXPECTED_CASE_IDS:
        raise ConformanceRunError(
            "the corpus does not carry exactly the accepted cases, in order"
        )
    if corpus.disposition_totals != EXPECTED_DISPOSITION_TOTALS:
        raise ConformanceRunError(
            "the corpus does not carry the accepted disposition totals"
        )
    reports = tuple(execute_case(case) for case in corpus.cases)
    passed = sum(1 for report in reports if report.passed)
    return CorpusReport(
        reports=reports,
        total=len(reports),
        passed=passed,
        failed=len(reports) - passed,
    )


def run_corpus_file(path: Path) -> CorpusReport:
    """Load the corpus at `path` and run it.

    `path` is explicit on purpose: where the accepted corpus lives is the
    caller's knowledge, and a default here would make this module an authority
    on a file location it has no business owning.
    """
    if not isinstance(path, Path):
        raise ConformanceRunError("path must be a Path")
    return run_corpus(load_corpus(path))


__all__ = [
    "COMPARED_SCALARS",
    "OBSERVED_FLAGS",
    "CaseComparison",
    "ConformanceRunError",
    "Mismatch",
    "compare_case",
    "execute_case",
    "run_corpus",
    "run_corpus_file",
]
