"""The denial, cancellation and deadline recipes: `SPI-V-010` to `SPI-V-024`.

The same three halves as the first slice. Each of the fifteen scenarios is
prepared and executed against its provider, so a recipe that builds a request
the wrapper or the provider cannot take fails here. Then the corpus fields the
new cases carry -- required capabilities, elapsed time, the deadline a case
omits -- are compared against the request that came out of them. Then the
hostile ``when`` values are mistyped, one at a time, and refused.

What is deliberately absent, exactly as in the first slice: any assertion about
which answer a case produces. This module is the input side, and the input side
must be gradeable without the answers being visible from it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.agent_host import vector_inputs
from omnivia_core.agent_host.conformance import ConformanceCase, load_corpus
from omnivia_core.agent_host.mock import MockProvider
from omnivia_core.agent_host.spi import (
    Hook,
    HookOutcome,
    SpiRequest,
)
from omnivia_core.agent_host.vector_inputs import (
    SUPPORTED_CASE_IDS,
    VectorInputError,
    prepare_vector,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_FILE = (
    REPO_ROOT
    / "docs"
    / "quality"
    / "fixtures"
    / "core-agent-host-provider-spi"
    / "provider-spi-cases.json"
)

CASES = load_corpus(CORPUS_FILE).cases
BY_ID = {case.id: case for case in CASES}

#: This slice: the seven denial cases, the four cancellation cases and the four
#: deadline cases, in corpus order.
SLICE_IDS = tuple(f"SPI-V-{index:03d}" for index in range(10, 25))

DENIAL_IDS = tuple(f"SPI-V-{index:03d}" for index in range(10, 17))
CANCELLATION_IDS = tuple(f"SPI-V-{index:03d}" for index in range(17, 21))
DEADLINE_IDS = tuple(f"SPI-V-{index:03d}" for index in range(21, 25))


def _case(case_id: str) -> ConformanceCase:
    return BY_ID[case_id]


# --- the fifteen scenarios -------------------------------------------------------


def test_the_slice_is_part_of_the_supported_range() -> None:
    assert set(SLICE_IDS) <= set(SUPPORTED_CASE_IDS)
    # The exact window this slice occupies: the nine before it, then these.
    assert SUPPORTED_CASE_IDS[9 : 9 + len(SLICE_IDS)] == SLICE_IDS


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_case_belongs_to_the_family_this_slice_claims(case_id: str) -> None:
    families = {
        **dict.fromkeys(DENIAL_IDS, "denial"),
        **dict.fromkeys(CANCELLATION_IDS, "cancellation"),
        **dict.fromkeys(DEADLINE_IDS, "deadline"),
    }
    assert _case(case_id).family == families[case_id]


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_every_scenario_executes_and_returns_an_outcome(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    assert isinstance(scenario.provider, MockProvider)
    assert isinstance(scenario.request, SpiRequest)
    assert scenario.provider.negotiated
    before = len(scenario.provider.journal)
    outcome = scenario.provider.handle(scenario.request)
    # Sanity only: it is an outcome, for this hook, and it was journalled.
    assert isinstance(outcome, HookOutcome)
    assert outcome.hook is scenario.request.hook
    assert len(scenario.provider.journal) == before + 1


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_every_scenario_drove_real_preconditions(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    assert scenario.setup[0].startswith(Hook.NEGOTIATE.value)
    # Negotiation, plus at least the call that puts a live turn underneath.
    assert len(scenario.setup) > 1
    assert len(scenario.provider.journal) == len(scenario.setup)


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_every_precondition_sits_below_the_observed_call(case_id: str) -> None:
    """The run's positions are strictly increasing, so the ladder is ordered."""
    scenario = prepare_vector(_case(case_id))
    positions = [int(entry.split("@")[1].split("#")[0]) for entry in scenario.setup]
    assert positions == sorted(positions)
    assert positions[-1] < scenario.request.provenance.sequence


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_preparing_the_same_case_twice_builds_the_same_scenario(case_id: str) -> None:
    first = prepare_vector(_case(case_id))
    second = prepare_vector(_case(case_id))
    assert first.request == second.request
    assert first.setup == second.setup
    assert first.provider is not second.provider


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_executing_the_same_case_twice_journals_the_same_hooks(case_id: str) -> None:
    hooks: list[tuple[Hook, ...]] = []
    for _ in range(2):
        scenario = prepare_vector(_case(case_id))
        scenario.provider.handle(scenario.request)
        hooks.append(tuple(outcome.hook for outcome in scenario.provider.journal))
    assert hooks[0] == hooks[1]


# --- the recipe used the corpus fields -------------------------------------------


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_request_carries_the_cases_own_identity(case_id: str) -> None:
    case = _case(case_id)
    request = prepare_vector(case).request
    assert request.hook is case.hook
    assert request.caller == case.given["caller"]
    assert request.workspace == case.given["workspace"]
    assert request.purpose == case.given["purpose"]
    assert request.provenance.run == case.given["run"]
    assert request.granted_capabilities == case.given["granted_capabilities"]
    assert request.turn_ordinal == case.given["turn_ordinal"]


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_required_capabilities_come_from_the_cases_when_clause(
    case_id: str,
) -> None:
    case = _case(case_id)
    stated = tuple(case.when.get("required_capabilities", ()))
    assert prepare_vector(case).request.required_capabilities == stated


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_elapsed_time_comes_from_the_cases_when_clause(case_id: str) -> None:
    case = _case(case_id)
    assert prepare_vector(case).request.elapsed_ms == case.when.get("elapsed_ms", 0)


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_deadline_comes_from_the_case_or_from_the_named_default(
    case_id: str,
) -> None:
    case = _case(case_id)
    deadline = prepare_vector(case).request.deadline_ms
    if case_id == "SPI-V-024":
        # The one case whose `when` says the adapter omitted it entirely.
        assert "deadline_ms" not in case.given
        assert deadline is None
    else:
        assert deadline == case.given.get(
            "deadline_ms", vector_inputs.DEFAULT_DEADLINE_MS
        )


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_moving_a_corpus_field_moves_the_request(case_id: str) -> None:
    case = _case(case_id)
    moved = replace(case, given={**case.given, "caller": "caller-moved"})
    assert prepare_vector(moved).request.caller == "caller-moved"
    assert prepare_vector(case).request.caller == case.given["caller"]


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_moving_the_stated_capabilities_moves_the_request(case_id: str) -> None:
    case = _case(case_id)
    moved = replace(
        case, when={**case.when, "required_capabilities": ["job.control@1.0"]}
    )
    assert prepare_vector(moved).request.required_capabilities == ("job.control@1.0",)


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_moving_the_stated_elapsed_time_moves_the_request(case_id: str) -> None:
    case = _case(case_id)
    moved = replace(case, when={**case.when, "elapsed_ms": 11})
    assert prepare_vector(moved).request.elapsed_ms == 11


def test_the_hostile_composition_is_the_one_the_action_describes() -> None:
    """`SPI-V-020` asks turn control to take a Core durable job with it."""
    request = prepare_vector(_case("SPI-V-020")).request
    assert request.intent.compose_core_job_control
    assert "durable job" in _case("SPI-V-020").when["action"]


def test_the_governance_promotion_is_the_one_the_action_describes() -> None:
    """`SPI-V-015` asks capture for a governed record."""
    request = prepare_vector(_case("SPI-V-015")).request
    assert request.intent.promote_to_governed_knowledge
    assert "governed record" in _case("SPI-V-015").when["action"]


def test_the_nested_deadline_is_measured_against_a_parent() -> None:
    """`SPI-V-023` nests inside a parent recall with time left on it."""
    request = prepare_vector(_case("SPI-V-023")).request
    assert request.parent_remaining_ms == vector_inputs._PARENT_REMAINING_MS
    assert "nested inside a recall" in _case("SPI-V-023").when["action"]


def test_the_rebinding_case_was_bound_to_another_workspace_first() -> None:
    """`SPI-V-011`'s run really is bound elsewhere before the observed call."""
    scenario = prepare_vector(_case("SPI-V-011"))
    assert scenario.request.workspace == _case("SPI-V-011").given["workspace"]
    assert scenario.provider.open_turns[_case("SPI-V-011").given["run"]]


def test_the_repeat_cancellation_ran_a_first_cancellation() -> None:
    scenario = prepare_vector(_case("SPI-V-018"))
    assert _case("SPI-V-018").when["repeat_of"] == "SPI-V-017"
    assert any(entry.startswith(Hook.TURN_CANCEL.value) for entry in scenario.setup)


def test_the_capture_race_ran_a_capture_first() -> None:
    scenario = prepare_vector(_case("SPI-V-019"))
    assert any(
        entry.startswith(Hook.CAPTURE_AFTER_TURN.value) for entry in scenario.setup
    )


# --- fail-closed -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("job.control@1.0", "must be a list"),
        (7, "must be a list"),
        ([7], "must hold only strings"),
    ],
)
def test_a_mistyped_required_capability_list_is_refused(value: Any, match: str) -> None:
    case = _case("SPI-V-012")
    with pytest.raises(VectorInputError, match=match):
        prepare_vector(
            replace(case, when={**case.when, "required_capabilities": value})
        )


@pytest.mark.parametrize("value", ["2000", True, 1.5, None])
def test_a_mistyped_elapsed_time_is_refused(value: Any) -> None:
    case = _case("SPI-V-021")
    with pytest.raises(VectorInputError, match="elapsed_ms must be an integer"):
        prepare_vector(replace(case, when={**case.when, "elapsed_ms": value}))


def test_this_slice_is_exactly_its_window_of_the_supported_range() -> None:
    """The catalogue is complete, so the slice is a window rather than a bound."""
    assert SUPPORTED_CASE_IDS[9 : 9 + len(SLICE_IDS)] == SLICE_IDS
    assert SUPPORTED_CASE_IDS[9 + len(SLICE_IDS)] == "SPI-V-025"
    assert len(SUPPORTED_CASE_IDS) == 42


def test_an_id_the_catalogue_does_not_hold_is_still_refused() -> None:
    with pytest.raises(VectorInputError, match="outside"):
        prepare_vector(replace(_case("SPI-V-010"), id="SPI-V-999"))


# --- the recipes still do not know the answers -----------------------------------


def test_the_recipe_module_never_spells_an_answer_lookup() -> None:
    source = Path(vector_inputs.__file__).read_text(encoding="utf-8")
    for token in (
        "ExpectedOutcome",
        "Disposition",
        "disposition",
        "Reason",
        "ERROR_CODE",
        "RETRY_CLASS",
        "compatibility_status",
        "COMPATIBILITY_STATUS",
        "audit",
        "boundary_",
    ):
        assert token not in source


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_scenario_carries_no_answer_of_its_own(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    fields = set(vars(scenario))
    assert fields == {"case_id", "provider", "request", "setup"}
