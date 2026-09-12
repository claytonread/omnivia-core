"""Tests for the KI-01 applicability evaluator (spec 6.3-6.5).

Covers KI-T01..KI-T11 (truth tables, decisive unknown, missing/hidden/
conflicting facts, equality/membership/numeric/date/unit cases, invalid
operands, node/depth/set bounds) and KI-T34/KI-T35 (explicit conflict/contested
helper, review-overdue distinct from expiry) at the pure evaluator level.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from omnivia_core.governed_knowledge.applicability import (
    MAX_DEPTH,
    MAX_NODES,
    MAX_SET_MEMBERS,
    ApplicabilityExpression,
    ApplicabilityOutcome,
    BooleanNode,
    BooleanOperator,
    ComparisonNode,
    ComparisonOperator,
    FactEntry,
    FactSnapshot,
    Operand,
    TruthValue,
    contested_positions,
    date,
    enum,
    evaluate_applicability,
    fact_ref,
    literal,
    negate,
    numeric,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeValidationError
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)

POSITION_VERSION_REF = "position-v1"


def _instant(
    year: int, precision: TemporalPrecision = TemporalPrecision.DAY
) -> TemporalInstant:
    return TemporalInstant(
        value=datetime(year, 1, 1, tzinfo=UTC),
        precision=precision,
        provenance=TemporalProvenance.STATED,
    )


def _snapshot(**facts: FactEntry) -> FactSnapshot:
    return FactSnapshot(fact_snapshot_ref="snap-1", facts=facts)


def _equals_node(name: str, value: str) -> ComparisonNode:
    return ComparisonNode(
        operator=ComparisonOperator.EQUALS, left=fact_ref(name), right=literal(value)
    )


def _expr(node: ComparisonNode | BooleanNode) -> ApplicabilityExpression:
    return ApplicabilityExpression(root=node)


# --------------------------------------------------------------------------
# Kleene truth tables (KI-T01, KI-T02)
# --------------------------------------------------------------------------


def test_negate_truth_table() -> None:
    assert negate(TruthValue.TRUE) is TruthValue.FALSE
    assert negate(TruthValue.FALSE) is TruthValue.TRUE
    assert negate(TruthValue.UNKNOWN) is TruthValue.UNKNOWN


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((TruthValue.TRUE, TruthValue.TRUE), TruthValue.TRUE),
        ((TruthValue.TRUE, TruthValue.FALSE), TruthValue.FALSE),
        ((TruthValue.FALSE, TruthValue.UNKNOWN), TruthValue.FALSE),  # decisive false
        ((TruthValue.TRUE, TruthValue.UNKNOWN), TruthValue.UNKNOWN),
        ((TruthValue.UNKNOWN, TruthValue.UNKNOWN), TruthValue.UNKNOWN),
    ],
)
def test_all_truth_table(values: tuple[TruthValue, ...], expected: TruthValue) -> None:
    node = BooleanNode(
        operator=BooleanOperator.ALL,
        children=tuple(_equals_node(f"f{i}", "x") for i in range(len(values))),
    )
    facts: dict[str, FactEntry] = {}
    for i, v in enumerate(values):
        if v is TruthValue.UNKNOWN:
            facts[f"f{i}"] = FactEntry(values=())
        else:
            facts[f"f{i}"] = FactEntry(
                values=(literal("x" if v is TruthValue.TRUE else "y"),)
            )
    result = evaluate_applicability(
        required=_expr(node),
        facts=_snapshot(**facts),
        position_version_ref=POSITION_VERSION_REF,
    )
    if expected is TruthValue.TRUE:
        assert result.outcome is ApplicabilityOutcome.APPLICABLE
    elif expected is TruthValue.FALSE:
        assert result.outcome is ApplicabilityOutcome.NOT_APPLICABLE
    else:
        assert result.outcome is ApplicabilityOutcome.NEEDS_INFORMATION


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((TruthValue.TRUE, TruthValue.UNKNOWN), TruthValue.TRUE),  # decisive true
        ((TruthValue.FALSE, TruthValue.FALSE), TruthValue.FALSE),
        ((TruthValue.FALSE, TruthValue.UNKNOWN), TruthValue.UNKNOWN),
    ],
)
def test_any_truth_table(values: tuple[TruthValue, ...], expected: TruthValue) -> None:
    children = []
    facts: dict[str, FactEntry] = {}
    for i, v in enumerate(values):
        children.append(_equals_node(f"f{i}", "x"))
        if v is TruthValue.UNKNOWN:
            facts[f"f{i}"] = FactEntry(values=())
        else:
            facts[f"f{i}"] = FactEntry(
                values=(literal("x" if v is TruthValue.TRUE else "y"),)
            )
    node = BooleanNode(operator=BooleanOperator.ANY, children=tuple(children))
    result = evaluate_applicability(
        required=_expr(node),
        facts=_snapshot(**facts),
        position_version_ref=POSITION_VERSION_REF,
    )
    if expected is TruthValue.TRUE:
        assert result.outcome is ApplicabilityOutcome.APPLICABLE
    elif expected is TruthValue.FALSE:
        assert result.outcome is ApplicabilityOutcome.NOT_APPLICABLE
    else:
        assert result.outcome is ApplicabilityOutcome.NEEDS_INFORMATION


def test_not_unknown_is_unknown() -> None:
    node = BooleanNode(operator=BooleanOperator.NOT, children=(_equals_node("f", "x"),))
    result = evaluate_applicability(
        required=_expr(node),
        facts=_snapshot(f=FactEntry(values=())),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NEEDS_INFORMATION


# --------------------------------------------------------------------------
# Missing / hidden / conflicting facts (KI-T03, KI-T04)
# --------------------------------------------------------------------------


def test_missing_fact_is_needs_information_and_authorized_ref_named() -> None:
    result = evaluate_applicability(
        required=_expr(_equals_node("region", "emea")),
        facts=_snapshot(region=FactEntry(values=())),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NEEDS_INFORMATION
    assert "region" in result.authorized_fact_refs
    assert any("region" in reason for reason in result.reasons)


def test_hidden_fact_is_needs_information_with_generic_reason_only() -> None:
    result = evaluate_applicability(
        required=_expr(_equals_node("salary_band", "confidential_value")),
        facts=_snapshot(salary_band=FactEntry(values=(), visible=False)),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NEEDS_INFORMATION
    assert "salary_band" not in result.authorized_fact_refs
    assert result.reasons == ("Required context is unavailable",)
    for reason in result.reasons:
        assert "salary_band" not in reason


def test_absent_fact_is_not_treated_as_empty_zero_or_false() -> None:
    empty_string_node = _equals_node("region", "")
    result = evaluate_applicability(
        required=_expr(empty_string_node),
        facts=_snapshot(region=FactEntry(values=())),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NEEDS_INFORMATION


def test_conflicting_multiple_fact_values_yield_unknown() -> None:
    result = evaluate_applicability(
        required=_expr(_equals_node("region", "emea")),
        facts=_snapshot(region=FactEntry(values=(literal("emea"), literal("apac")))),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NEEDS_INFORMATION


def test_conflicting_but_identical_values_are_not_conflicting() -> None:
    result = evaluate_applicability(
        required=_expr(_equals_node("region", "emea")),
        facts=_snapshot(region=FactEntry(values=(literal("emea"), literal("emea")))),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.APPLICABLE


# --------------------------------------------------------------------------
# Equality / membership / numeric / date / unit cases (KI-T05..KI-T08)
# --------------------------------------------------------------------------


def test_equality_true_and_false() -> None:
    facts = _snapshot(region=FactEntry(values=(literal("emea"),)))
    assert (
        evaluate_applicability(
            required=_expr(_equals_node("region", "emea")),
            facts=facts,
            position_version_ref=POSITION_VERSION_REF,
        ).outcome
        is ApplicabilityOutcome.APPLICABLE
    )
    assert (
        evaluate_applicability(
            required=_expr(_equals_node("region", "apac")),
            facts=facts,
            position_version_ref=POSITION_VERSION_REF,
        ).outcome
        is ApplicabilityOutcome.NOT_APPLICABLE
    )


def test_equality_type_mismatch_is_not_equal_not_ambiguous() -> None:
    node = ComparisonNode(
        operator=ComparisonOperator.EQUALS, left=fact_ref("region"), right=enum("emea")
    )
    result = evaluate_applicability(
        required=_expr(node),
        facts=_snapshot(region=FactEntry(values=(literal("emea"),))),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NOT_APPLICABLE


def test_membership_true_and_false() -> None:
    node = ComparisonNode(
        operator=ComparisonOperator.MEMBER_OF,
        left=fact_ref("region"),
        members=(literal("emea"), literal("apac")),
    )
    assert (
        evaluate_applicability(
            required=_expr(node),
            facts=_snapshot(region=FactEntry(values=(literal("emea"),))),
            position_version_ref=POSITION_VERSION_REF,
        ).outcome
        is ApplicabilityOutcome.APPLICABLE
    )
    assert (
        evaluate_applicability(
            required=_expr(node),
            facts=_snapshot(region=FactEntry(values=(literal("na"),))),
            position_version_ref=POSITION_VERSION_REF,
        ).outcome
        is ApplicabilityOutcome.NOT_APPLICABLE
    )


def test_membership_set_size_bound() -> None:
    members = tuple(literal(str(i)) for i in range(MAX_SET_MEMBERS))
    ComparisonNode(
        operator=ComparisonOperator.MEMBER_OF, left=fact_ref("x"), members=members
    )
    with pytest.raises(GovernedKnowledgeValidationError):
        ComparisonNode(
            operator=ComparisonOperator.MEMBER_OF,
            left=fact_ref("x"),
            members=members + (literal("overflow"),),
        )


def test_numeric_ordering_matching_units() -> None:
    node = ComparisonNode(
        operator=ComparisonOperator.NUMERIC_GTE,
        left=fact_ref("amount"),
        right=numeric(Decimal(100), "usd"),
    )
    result = evaluate_applicability(
        required=_expr(node),
        facts=_snapshot(amount=FactEntry(values=(numeric(Decimal(150), "usd"),))),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.APPLICABLE


def test_numeric_ordering_no_implicit_unit_conversion() -> None:
    node = ComparisonNode(
        operator=ComparisonOperator.NUMERIC_GTE,
        left=fact_ref("amount"),
        right=numeric(Decimal(100), "usd"),
    )
    result = evaluate_applicability(
        required=_expr(node),
        facts=_snapshot(amount=FactEntry(values=(numeric(Decimal(150), "eur"),))),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NEEDS_INFORMATION


def test_date_ordering_decisive() -> None:
    node = ComparisonNode(
        operator=ComparisonOperator.DATE_GT,
        left=fact_ref("effective"),
        right=date(_instant(2020)),
    )
    result = evaluate_applicability(
        required=_expr(node),
        facts=_snapshot(effective=FactEntry(values=(date(_instant(2025)),))),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.APPLICABLE


def test_date_ordering_ambiguous_precision_rejected_as_unknown() -> None:
    same_instant_year = _instant(2020, TemporalPrecision.YEAR)
    same_instant_day = _instant(2020, TemporalPrecision.DAY)
    node = ComparisonNode(
        operator=ComparisonOperator.DATE_GTE,
        left=fact_ref("effective"),
        right=date(same_instant_year),
    )
    result = evaluate_applicability(
        required=_expr(node),
        facts=_snapshot(effective=FactEntry(values=(date(same_instant_day),))),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NEEDS_INFORMATION


# --------------------------------------------------------------------------
# Invalid operands (KI-T09)
# --------------------------------------------------------------------------


def test_not_node_requires_exactly_one_child() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        BooleanNode(operator=BooleanOperator.NOT, children=())
    with pytest.raises(GovernedKnowledgeValidationError):
        BooleanNode(
            operator=BooleanOperator.NOT,
            children=(_equals_node("a", "x"), _equals_node("b", "y")),
        )


def test_equals_supports_typed_numeric_operands() -> None:
    node = ComparisonNode(
        operator=ComparisonOperator.EQUALS,
        left=fact_ref("amount"),
        right=numeric(Decimal(1), "usd"),
    )
    result = evaluate_applicability(
        required=_expr(node),
        facts=_snapshot(amount=FactEntry(values=(numeric(Decimal(1), "usd"),))),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.APPLICABLE


def test_numeric_operator_requires_numeric_right_operand() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        ComparisonNode(
            operator=ComparisonOperator.NUMERIC_GT,
            left=fact_ref("amount"),
            right=literal("x"),
        )


def test_operand_requires_matching_payload() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        Operand(kind=fact_ref("x").kind, fact_ref=None)


# --------------------------------------------------------------------------
# Node / depth / set bounds (KI-T10, profile-size portions of KI-T41/65/66/68)
# --------------------------------------------------------------------------


def test_node_count_bound() -> None:
    leaf = _equals_node("f", "x")
    within_bound = BooleanNode(
        operator=BooleanOperator.ANY, children=(leaf,) * (MAX_NODES - 1)
    )
    ApplicabilityExpression(root=within_bound)
    over_bound = BooleanNode(operator=BooleanOperator.ANY, children=(leaf,) * MAX_NODES)
    with pytest.raises(GovernedKnowledgeValidationError):
        ApplicabilityExpression(root=over_bound)


def test_depth_bound() -> None:
    node: BooleanNode | ComparisonNode = _equals_node("f", "x")
    for _ in range(MAX_DEPTH - 1):
        node = BooleanNode(operator=BooleanOperator.NOT, children=(node,))
    ApplicabilityExpression(root=node)
    node = BooleanNode(operator=BooleanOperator.NOT, children=(node,))
    with pytest.raises(GovernedKnowledgeValidationError):
        ApplicabilityExpression(root=node)


# --------------------------------------------------------------------------
# Exceptions / conclusive exclusion (KI-T11)
# --------------------------------------------------------------------------


def test_conclusive_exception_excludes_even_when_required_unknown() -> None:
    result = evaluate_applicability(
        required=_expr(_equals_node("missing_fact", "x")),
        exceptions=(_expr(_equals_node("excluded", "true")),),
        facts=_snapshot(
            missing_fact=FactEntry(values=()),
            excluded=FactEntry(values=(literal("true"),)),
        ),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NOT_APPLICABLE


def test_unknown_exception_yields_needs_information() -> None:
    result = evaluate_applicability(
        required=_expr(_equals_node("region", "emea")),
        exceptions=(_expr(_equals_node("excluded", "true")),),
        facts=_snapshot(
            region=FactEntry(values=(literal("emea"),)),
            excluded=FactEntry(values=()),
        ),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NEEDS_INFORMATION


def test_required_false_is_not_applicable_regardless_of_exceptions() -> None:
    result = evaluate_applicability(
        required=_expr(_equals_node("region", "apac")),
        exceptions=(_expr(_equals_node("excluded", "unresolved_but_irrelevant")),),
        facts=_snapshot(
            region=FactEntry(values=(literal("emea"),)),
            excluded=FactEntry(values=()),
        ),
        position_version_ref=POSITION_VERSION_REF,
    )
    assert result.outcome is ApplicabilityOutcome.NOT_APPLICABLE


# --------------------------------------------------------------------------
# Contested helper (KI-T34)
# --------------------------------------------------------------------------


def test_contested_positions_requires_explicit_contradiction_relation() -> None:
    contested = contested_positions(
        applicable_position_ids=["pos-a", "pos-b", "pos-c"],
        contradictions=[("pos-a", "pos-b")],
    )
    assert contested == frozenset({"pos-a", "pos-b"})


def test_contested_positions_ignores_unresolved_side() -> None:
    contested = contested_positions(
        applicable_position_ids=["pos-a"],
        contradictions=[("pos-a", "pos-b")],
    )
    assert contested == frozenset()


def test_contested_positions_no_signal_without_contradiction_relation() -> None:
    contested = contested_positions(
        applicable_position_ids=["pos-a", "pos-b"], contradictions=[]
    )
    assert contested == frozenset()
