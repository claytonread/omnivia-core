"""The execution and comparator layer: all 42 vectors, graded (V06-8, A9-P3).

Four halves. First the whole corpus is executed and the totals are checked
against the corpus's own -- 42 run, 42 passed, 0 failed, and the six disposition
totals observed rather than restated. Then the comparator is mutated in both
directions: every compared scalar and every one of the ten flags is flipped on
the expectation side and again on the observation side, and each flip has to
turn a passing case into a failing one that names that field. Then the runner is
fed corpora that are not the accepted one -- reordered, short, repeating a case
-- and has to refuse them rather than report on them. Then the two architectural
properties are asserted against the source itself: the runner branches on no
case identifier, and `vector_inputs` still cannot see an answer.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from omnivia_core.agent_host import conformance_runner
from omnivia_core.agent_host.conformance import (
    EXPECT_FLAGS,
    EXPECTED_CASE_IDS,
    EXPECTED_DISPOSITION_TOTALS,
    CaseReport,
    ConformanceCase,
    ConformanceCorpus,
    CorpusReport,
    load_corpus,
)
from omnivia_core.agent_host.conformance_runner import (
    COMPARED_SCALARS,
    OBSERVED_FLAGS,
    CaseComparison,
    ConformanceRunError,
    Mismatch,
    compare_case,
    execute_case,
    run_corpus,
    run_corpus_file,
)
from omnivia_core.agent_host.spi import Disposition, Hook, HookOutcome
from omnivia_core.agent_host.vector_inputs import prepare_vector

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_FILE = (
    REPO_ROOT
    / "docs"
    / "quality"
    / "fixtures"
    / "core-agent-host-provider-spi"
    / "provider-spi-cases.json"
)

CORPUS = load_corpus(CORPUS_FILE)
CASES = CORPUS.cases
ALL_IDS = tuple(case.id for case in CASES)

#: A second value for each compared scalar, so a mutation can flip it away from
#: whatever the corpus states without knowing which value that was.
_OTHER_SCALARS = {
    "error_code": "not_a_frozen_code",
    "retry_class": "not_a_frozen_class",
    "compatibility_status": "not_a_frozen_status",
}


@pytest.fixture(scope="module")
def report() -> CorpusReport:
    return run_corpus(CORPUS)


def _outcome(case: ConformanceCase) -> HookOutcome:
    scenario = prepare_vector(case)
    return scenario.provider.handle(scenario.request)


# --- the whole corpus ------------------------------------------------------------


def test_the_whole_corpus_passes_with_exact_totals(report: CorpusReport) -> None:
    assert report.total == len(EXPECTED_CASE_IDS) == 42
    assert report.passed == 42
    assert report.failed == 0
    assert tuple(item.case_id for item in report.reports) == EXPECTED_CASE_IDS
    assert all(item.detail == "" for item in report.reports)


def test_the_observed_dispositions_total_what_the_corpus_states(
    report: CorpusReport,
) -> None:
    totals: dict[Disposition, int] = {}
    for item in report.reports:
        assert item.observed is not None
        totals[item.observed] = totals.get(item.observed, 0) + 1
    assert totals == dict(EXPECTED_DISPOSITION_TOTALS)


def test_running_the_corpus_from_a_path_matches_running_the_loaded_corpus(
    report: CorpusReport,
) -> None:
    assert run_corpus_file(CORPUS_FILE) == report


def test_the_run_is_deterministic() -> None:
    assert run_corpus(CORPUS) == run_corpus(load_corpus(CORPUS_FILE))


@pytest.mark.parametrize("case_id", ALL_IDS)
def test_one_case_at_a_time_produces_the_same_report(
    case_id: str, report: CorpusReport
) -> None:
    single = execute_case(CORPUS.case(case_id))
    assert isinstance(single, CaseReport)
    assert single == next(item for item in report.reports if item.case_id == case_id)


@pytest.mark.parametrize("case_id", ALL_IDS)
def test_the_observed_call_is_made_exactly_once(
    case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def counting_prepare(case: ConformanceCase) -> object:
        scenario = prepare_vector(case)
        handle = scenario.provider.handle

        def wrapper(*args: object, **kwargs: object) -> object:
            calls.append(args)
            return handle(*args, **kwargs)  # type: ignore[arg-type]

        scenario.provider.handle = wrapper  # type: ignore[method-assign, assignment]
        return scenario

    monkeypatch.setattr(conformance_runner, "prepare_vector", counting_prepare)
    execute_case(CORPUS.case(case_id))
    assert len(calls) == 1


# --- the comparator, mutated -----------------------------------------------------


def _mutated_scalar(case: ConformanceCase, name: str) -> ConformanceCase:
    """The same case with one compared scalar moved off what the corpus states."""
    if name == "hook":
        other = next(hook for hook in Hook if hook is not case.hook)
        return replace(case, hook=other)
    if name == "disposition":
        other_disposition = next(
            item for item in Disposition if item is not case.expect.disposition
        )
        return replace(case, expect=replace(case.expect, disposition=other_disposition))
    current = getattr(case.expect, name)
    replacement = None if current is not None else _OTHER_SCALARS[name]
    return replace(case, expect=replace(case.expect, **{name: replacement}))


@pytest.mark.parametrize("name", COMPARED_SCALARS)
@pytest.mark.parametrize("case_id", ALL_IDS)
def test_every_compared_scalar_is_load_bearing(case_id: str, name: str) -> None:
    """Moving any compared scalar off the corpus's value fails, naming that field.

    Both directions are covered across the corpus: the three optional scalars are
    stated by some cases and omitted by others, so `None` is mutated to a value
    on one case and a value to `None` on the next.
    """
    case = CORPUS.case(case_id)
    outcome = _outcome(case)
    assert compare_case(case, outcome).passed

    comparison = compare_case(_mutated_scalar(case, name), outcome)
    assert not comparison.passed
    assert [entry.field for entry in comparison.mismatches] == [name]
    assert name in comparison.detail


@pytest.mark.parametrize("flag", EXPECT_FLAGS)
@pytest.mark.parametrize("case_id", ALL_IDS)
def test_flipping_an_expected_flag_fails_the_case(case_id: str, flag: str) -> None:
    case = CORPUS.case(case_id)
    outcome = _outcome(case)
    flags = dict(case.expect.flags)
    flags[flag] = not flags[flag]
    mutated = replace(case, expect=replace(case.expect, flags=flags))

    comparison = compare_case(mutated, outcome)
    assert [entry.field for entry in comparison.mismatches] == [flag]
    assert comparison.detail == (
        f"{flag}: expected {not case.expect.flags[flag]!r},"
        f" observed {case.expect.flags[flag]!r}"
    )


@pytest.mark.parametrize("flag", EXPECT_FLAGS)
@pytest.mark.parametrize("case_id", ALL_IDS)
def test_flipping_an_observed_flag_fails_the_case(
    case_id: str, flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observation side is load-bearing too, for all ten.

    Five of the ten are structurally false, so an observation that quietly
    stopped deriving them would still be green against the corpus. Inverting one
    derivation at a time is what proves the comparator reads it at all.
    """
    case = CORPUS.case(case_id)
    outcome = _outcome(case)
    derived = OBSERVED_FLAGS[flag]
    monkeypatch.setattr(
        conformance_runner,
        "OBSERVED_FLAGS",
        MappingProxyType(
            {**OBSERVED_FLAGS, flag: lambda item: not derived(item)},
        ),
    )
    comparison = compare_case(case, outcome)
    assert [entry.field for entry in comparison.mismatches] == [flag]


def test_the_ten_flags_are_derived_from_the_outcome_alone() -> None:
    assert tuple(OBSERVED_FLAGS) == EXPECT_FLAGS
    case = CORPUS.case("SPI-V-001")
    outcome = _outcome(case)
    # Every derivation takes the outcome and nothing else, so the same outcome
    # graded against a different case's expectation observes the same values.
    other = CORPUS.case("SPI-V-042")
    assert compare_case(case, outcome).observed is outcome
    first = {flag: OBSERVED_FLAGS[flag](outcome) for flag in EXPECT_FLAGS}
    compare_case(other, outcome)
    assert {flag: OBSERVED_FLAGS[flag](outcome) for flag in EXPECT_FLAGS} == first


def test_a_mismatch_names_every_differing_field_in_a_fixed_order() -> None:
    case = CORPUS.case("SPI-V-001")
    outcome = _outcome(case)
    flags = {flag: not value for flag, value in case.expect.flags.items()}
    mutated = replace(
        case,
        expect=replace(
            case.expect,
            disposition=next(
                item for item in Disposition if item is not case.expect.disposition
            ),
            flags=flags,
        ),
    )
    comparison = compare_case(mutated, outcome)
    assert [entry.field for entry in comparison.mismatches] == [
        "disposition",
        *EXPECT_FLAGS,
    ]
    assert comparison.detail == "; ".join(
        f"{entry.field}: expected {entry.expected}, observed {entry.observed}"
        for entry in comparison.mismatches
    )
    # Deterministic: the same inputs render the same detail, twice.
    assert compare_case(mutated, outcome).detail == comparison.detail


def test_an_ordinary_mismatch_is_reported_rather_than_raised() -> None:
    case = CORPUS.case("SPI-V-001")
    mutated = _mutated_scalar(case, "disposition")
    comparison = compare_case(mutated, _outcome(case))
    assert not comparison.passed
    report = comparison.report()
    assert report.passed is False
    assert report.case_id == case.id
    assert report.detail == comparison.detail


def test_the_comparator_refuses_wrong_argument_types() -> None:
    case = CORPUS.case("SPI-V-001")
    with pytest.raises(ConformanceRunError):
        compare_case("SPI-V-001", _outcome(case))  # type: ignore[arg-type]
    with pytest.raises(ConformanceRunError):
        compare_case(case, "refused")  # type: ignore[arg-type]
    with pytest.raises(ConformanceRunError):
        execute_case(case.id)  # type: ignore[arg-type]


# --- results are immutable -------------------------------------------------------


def test_comparison_results_are_immutable() -> None:
    comparison = compare_case(
        _mutated_scalar(CORPUS.case("SPI-V-001"), "disposition"),
        _outcome(CORPUS.case("SPI-V-001")),
    )
    assert isinstance(comparison, CaseComparison)
    assert isinstance(comparison.mismatches, tuple)
    with pytest.raises((AttributeError, TypeError)):
        comparison.case_id = "SPI-V-002"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        comparison.mismatches[0].field = "hook"  # type: ignore[misc]


def test_a_mismatch_refuses_a_non_string_field() -> None:
    with pytest.raises(TypeError):
        Mismatch(field=1, expected="a", observed="b")  # type: ignore[arg-type]


def test_running_the_corpus_leaves_the_corpus_and_its_cases_unchanged() -> None:
    before = load_corpus(CORPUS_FILE)
    run_corpus(CORPUS)
    assert CORPUS == before
    assert CORPUS.cases == before.cases
    with pytest.raises((AttributeError, TypeError)):
        CORPUS.cases[0].id = "SPI-V-002"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        CORPUS.cases[0].expect.flags["audit_record_required"] = True  # type: ignore[index]


# --- the runner fails closed -----------------------------------------------------


def _corpus_with(cases: tuple[ConformanceCase, ...]) -> ConformanceCorpus:
    return replace(CORPUS, cases=cases)


def test_the_runner_refuses_a_wrong_argument_type() -> None:
    with pytest.raises(ConformanceRunError):
        run_corpus(CASES)  # type: ignore[arg-type]
    with pytest.raises(ConformanceRunError):
        run_corpus(str(CORPUS_FILE))  # type: ignore[arg-type]
    with pytest.raises(ConformanceRunError):
        run_corpus_file(str(CORPUS_FILE))  # type: ignore[arg-type]


def test_the_runner_refuses_a_different_corpus_identity() -> None:
    with pytest.raises(ConformanceRunError):
        run_corpus(replace(CORPUS, fixture_version="1.1.1"))


def test_the_runner_refuses_a_different_hook_taxonomy() -> None:
    with pytest.raises(ConformanceRunError):
        run_corpus(replace(CORPUS, hooks=tuple(reversed(CORPUS.hooks))))


def test_the_runner_refuses_different_disposition_totals() -> None:
    first = CASES[0]
    replacement = next(
        disposition
        for disposition in Disposition
        if disposition is not first.expect.disposition
    )
    changed = replace(
        first,
        expect=replace(first.expect, disposition=replacement),
    )
    with pytest.raises(ConformanceRunError):
        run_corpus(_corpus_with((changed, *CASES[1:])))


def test_the_runner_refuses_a_reordered_corpus() -> None:
    reordered = (CASES[1], CASES[0], *CASES[2:])
    with pytest.raises(ConformanceRunError):
        run_corpus(_corpus_with(reordered))


def test_the_runner_refuses_a_repeated_case() -> None:
    repeated = (CASES[0], *CASES[:-1])
    with pytest.raises(ConformanceRunError):
        run_corpus(_corpus_with(repeated))


def test_the_runner_refuses_an_incomplete_corpus() -> None:
    with pytest.raises(ConformanceRunError):
        run_corpus(_corpus_with(CASES[:-1]))
    with pytest.raises(ConformanceRunError):
        run_corpus(_corpus_with(()))


# --- the architectural properties, asserted against the source -------------------

_RUNNER_SOURCE = (
    REPO_ROOT / "src" / "omnivia_core" / "agent_host" / "conformance_runner.py"
).read_text(encoding="utf-8")
_INPUTS_SOURCE = (
    REPO_ROOT / "src" / "omnivia_core" / "agent_host" / "vector_inputs.py"
).read_text(encoding="utf-8")


def test_the_runner_names_no_case_identifier() -> None:
    """No answer table, and no per-case branch: not even in a comment."""
    assert not re.search(r"SPI-V-\d", _RUNNER_SOURCE)
    assert not re.search(r"\bcase(?:\.|_)id\s*(?:==|!=|in\s*[\({\[])", _RUNNER_SOURCE)


def test_the_runner_does_not_branch_on_the_expected_disposition() -> None:
    assert not re.search(r"expect\.disposition\s*(?:is|==|!=)", _RUNNER_SOURCE)
    assert not re.search(r"Disposition\.[A-Z]", _RUNNER_SOURCE)


def test_the_input_recipes_remain_answer_blind() -> None:
    """`vector_inputs` still cannot see an expectation, so it cannot answer from one."""
    for forbidden in (
        "case.expect",
        "ExpectedOutcome",
        "Disposition",
        "EXPECT_FLAGS",
        "conformance_runner",
    ):
        assert forbidden not in _INPUTS_SOURCE


def test_the_loader_remains_execution_free() -> None:
    source = (
        REPO_ROOT / "src" / "omnivia_core" / "agent_host" / "conformance.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("vector_inputs", "MockProvider", "prepare_vector", ".handle("):
        assert forbidden not in source
