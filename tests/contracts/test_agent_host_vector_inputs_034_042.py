"""The boundary recipes: `SPI-V-034` to `SPI-V-042`, closing the catalogue.

The same three halves as the earlier slices. Each of the nine scenarios is
prepared and executed against its provider, so a recipe that builds a request
the wrapper or the provider cannot take fails here. Then the corpus fields the
new cases carry -- the approval kind, the capabilities the escalation marks
required, the ordinal the completed turn sits at -- are compared against the
request and the setup that came out of them. Then the hostile ``when`` and
``given`` values are mistyped, one at a time, and refused.

Four of these cases state their condition only as prose: a hook that can be
satisfied only by patching host source, an adapter opening the workspace store
itself, a payload inlined past the declared ceiling, and host identity injected
into a nested envelope. Each is carried by an intent flag the recipe chooses,
and each such flag is tied back here to the sentence in ``when.action`` that
asked for it -- so a recipe cannot quietly declare an intent the corpus never
described.

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
from omnivia_core.agent_host.mock import MockProvider, ProviderProfile
from omnivia_core.agent_host.spi import (
    ApprovalKind,
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

#: This slice: the last nine cases, in corpus order.
SLICE_IDS = tuple(f"SPI-V-{index:03d}" for index in range(34, 43))

#: The whole catalogue, which this slice completes.
ALL_IDS = tuple(f"SPI-V-{index:03d}" for index in range(1, 43))

#: The earlier slices, whose recipes this one must leave exactly as they were.
EARLIER_IDS = tuple(f"SPI-V-{index:03d}" for index in range(1, 34))

#: The one case here whose observed call is the handshake itself, so no
#: handshake runs beneath it.
UNNEGOTIATED_IDS = ("SPI-V-034",)

#: Cases whose precondition is a real turn. The run-level compaction in 036
#: needs only the ordinary negotiation beneath it.
TURN_SETUP_IDS = tuple(
    entry for entry in SLICE_IDS if entry not in (*UNNEGOTIATED_IDS, "SPI-V-036")
)

#: The cases whose condition the corpus states only as prose, paired with the
#: intent field the recipe uses to carry it.
PROSE_CONDITION_FIELDS = {
    "SPI-V-034": "requires_host_source_patch",
    "SPI-V-035": "direct_storage_access",
    "SPI-V-038": "inline_payload_bytes",
    "SPI-V-042": "inject_host_identity",
}


def _case(case_id: str) -> ConformanceCase:
    return BY_ID[case_id]


# --- the nine scenarios ----------------------------------------------------------


def test_the_slice_closes_the_supported_range() -> None:
    assert SUPPORTED_CASE_IDS == ALL_IDS
    assert SUPPORTED_CASE_IDS[-len(SLICE_IDS) :] == SLICE_IDS
    assert tuple(case.id for case in CASES) == ALL_IDS


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_case_belongs_to_the_family_the_corpus_gives_it(case_id: str) -> None:
    families = {
        "SPI-V-034": "boundary",
        "SPI-V-035": "boundary",
        "SPI-V-036": "boundary",
        "SPI-V-037": "boundary",
        "SPI-V-038": "boundary",
        "SPI-V-039": "reordered_callback",
        "SPI-V-040": "retry_recovery",
        "SPI-V-041": "denial",
        "SPI-V-042": "boundary",
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
def test_the_handshake_case_runs_against_an_unnegotiated_provider(
    case_id: str,
) -> None:
    scenario = prepare_vector(_case(case_id))
    assert not scenario.provider.negotiated
    assert scenario.provider.selected_spi_version is None
    assert scenario.setup == ()


@pytest.mark.parametrize("case_id", TURN_SETUP_IDS)
def test_the_other_cases_run_against_a_negotiated_provider_with_a_real_turn(
    case_id: str,
) -> None:
    scenario = prepare_vector(_case(case_id))
    assert scenario.provider.negotiated
    assert scenario.setup[0] == f"{Hook.NEGOTIATE.value}@0"
    assert any(
        entry.startswith(Hook.RECALL_BEFORE_TURN.value) for entry in scenario.setup
    )


def test_the_setup_topology_is_exactly_what_each_recipe_claims() -> None:
    """The whole ladder, per case: what ran, at which position, in which turn."""
    expected = {
        "SPI-V-034": (),
        "SPI-V-035": ("spi.negotiate@0", "recall.before_turn@3#turn1"),
        "SPI-V-036": ("spi.negotiate@0",),
        "SPI-V-037": (
            "spi.negotiate@0",
            "recall.before_turn@9#turn4",
            "context.compact@10",
        ),
        "SPI-V-038": ("spi.negotiate@0", "recall.before_turn@3#turn1"),
        "SPI-V-039": (
            "spi.negotiate@0",
            "recall.before_turn@39#turn7",
            "turn.complete@40#turn7",
        ),
        "SPI-V-040": ("spi.negotiate@0", "recall.before_turn@3#turn3"),
        "SPI-V-041": ("spi.negotiate@0", "recall.before_turn@3#turn6"),
        "SPI-V-042": ("spi.negotiate@0", "recall.before_turn@3#turn2"),
    }
    assert {
        case_id: prepare_vector(_case(case_id)).setup for case_id in SLICE_IDS
    } == expected


@pytest.mark.parametrize(
    "case_id", tuple(entry for entry in SLICE_IDS if entry not in UNNEGOTIATED_IDS)
)
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
def test_the_run_position_is_the_stated_one_or_the_slices_default(
    case_id: str,
) -> None:
    case = _case(case_id)
    sequence = prepare_vector(case).request.provenance.sequence
    assert sequence == case.given.get(
        "sequence", vector_inputs._PRECONDITIONED_SEQUENCE
    )


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_required_capabilities_come_from_the_cases_when_clause(
    case_id: str,
) -> None:
    case = _case(case_id)
    stated = tuple(case.when.get("required_capabilities", ()))
    assert prepare_vector(case).request.required_capabilities == stated


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_stated_key_reaches_the_observed_call(case_id: str) -> None:
    case = _case(case_id)
    assert prepare_vector(case).request.idempotency_key == case.given.get(
        "idempotency_key"
    )


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_the_declared_version_comes_from_the_cases_when_clause(case_id: str) -> None:
    case = _case(case_id)
    expected = (
        case.when.get("declared_spi_version") if case.hook is Hook.NEGOTIATE else None
    )
    assert prepare_vector(case).request.declared_spi_version == expected


@pytest.mark.parametrize("case_id", SLICE_IDS)
def test_moving_a_corpus_field_moves_the_request(case_id: str) -> None:
    case = _case(case_id)
    moved = replace(case, given={**case.given, "caller": "caller-moved"})
    assert prepare_vector(moved).request.caller == "caller-moved"
    assert prepare_vector(case).request.caller == case.given["caller"]


def test_the_escalations_approval_kind_and_capability_are_the_corpus_fields() -> None:
    """`SPI-V-041` states both as structured fields, so the recipe states neither."""
    case = _case("SPI-V-041")
    request = prepare_vector(case).request
    assert request.approval_kind is ApprovalKind(case.when["approval_kind"])
    assert request.approval_kind is ApprovalKind.CORE_GOVERNANCE_ESCALATION
    assert request.required_capabilities == tuple(case.when["required_capabilities"])
    moved = replace(case, when={**case.when, "approval_kind": "runtime_tool_approval"})
    assert prepare_vector(moved).request.approval_kind is (
        ApprovalKind.RUNTIME_TOOL_APPROVAL
    )


def test_the_retry_requires_the_job_capability_the_corpus_marks_required() -> None:
    case = _case("SPI-V-040")
    request = prepare_vector(case).request
    assert request.required_capabilities == tuple(case.when["required_capabilities"])
    assert request.intent.compose_core_job_control is True
    assert "retry the Core durable job" in case.when["action"]


def test_the_completed_turn_is_the_ordinal_below_the_one_observed() -> None:
    """`SPI-V-039`: ordinal 7 really completed before ordinal 8 opens, in one run."""
    case = _case("SPI-V-039")
    scenario = prepare_vector(case)
    earlier = case.given["turn_ordinal"] - 1
    assert f"ordinal {earlier} completed" in case.when["action"]
    assert scenario.setup[1:] == (
        f"{Hook.RECALL_BEFORE_TURN.value}@{case.given['sequence'] - 2}#turn{earlier}",
        f"{Hook.TURN_COMPLETE.value}@{case.given['sequence'] - 1}#turn{earlier}",
    )
    assert scenario.request.turn_ordinal == case.given["turn_ordinal"]
    assert scenario.provider.open_turns[case.given["run"]] == ()


def test_moving_the_observed_ordinal_moves_the_completed_one() -> None:
    case = _case("SPI-V-039")
    moved = replace(case, given={**case.given, "turn_ordinal": 4})
    assert prepare_vector(moved).setup[1].endswith("#turn3")


def test_the_compaction_beneath_the_promotion_capture_really_ran() -> None:
    """`SPI-V-037`: a host-local compaction, then the capture that offers its summary."""
    case = _case("SPI-V-037")
    scenario = prepare_vector(case)
    assert "compaction" in case.when["action"]
    assert scenario.setup[-1] == (
        f"{Hook.CONTEXT_COMPACT.value}@{case.given['sequence'] - 1}"
    )
    compacted = scenario.provider.journal[-1]
    assert compacted.hook is Hook.CONTEXT_COMPACT
    assert scenario.request.hook is Hook.CAPTURE_AFTER_TURN
    assert scenario.request.intent.promote_to_governed_knowledge is True


def test_the_run_state_request_is_what_the_compaction_case_asks_for() -> None:
    case = _case("SPI-V-036")
    request = prepare_vector(case).request
    assert request.intent.request_core_run_state is True
    assert "task graph" in case.when["action"]
    assert request.turn_ordinal is None


@pytest.mark.parametrize("case_id", tuple(PROSE_CONDITION_FIELDS))
def test_a_prose_only_condition_is_tied_to_the_action_that_states_it(
    case_id: str,
) -> None:
    """The recipe may spell these, but only because `when.action` describes them."""
    case = _case(case_id)
    value = getattr(
        prepare_vector(case).request.intent, PROSE_CONDITION_FIELDS[case_id]
    )
    assert value not in (False, 0, None)
    phrases = {
        "SPI-V-034": "editing the agent host's own source tree",
        "SPI-V-035": "opens the workspace store itself",
        "SPI-V-038": "above the declared byte ceiling",
        "SPI-V-042": "adds agent, session, run and turn-ordinal fields",
    }
    assert phrases[case_id] in case.when["action"]


def test_the_inline_payload_is_one_byte_past_the_profiles_own_ceiling() -> None:
    """`SPI-V-038` states no number, so the bound comes off `ProviderProfile`."""
    case = _case("SPI-V-038")
    inlined = prepare_vector(case).request.intent.inline_payload_bytes
    assert inlined == ProviderProfile().max_inline_result_bytes + 1
    assert str(inlined) not in case.when["action"]
    assert prepare_vector(case).request.intent.content_reference is None


# --- the earlier slices are unchanged --------------------------------------------


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


@pytest.mark.parametrize("case_id", EARLIER_IDS)
def test_the_earlier_slices_still_open_the_way_they_did(case_id: str) -> None:
    scenario = prepare_vector(_case(case_id))
    negotiated = (
        case_id not in ("SPI-V-025",) and _case(case_id).hook is not Hook.NEGOTIATE
    )
    assert scenario.provider.negotiated is negotiated
    assert (scenario.setup[:1] == (f"{Hook.NEGOTIATE.value}@0",)) is negotiated


# --- fail-closed -----------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 1.0, True, ["1.0.0"], {"spi": "1.0.0"}])
def test_a_mistyped_declared_version_is_refused(value: Any) -> None:
    case = _case("SPI-V-034")
    with pytest.raises(VectorInputError, match="declared_spi_version must be a string"):
        prepare_vector(replace(case, when={**case.when, "declared_spi_version": value}))


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("knowledge.govern@1.0", "must be a list"),
        (7, "must be a list"),
        ([7], "must hold only strings"),
    ],
)
def test_a_mistyped_required_capability_list_is_refused(value: Any, match: str) -> None:
    case = _case("SPI-V-041")
    with pytest.raises(VectorInputError, match=match):
        prepare_vector(
            replace(case, when={**case.when, "required_capabilities": value})
        )


@pytest.mark.parametrize("value", [7, True, ["core_governance_escalation"], 1.0])
def test_a_mistyped_approval_kind_is_refused(value: Any) -> None:
    case = _case("SPI-V-041")
    with pytest.raises(VectorInputError, match="approval_kind must be a string"):
        prepare_vector(replace(case, when={**case.when, "approval_kind": value}))


def test_an_unknown_approval_kind_is_refused() -> None:
    case = _case("SPI-V-041")
    with pytest.raises(VectorInputError, match="unknown approval kind"):
        prepare_vector(replace(case, when={**case.when, "approval_kind": "board_vote"}))


@pytest.mark.parametrize("value", ["8", True, 1.5, None, [8]])
def test_a_mistyped_turn_ordinal_is_refused(value: Any) -> None:
    case = _case("SPI-V-039")
    with pytest.raises(VectorInputError, match="turn_ordinal must be an integer"):
        prepare_vector(replace(case, given={**case.given, "turn_ordinal": value}))


@pytest.mark.parametrize("value", ["11", True, 1.5, None])
def test_a_mistyped_sequence_is_refused(value: Any) -> None:
    case = _case("SPI-V-037")
    with pytest.raises(VectorInputError, match="sequence must be an integer"):
        prepare_vector(replace(case, given={**case.given, "sequence": value}))


@pytest.mark.parametrize("value", [7, None, True, ["idem"]])
def test_a_mistyped_key_is_refused(value: Any) -> None:
    case = _case("SPI-V-040")
    with pytest.raises(VectorInputError, match="idempotency_key must be a string"):
        prepare_vector(replace(case, given={**case.given, "idempotency_key": value}))


@pytest.mark.parametrize("value", [7, None, True, ["workspace-theta"]])
def test_a_mistyped_workspace_is_refused(value: Any) -> None:
    case = _case("SPI-V-035")
    with pytest.raises(VectorInputError, match="workspace must be a string"):
        prepare_vector(replace(case, given={**case.given, "workspace": value}))


@pytest.mark.parametrize("value", ["SPI-V-042", None, {"id": "SPI-V-042"}, 1])
def test_something_that_is_not_a_case_is_refused(value: Any) -> None:
    with pytest.raises(VectorInputError, match="must be a ConformanceCase"):
        prepare_vector(value)


def test_a_case_id_the_catalogue_does_not_hold_is_refused() -> None:
    case = replace(_case("SPI-V-042"), id="SPI-V-999")
    with pytest.raises(VectorInputError, match="outside"):
        prepare_vector(case)


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
        "case.expect",
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
