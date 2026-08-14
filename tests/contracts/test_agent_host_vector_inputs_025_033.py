"""The handshake and recovery recipes: `SPI-V-025` to `SPI-V-033`.

The same three halves as the first two slices. Each of the nine scenarios is
prepared and executed against its provider, so a recipe that builds a request
the wrapper or the provider cannot take fails here. Then the corpus fields the
new cases carry -- the declared SPI version, the capabilities negotiation marks
required, the failure a retry's action quotes -- are compared against the
request and the setup that came out of them. Then the hostile ``when`` values
are mistyped, one at a time, and refused.

Two setup shapes are new and are checked as topology rather than as answers:
the five handshake cases and the pre-handshake lifecycle case run against a
provider that has *not* negotiated, and the retry cases run against one that has
a real open turn beneath them.

What is deliberately absent, exactly as in the earlier slices: any assertion
about which answer a case produces. This module is the input side, and the input
side must be gradeable without the answers being visible from it.
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

#: This slice: the six version-and-capability cases and the three recovery
#: cases, in corpus order.
SLICE_IDS = tuple(f"SPI-V-{index:03d}" for index in range(25, 34))

VERSION_IDS = tuple(f"SPI-V-{index:03d}" for index in range(25, 31))
RECOVERY_IDS = tuple(f"SPI-V-{index:03d}" for index in range(31, 34))

#: The six cases prepared against a provider that has not negotiated: the five
#: whose observed call is the handshake, and the one that precedes it.
UNNEGOTIATED_IDS = VERSION_IDS

#: The earlier slices, whose recipes this one must leave exactly as they were.
EARLIER_IDS = tuple(f"SPI-V-{index:03d}" for index in range(1, 25))


def _case(case_id: str) -> ConformanceCase:
    return BY_ID[case_id]


# --- the nine scenarios ----------------------------------------------------------


def test_the_slice_follows_the_earlier_ones_in_corpus_order() -> None:
    assert SUPPORTED_CASE_IDS[: len(EARLIER_IDS) + len(SLICE_IDS)] == (
        EARLIER_IDS + SLICE_IDS
    )


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_case_belongs_to_the_family_this_slice_claims(case_id: str) -> None:
    families = {
        **dict.fromkeys(VERSION_IDS, "version_capability"),
        **dict.fromkeys(RECOVERY_IDS, "retry_recovery"),
    }
    assert _case(case_id).family == families[case_id]


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_every_scenario_executes_and_returns_an_outcome(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    assert isinstance(scenario.provider, MockProvider)
    assert isinstance(scenario.request, SpiRequest)
    before = len(scenario.provider.journal)
    outcome = scenario.provider.handle(scenario.request)
    # Sanity only: it is an outcome, for this hook, and it was journalled.
    assert isinstance(outcome, HookOutcome)
    assert outcome.hook is scenario.request.hook
    assert len(scenario.provider.journal) == before + 1


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_setup_is_the_trace_of_what_really_ran(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    assert len(scenario.provider.journal) == len(scenario.setup)
    assert scenario.case_id == case_id


@pytest.mark.parametrize("case_id", UNNEGOTIATED_IDS)
def test_the_handshake_cases_run_against_an_unnegotiated_provider(
    case_id: str,
) -> None:
    """No handshake happened, and the recipe ran nothing at all beneath the call."""
    scenario = prepare_vector(_case(case_id))
    assert not scenario.provider.negotiated
    assert scenario.provider.selected_spi_version is None
    assert scenario.setup == ()


@pytest.mark.parametrize("case_id", RECOVERY_IDS)
def test_the_recovery_cases_run_against_a_negotiated_provider_with_an_open_turn(
    case_id: str,
) -> None:
    case = _case(case_id)
    scenario = prepare_vector(case)
    assert scenario.provider.negotiated
    assert scenario.setup[0].startswith(Hook.NEGOTIATE.value)
    assert any(
        entry.startswith(Hook.RECALL_BEFORE_TURN.value) for entry in scenario.setup
    )
    assert scenario.provider.open_turns[case.given["run"]] == (
        case.given["turn_ordinal"],
    )


@pytest.mark.parametrize("case_id", RECOVERY_IDS)
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
    assert request.provenance.agent == case.given["agent"]
    assert request.provenance.session == case.given["session"]
    assert request.provenance.run == case.given["run"]
    assert request.granted_capabilities == case.given["granted_capabilities"]
    assert request.turn_ordinal == case.given.get("turn_ordinal")


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_required_capabilities_come_from_the_cases_when_clause(
    case_id: str,
) -> None:
    case = _case(case_id)
    stated = tuple(case.when.get("required_capabilities", ()))
    assert prepare_vector(case).request.required_capabilities == stated


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_declared_version_comes_from_the_cases_when_clause(case_id: str) -> None:
    case = _case(case_id)
    assert prepare_vector(case).request.declared_spi_version == case.when.get(
        "declared_spi_version"
    )


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_stated_key_reaches_the_observed_call(case_id: str) -> None:
    case = _case(case_id)
    assert prepare_vector(case).request.idempotency_key == case.given.get(
        "idempotency_key"
    )


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_moving_a_corpus_field_moves_the_request(case_id: str) -> None:
    case = _case(case_id)
    moved = replace(case, given={**case.given, "caller": "caller-moved"})
    assert prepare_vector(moved).request.caller == "caller-moved"
    assert prepare_vector(case).request.caller == case.given["caller"]


@pytest.mark.parametrize("case_id", VERSION_IDS)
def test_moving_the_declared_version_moves_the_request(case_id: str) -> None:
    case = _case(case_id)
    moved = replace(case, when={**case.when, "declared_spi_version": "1.1.0"})
    assert prepare_vector(moved).request.declared_spi_version == (
        "1.1.0" if case.hook is Hook.NEGOTIATE else None
    )


def test_the_legacy_workspace_condition_is_keyed_from_the_cases_workspace() -> None:
    """`SPI-V-030`'s server is the one that cannot read *this* workspace's format."""
    case = _case("SPI-V-030")
    scenario = prepare_vector(case)
    assert scenario.request.workspace == case.given["workspace"]
    moved = replace(case, given={**case.given, "workspace": "workspace-moved"})
    assert prepare_vector(moved).request.workspace == "workspace-moved"
    assert "format" in case.when["action"]


def test_the_retried_hook_is_the_one_the_action_quotes() -> None:
    """`SPI-V-031` really drove the failed capture, under the case's own key."""
    case = _case("SPI-V-031")
    scenario = prepare_vector(case)
    quoted = case.when["action"]
    assert Hook.CAPTURE_AFTER_TURN.value in quoted
    assert any(
        entry.startswith(Hook.CAPTURE_AFTER_TURN.value) for entry in scenario.setup
    )
    assert scenario.request.idempotency_key == case.given["idempotency_key"]


def test_the_retry_carries_the_prior_failure_the_action_quotes() -> None:
    """`SPI-V-032` names its prior failure only in prose; the recipe reads it there."""
    case = _case("SPI-V-032")
    request = prepare_vector(case).request
    carried = request.intent.prior_failure_code
    assert carried is not None
    assert f"`{carried}`" in case.when["action"]


def test_moving_the_quoted_prior_failure_moves_the_request() -> None:
    case = _case("SPI-V-032")
    moved = replace(case, when={**case.when, "action": "It failed with `conflict`."})
    assert prepare_vector(moved).request.intent.prior_failure_code == "conflict"


def test_the_unknown_future_classification_is_the_recipes_own_sentinel() -> None:
    """The corpus states no spelling for `SPI-V-033`, so the recipe fixes one."""
    case = _case("SPI-V-033")
    request = prepare_vector(case).request
    carried = request.intent.unrecognised_retry_class
    assert carried == vector_inputs._FUTURE_CLASSIFICATION
    assert carried not in case.when["action"]
    assert prepare_vector(case).request == request


# --- the earlier slices are unchanged --------------------------------------------


@pytest.mark.parametrize("case_id", EARLIER_IDS)
def test_the_earlier_slices_still_open_with_a_real_handshake(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    assert scenario.provider.negotiated
    assert scenario.setup[0] == f"{Hook.NEGOTIATE.value}@0"
    assert scenario.request.declared_spi_version is None


@pytest.mark.parametrize("case_id", EARLIER_IDS)
def test_the_earlier_slices_keep_their_stated_run_position(case_id: str) -> None:
    case = _case(case_id)
    default = (
        vector_inputs.DEFAULT_SEQUENCE
        if case_id in tuple(f"SPI-V-{index:03d}" for index in range(1, 10))
        else vector_inputs._PRECONDITIONED_SEQUENCE
    )
    sequence = prepare_vector(case).request.provenance.sequence
    assert sequence == case.given.get("sequence", default)


# --- fail-closed -----------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 1.0, True, ["1.0.0"], {"spi": "1.0.0"}])
def test_a_mistyped_declared_version_is_refused(value: Any) -> None:
    case = _case("SPI-V-026")
    with pytest.raises(VectorInputError, match="declared_spi_version must be a string"):
        prepare_vector(replace(case, when={**case.when, "declared_spi_version": value}))


@pytest.mark.parametrize("value", [7, ["quoted"], {"action": "x"}, True])
def test_a_mistyped_action_is_refused(value: Any) -> None:
    case = _case("SPI-V-032")
    with pytest.raises(VectorInputError, match="action must be a string"):
        prepare_vector(replace(case, when={**case.when, "action": value}))


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("memory.write@9.0", "must be a list"),
        (7, "must be a list"),
        ([7], "must hold only strings"),
    ],
)
def test_a_mistyped_required_capability_list_is_refused(value: Any, match: str) -> None:
    case = _case("SPI-V-028")
    with pytest.raises(VectorInputError, match=match):
        prepare_vector(
            replace(case, when={**case.when, "required_capabilities": value})
        )


@pytest.mark.parametrize("value", [7, None, True, ["idem"]])
def test_a_mistyped_key_is_refused(value: Any) -> None:
    case = _case("SPI-V-031")
    with pytest.raises(VectorInputError, match="idempotency_key must be a string"):
        prepare_vector(replace(case, given={**case.given, "idempotency_key": value}))


def test_this_slice_is_exactly_its_window_of_the_supported_range() -> None:
    """The catalogue is complete, so the slice is a window rather than a bound."""
    assert SUPPORTED_CASE_IDS[24 : 24 + len(SLICE_IDS)] == SLICE_IDS
    assert SUPPORTED_CASE_IDS[24 + len(SLICE_IDS)] == "SPI-V-034"
    assert len(SUPPORTED_CASE_IDS) == 42


def test_an_id_the_catalogue_does_not_hold_is_still_refused() -> None:
    with pytest.raises(VectorInputError, match="outside"):
        prepare_vector(replace(_case("SPI-V-025"), id="SPI-V-999"))


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
def test_the_recipe_reads_nothing_from_the_cases_answer(case_id: str) -> None:
    """Giving a case another case's answer changes no input this module builds."""
    case = _case(case_id)
    other = _case("SPI-V-001" if case_id != "SPI-V-001" else "SPI-V-002")
    blinded = replace(case, expect=other.expect)
    assert prepare_vector(blinded).request == prepare_vector(case).request
    assert prepare_vector(blinded).setup == prepare_vector(case).setup


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_scenario_carries_no_answer_of_its_own(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    fields = set(vars(scenario))
    assert fields == {"case_id", "provider", "request", "setup"}
