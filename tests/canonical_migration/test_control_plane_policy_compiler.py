from dataclasses import asdict

import pytest

from omnivia_core.control_plane.models import (
    CONTROL_PLANE_SCHEMA_VERSION,
    PolicyAttributeCondition,
    PolicyAttributeExpression,
)
from omnivia_core.control_plane.validation import (
    ControlPlaneValidationError,
    compile_policy_expression,
    validate_control_plane_manifest,
)


def _condition(node: PolicyAttributeExpression) -> PolicyAttributeCondition:
    assert node.op == "condition"
    assert node.condition is not None
    return node.condition


def _manifest_with_policy_expression(expression: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": CONTROL_PLANE_SCHEMA_VERSION,
        "workspace": {"id": "workspace.compiler"},
        "connections": [
            {
                "id": "connection.demo",
                "kind": "app",
                "lifecycle": "active",
            }
        ],
        "capabilities": [
            {
                "id": "capability.demo.read",
                "capability_type": "query",
                "connection_id": "connection.demo",
                "side_effect": "read",
                "lifecycle": "active",
            }
        ],
        "policies": [
            {
                "id": "policy.expression.compiled",
                "decision": "allow",
                "capability_ids": ["capability.demo.read"],
                "attribute_expression": expression,
            }
        ],
    }


def test_compile_policy_expression_maps_comparisons() -> None:
    expression = compile_policy_expression("actor.clearance == 'high'")
    condition = _condition(expression)
    assert condition.scope == "actor"
    assert condition.key == "clearance"
    assert condition.operator == "equals"
    assert condition.value == "high"
    assert condition.values == []

    not_equals = _condition(compile_policy_expression("workspace.tier != 'bronze'"))
    assert not_equals.scope == "workspace"
    assert not_equals.operator == "not_equals"
    assert not_equals.value == "bronze"


def test_compile_policy_expression_maps_numeric_comparisons() -> None:
    cases = {
        "actor.seniority > 3": ("greater_than", "3"),
        "actor.seniority >= 3": ("greater_than_or_equal", "3"),
        "actor.seniority < 10": ("less_than", "10"),
        "actor.seniority <= 10": ("less_than_or_equal", "10"),
        "workspace.budget >= -2.5": ("greater_than_or_equal", "-2.5"),
    }
    for source, (operator, value) in cases.items():
        condition = _condition(compile_policy_expression(source))
        assert condition.operator == operator
        assert condition.value == value


def test_compile_policy_expression_maps_membership() -> None:
    in_expression = compile_policy_expression("actor.team in ['ops','admin']")
    in_condition = _condition(in_expression)
    assert in_condition.operator == "in"
    assert in_condition.values == ["ops", "admin"]
    assert in_condition.value is None

    not_in_expression = compile_policy_expression(
        "workspace.region not in ['eu','us']"
    )
    not_in_condition = _condition(not_in_expression)
    assert not_in_condition.operator == "not_in"
    assert not_in_condition.values == ["eu", "us"]


def test_compile_policy_expression_combines_boolean_operators() -> None:
    expression = compile_policy_expression(
        "actor.clearance == 'high' && workspace.tier in ['gold'] "
        "|| !actor.suspended == 'yes'"
    )
    # || is lowest precedence, so the root is an any node flattening the
    # top-level alternatives.
    assert expression.op == "any"
    assert len(expression.children) == 2
    left, right = expression.children
    assert left.op == "all"
    assert [_condition(child).key for child in left.children] == [
        "clearance",
        "tier",
    ]
    assert right.op == "not"
    assert _condition(right.children[0]).key == "suspended"


def test_compile_policy_expression_flattens_repeated_operators() -> None:
    expression = compile_policy_expression(
        "actor.a == '1' && actor.b == '2' && actor.c == '3'"
    )
    assert expression.op == "all"
    assert len(expression.children) == 3


def test_compile_policy_expression_supports_parentheses() -> None:
    expression = compile_policy_expression(
        "(actor.clearance == 'high' || workspace.tier == 'gold') "
        "&& actor.active == 'yes'"
    )
    assert expression.op == "all"
    grouped, flag = expression.children
    assert grouped.op == "any"
    assert _condition(flag).key == "active"


def test_compile_policy_expression_round_trips_through_validation() -> None:
    expression = compile_policy_expression(
        "actor.clearance == 'high' && workspace.tier in ['gold','platinum']"
    )
    manifest = _manifest_with_policy_expression(asdict(expression))
    # The compiled tree validates as a normal manifest attribute_expression.
    result = validate_control_plane_manifest(manifest)
    assert result.valid, result.errors


@pytest.mark.parametrize(
    "source",
    [
        "size(actor.team) > 0",  # function call
        "request.path == '/x'",  # arbitrary identifier / scope
        "actor == 'x'",  # missing .key segment
        "actor.team.lead == 'x'",  # attribute path beyond simple key
        "actor.cost + 1 == 2",  # arithmetic
        "actor.name =~ 'admin.*'",  # regex match operator
        "import os",  # import-like text
        "actor.role == `admin`",  # backtick literal
        "actor.role == '${injected}'",  # template interpolation
        "actor.role == \"a\" ? 'b' : 'c'",  # ternary / unsupported syntax
        "actor.team in ['ops'",  # unbalanced list
        "(actor.role == 'x'",  # unbalanced parenthesis
        "actor.role",  # missing comparison
        "actor.role == 'x' &&",  # dangling operator
        "actor.seniority > 'high'",  # numeric op needs number
        "actor.team in []",  # empty membership list
        "actor.team in ['ops', 3]",  # non-string list member
        "",  # empty source
        "   ",  # whitespace only
    ],
)
def test_compile_policy_expression_rejects_unsupported_syntax(source: str) -> None:
    with pytest.raises(ControlPlaneValidationError):
        compile_policy_expression(source)


@pytest.mark.parametrize(
    "source",
    [
        "actor.api_key == 'x'",  # secret-looking key
        "actor.session_token == 'x'",  # secret-looking key
        "actor.role == 'secret-value'",  # secret-looking value
        "workspace.tier in ['bearer-xyz']",  # secret-looking list member
    ],
)
def test_compile_policy_expression_rejects_secret_like_text(source: str) -> None:
    with pytest.raises(ControlPlaneValidationError):
        compile_policy_expression(source)


def test_compile_policy_expression_rejects_non_string_input() -> None:
    with pytest.raises(ControlPlaneValidationError):
        compile_policy_expression(123)  # type: ignore[arg-type]
