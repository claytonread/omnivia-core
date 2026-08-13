"""The first nine vector input recipes: what they build, and what they must not know.

Three halves, really. The first loads the accepted corpus, prepares all nine
scenarios and executes each one against its provider, so a recipe that builds a
request the wrapper or the provider cannot take fails here. The second proves
each recipe read the case rather than a copy of it: every scalar the wrapper
carries is compared against the corpus field it came from. The third is the
separation check -- the recipe module's own source is read and searched for any
sign that it consults the fixed answers.

What is deliberately absent: any assertion about which answer a case produces.
That is the next slice's work, and asserting it here from a module that must not
know it would be asserting it twice from one source.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.agent_host import vector_inputs
from omnivia_core.agent_host.conformance import ConformanceCase, load_corpus
from omnivia_core.agent_host.mock import MockProvider
from omnivia_core.agent_host.spi import (
    RUN_LEVEL_HOOKS,
    Hook,
    HookOutcome,
    SpiRequest,
)
from omnivia_core.agent_host.vector_inputs import (
    SUPPORTED_CASE_IDS,
    VectorInputError,
    VectorScenario,
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
SCOPED = [BY_ID[case_id] for case_id in SUPPORTED_CASE_IDS]

#: Names and spellings that would mean the recipe module reached for a fixed
#: answer: the answer DTO, the answer vocabulary, and the four answer fields.
FORBIDDEN_SOURCE_TOKENS = (
    "ExpectedOutcome",
    "Disposition",
    "disposition",
    "expect",
    "error_code",
    "ERROR_CODE",
    "retry_class",
    "RETRY_CLASS",
    "compatibility_status",
    "COMPATIBILITY_STATUS",
    "audit",
)


def _case(case_id: str) -> ConformanceCase:
    return BY_ID[case_id]


# --- the nine scenarios ----------------------------------------------------------


def test_the_slice_covers_exactly_the_first_nine_cases() -> None:
    assert SUPPORTED_CASE_IDS == tuple(f"SPI-V-{index:03d}" for index in range(1, 10))
    assert [case.id for case in SCOPED] == list(SUPPORTED_CASE_IDS)


@pytest.mark.parametrize("case_id", SUPPORTED_CASE_IDS)
def test_a_scenario_is_a_real_provider_and_a_real_request(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    assert isinstance(scenario, VectorScenario)
    assert scenario.case_id == case_id
    assert isinstance(scenario.provider, MockProvider)
    assert isinstance(scenario.request, SpiRequest)
    # Negotiation is a real precondition, so the provider actually negotiated.
    assert scenario.provider.negotiated
    assert scenario.provider.selected_spi_version is not None


@pytest.mark.parametrize("case_id", SUPPORTED_CASE_IDS)
def test_every_scenario_executes_and_returns_an_outcome(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    before = len(scenario.provider.journal)
    outcome = scenario.provider.handle(scenario.request)
    assert isinstance(outcome, HookOutcome)
    assert outcome.hook is scenario.request.hook
    assert len(scenario.provider.journal) == before + 1


@pytest.mark.parametrize("case_id", SUPPORTED_CASE_IDS)
def test_a_scenario_records_the_preconditions_it_drove(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    assert isinstance(scenario.setup, tuple)
    assert all(isinstance(entry, str) for entry in scenario.setup)
    # Negotiation, plus whatever else the recipe needed, all really executed.
    assert scenario.setup[0].startswith(Hook.NEGOTIATE.value)
    assert len(scenario.provider.journal) == len(scenario.setup)


@pytest.mark.parametrize("case_id", SUPPORTED_CASE_IDS)
def test_preparing_the_same_case_twice_builds_the_same_scenario(case_id: str) -> None:
    first = prepare_vector(_case(case_id))
    second = prepare_vector(_case(case_id))
    assert first.request == second.request
    assert first.setup == second.setup
    assert first.provider is not second.provider


@pytest.mark.parametrize(
    "case_id", [case_id for case_id in SUPPORTED_CASE_IDS if case_id != "SPI-V-003"]
)
def test_a_case_that_needs_state_drove_more_than_negotiation(case_id: str) -> None:
    assert len(prepare_vector(_case(case_id)).setup) > 1


def test_a_case_needing_no_state_drove_negotiation_only() -> None:
    assert len(prepare_vector(_case("SPI-V-003")).setup) == 1


# --- the recipe used the corpus fields -------------------------------------------


@pytest.mark.parametrize("case_id", SUPPORTED_CASE_IDS)
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


@pytest.mark.parametrize("case_id", SUPPORTED_CASE_IDS)
def test_the_request_carries_the_cases_own_position_and_key(case_id: str) -> None:
    case = _case(case_id)
    request = prepare_vector(case).request
    assert request.provenance.sequence == case.given.get("sequence", 0)
    assert request.idempotency_key == case.given.get("idempotency_key")
    if case.hook in RUN_LEVEL_HOOKS:
        assert request.turn_ordinal is None
    else:
        assert request.turn_ordinal == case.given["turn_ordinal"]


def test_the_approval_kind_comes_from_the_cases_when_clause() -> None:
    case = _case("SPI-V-005")
    request = prepare_vector(case).request
    assert request.approval_kind is not None
    assert request.approval_kind.value == case.when["approval_kind"]


def test_a_deadline_the_case_does_not_state_falls_back_to_the_default() -> None:
    case = _case("SPI-V-001")
    assert "deadline_ms" not in case.given
    assert prepare_vector(case).request.deadline_ms == vector_inputs.DEFAULT_DEADLINE_MS


def test_a_deadline_the_case_states_is_the_one_used() -> None:
    case = replace(
        _case("SPI-V-001"), given={**_case("SPI-V-001").given, "deadline_ms": 900}
    )
    assert prepare_vector(case).request.deadline_ms == 900


@pytest.mark.parametrize("case_id", SUPPORTED_CASE_IDS)
def test_moving_a_corpus_field_moves_the_request(case_id: str) -> None:
    """The values are read, not restated: change one and the request changes."""
    case = _case(case_id)
    moved = replace(case, given={**case.given, "caller": "caller-moved"})
    assert prepare_vector(moved).request.caller == "caller-moved"
    assert prepare_vector(case).request.caller == case.given["caller"]


# --- fail-closed -----------------------------------------------------------------


@pytest.mark.parametrize("case_id", ["SPI-V-010", "SPI-V-030", "SPI-V-042"])
def test_a_case_outside_the_slice_is_refused(case_id: str) -> None:
    with pytest.raises(VectorInputError, match="outside"):
        prepare_vector(_case(case_id))


@pytest.mark.parametrize("value", ["SPI-V-001", None, {"id": "SPI-V-001"}, 1])
def test_something_that_is_not_a_case_is_refused(value: Any) -> None:
    with pytest.raises(VectorInputError, match="must be a ConformanceCase"):
        prepare_vector(value)


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("caller", 7, "caller must be a string"),
        ("agent", None, "agent must be a string"),
        ("session", ["session-alpha"], "session must be a string"),
        ("run", 1.5, "run must be a string"),
        ("workspace", {}, "workspace must be a string"),
        ("purpose", 3, "purpose must be a string"),
        ("idempotency_key", 9, "idempotency_key must be a string"),
        ("sequence", "12", "sequence must be an integer"),
        ("sequence", True, "sequence must be an integer"),
        ("turn_ordinal", "3", "turn_ordinal must be an integer"),
        ("deadline_ms", "900", "deadline_ms must be an integer"),
        ("granted_capabilities", "memory.write@1.0", "must be a list"),
        ("granted_capabilities", [7], "must hold only strings"),
    ],
)
def test_a_mistyped_given_field_is_refused(key: str, value: Any, match: str) -> None:
    case = _case("SPI-V-001")
    with pytest.raises(VectorInputError, match=match):
        prepare_vector(replace(case, given={**case.given, key: value}))


@pytest.mark.parametrize("key", ["caller", "agent", "session", "run", "workspace"])
def test_a_missing_given_field_is_refused(key: str) -> None:
    case = _case("SPI-V-001")
    given = {name: item for name, item in case.given.items() if name != key}
    with pytest.raises(VectorInputError, match=f"{key} must be a string"):
        prepare_vector(replace(case, given=given))


def test_a_mistyped_approval_kind_is_refused() -> None:
    case = _case("SPI-V-005")
    with pytest.raises(VectorInputError, match="approval_kind must be a string"):
        prepare_vector(replace(case, when={**case.when, "approval_kind": 4}))


def test_an_unknown_approval_kind_is_refused() -> None:
    case = _case("SPI-V-005")
    with pytest.raises(VectorInputError, match="unknown approval kind"):
        prepare_vector(
            replace(case, when={**case.when, "approval_kind": "rubber_stamp"})
        )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"case_id": "SPI-V-042"}, "has no recipe"),
        ({"provider": "a provider"}, "provider must be a MockProvider"),
        ({"request": None}, "request must be an SpiRequest"),
        ({"setup": "spi.negotiate@0"}, "setup must be a sequence"),
        ({"setup": (1,)}, "setup must hold only strings"),
    ],
)
def test_a_scenario_refuses_a_malformed_field(
    overrides: dict[str, Any], match: str
) -> None:
    scenario = prepare_vector(_case("SPI-V-001"))
    fields: dict[str, Any] = {
        "case_id": scenario.case_id,
        "provider": scenario.provider,
        "request": scenario.request,
        "setup": scenario.setup,
        **overrides,
    }
    with pytest.raises(VectorInputError, match=match):
        VectorScenario(**fields)


def test_a_scenario_cannot_be_edited_after_it_is_built() -> None:
    scenario = prepare_vector(_case("SPI-V-001"))
    with pytest.raises(AttributeError):
        scenario.request = None  # type: ignore[misc]
    assert isinstance(scenario.setup, tuple)


# --- the recipes do not know the answers -----------------------------------------


def _module_source() -> str:
    return Path(vector_inputs.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("token", FORBIDDEN_SOURCE_TOKENS)
def test_the_recipe_module_never_spells_an_answer_lookup(token: str) -> None:
    assert token not in _module_source()


def test_the_recipe_module_imports_no_answer_type() -> None:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(_module_source())):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
    assert "ExpectedOutcome" not in imported
    assert "Disposition" not in imported
    assert "Reason" not in imported


def test_the_recipe_module_binds_no_answer_name() -> None:
    namespace = vars(vector_inputs)
    assert "ExpectedOutcome" not in namespace
    assert "Disposition" not in namespace
    assert "Reason" not in namespace


def test_the_corpus_file_was_only_ever_read() -> None:
    assert len(CASES) == 42
    assert CORPUS_FILE.is_file()
