"""Shared deterministic wire encode/decode helpers for governed-knowledge content.

Every governed-knowledge profile's `*_to_content`/`*_from_content` pair uses
these helpers for the typed values (`TemporalInstant`, `EffectiveValidInterval`,
`ApplicabilityExpression`, `EvidenceSpan`, `ApplicabilityResult`) that a hand
-written per-profile encoder would otherwise reimplement inconsistently.
Encoding produces a plain JSON-safe `dict`/`list`/scalar tree; decoding is
strict -- an unexpected shape, type, or enum value raises a typed
`GovernedKnowledgeValidationError` rather than silently defaulting or
reinterpreting. Decoded typed values are then re-validated by their own
constructors, so a decode can never produce a value its own type would refuse.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar, cast

from omnivia_core.governed_knowledge.applicability import (
    ApplicabilityExpression,
    ApplicabilityNode,
    ApplicabilityOutcome,
    ApplicabilityResult,
    BooleanNode,
    BooleanOperator,
    ComparisonNode,
    ComparisonOperator,
    Operand,
    OperandKind,
)
from omnivia_core.governed_knowledge.errors import (
    GovernedKnowledgeErrorCode,
    GovernedKnowledgeValidationError,
    require,
)
from omnivia_core.semantic_registry.errors import SemanticRegistryError
from omnivia_core.semantic_registry.evidence import EvidenceSpan
from omnivia_core.semantic_registry.temporal import (
    EffectiveValidInterval,
    EndBoundaryState,
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)

_T = TypeVar("_T")


def _wrap(factory: Callable[..., _T], /, **kwargs: Any) -> _T:
    """Call `factory(**kwargs)`, translating a lower-layer failure to ours."""
    try:
        return factory(**kwargs)
    except SemanticRegistryError as error:
        raise GovernedKnowledgeValidationError(
            GovernedKnowledgeErrorCode.INVALID_FIELD, str(error)
        ) from error


def require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    require(
        isinstance(value, Mapping),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{what} must be a mapping",
    )
    return cast(Mapping[str, Any], value)


def require_str(value: Any, what: str) -> str:
    require(
        isinstance(value, str),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{what} must be a string",
    )
    return cast(str, value)


def require_opt_str(value: Any, what: str) -> str | None:
    if value is None:
        return None
    return require_str(value, what)


def require_str_list(value: Any, what: str) -> list[str]:
    require(
        isinstance(value, (list, tuple)),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{what} must be a list",
    )
    for index, item in enumerate(value):
        require(
            isinstance(item, str),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"{what}[{index}] must be a string",
        )
    return list(value)


def require_enum(value: Any, what: str, enum_cls: type[Any]) -> Any:
    raw = require_str(value, what)
    require(
        raw in {member.value for member in enum_cls},
        GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
        f"{what} {raw!r} is not a recognised {enum_cls.__name__}",
    )
    return enum_cls(raw)


# --------------------------------------------------------------------------
# TemporalInstant
# --------------------------------------------------------------------------


def encode_instant(instant: TemporalInstant) -> dict[str, Any]:
    return {
        "value": instant.value.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "precision": instant.precision.value,
        "provenance": instant.provenance.value,
        "original_source_text": instant.original_source_text,
        "source_timezone": instant.source_timezone,
    }


def decode_instant(data: Any) -> TemporalInstant:
    mapping = require_mapping(data, "instant")
    value_text = require_str(mapping.get("value"), "instant['value']")
    try:
        value = datetime.strptime(value_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise GovernedKnowledgeValidationError(
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "instant['value'] is not a valid RFC3339 UTC timestamp",
        ) from error
    precision = require_enum(
        mapping.get("precision"), "instant['precision']", TemporalPrecision
    )
    provenance = require_enum(
        mapping.get("provenance"), "instant['provenance']", TemporalProvenance
    )
    return _wrap(
        TemporalInstant,
        value=value,
        precision=precision,
        provenance=provenance,
        original_source_text=require_opt_str(
            mapping.get("original_source_text"), "instant['original_source_text']"
        ),
        source_timezone=require_opt_str(
            mapping.get("source_timezone"), "instant['source_timezone']"
        ),
    )


def encode_opt_instant(instant: TemporalInstant | None) -> dict[str, Any] | None:
    return None if instant is None else encode_instant(instant)


def decode_opt_instant(data: Any) -> TemporalInstant | None:
    return None if data is None else decode_instant(data)


# --------------------------------------------------------------------------
# EffectiveValidInterval
# --------------------------------------------------------------------------


def encode_interval(interval: EffectiveValidInterval) -> dict[str, Any]:
    return {
        "effective_from": encode_instant(interval.effective_from),
        "effective_to": encode_opt_instant(interval.effective_to),
        "end_state": interval.end_state.value,
    }


def decode_interval(data: Any) -> EffectiveValidInterval:
    mapping = require_mapping(data, "interval")
    effective_from = decode_instant(mapping.get("effective_from"))
    effective_to = decode_opt_instant(mapping.get("effective_to"))
    end_state = require_enum(
        mapping.get("end_state"), "interval['end_state']", EndBoundaryState
    )
    return _wrap(
        EffectiveValidInterval,
        effective_from=effective_from,
        effective_to=effective_to,
        end_state=end_state,
    )


def encode_opt_interval(
    interval: EffectiveValidInterval | None,
) -> dict[str, Any] | None:
    return None if interval is None else encode_interval(interval)


def decode_opt_interval(data: Any) -> EffectiveValidInterval | None:
    return None if data is None else decode_interval(data)


# --------------------------------------------------------------------------
# EvidenceSpan
# --------------------------------------------------------------------------


def encode_span(span: EvidenceSpan) -> dict[str, Any]:
    return {
        "span_id": span.span_id,
        "start_offset": span.start_offset,
        "end_offset": span.end_offset,
        "page": span.page,
        "section": span.section,
    }


def decode_span(data: Any) -> EvidenceSpan:
    mapping = require_mapping(data, "span")
    span_id = require_str(mapping.get("span_id"), "span['span_id']")
    start_offset = mapping.get("start_offset")
    require(
        isinstance(start_offset, int) and not isinstance(start_offset, bool),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "span['start_offset'] must be an integer",
    )
    end_offset = mapping.get("end_offset")
    require(
        isinstance(end_offset, int) and not isinstance(end_offset, bool),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "span['end_offset'] must be an integer",
    )
    page = mapping.get("page")
    if page is not None:
        require(
            isinstance(page, int) and not isinstance(page, bool),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "span['page'] must be an integer or null",
        )
    section = require_opt_str(mapping.get("section"), "span['section']")
    return _wrap(
        EvidenceSpan,
        span_id=span_id,
        start_offset=start_offset,
        end_offset=end_offset,
        page=page,
        section=section,
    )


# --------------------------------------------------------------------------
# Operand
# --------------------------------------------------------------------------


def encode_operand(operand: Operand) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": operand.kind.value}
    if operand.kind is OperandKind.FACT_REF:
        payload["fact_ref"] = operand.fact_ref
    elif operand.kind is OperandKind.LITERAL:
        payload["literal_value"] = operand.literal_value
    elif operand.kind is OperandKind.ENUM:
        payload["enum_value"] = operand.enum_value
    elif operand.kind is OperandKind.NUMERIC:
        assert operand.numeric_value is not None
        payload["numeric_value"] = str(operand.numeric_value)
        payload["unit"] = operand.unit
    else:  # DATE
        assert operand.date_value is not None
        payload["date_value"] = encode_instant(operand.date_value)
    return payload


def decode_operand(data: Any) -> Operand:
    mapping = require_mapping(data, "operand")
    kind = require_enum(mapping.get("kind"), "operand['kind']", OperandKind)
    if kind is OperandKind.FACT_REF:
        return _wrap(
            Operand,
            kind=kind,
            fact_ref=require_str(mapping.get("fact_ref"), "operand['fact_ref']"),
        )
    if kind is OperandKind.LITERAL:
        value = mapping.get("literal_value")
        require(
            isinstance(value, (str, bool)),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "operand['literal_value'] must be a string or boolean",
        )
        return _wrap(Operand, kind=kind, literal_value=value)
    if kind is OperandKind.ENUM:
        return _wrap(
            Operand,
            kind=kind,
            enum_value=require_str(mapping.get("enum_value"), "operand['enum_value']"),
        )
    if kind is OperandKind.NUMERIC:
        raw = mapping.get("numeric_value")
        require(
            isinstance(raw, str),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "operand['numeric_value'] must be a string",
        )
        try:
            numeric_value = Decimal(cast(str, raw))
        except InvalidOperation as error:
            raise GovernedKnowledgeValidationError(
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "operand['numeric_value'] is not a valid decimal",
            ) from error
        unit = require_str(mapping.get("unit"), "operand['unit']")
        return _wrap(Operand, kind=kind, numeric_value=numeric_value, unit=unit)
    # DATE
    return _wrap(
        Operand, kind=kind, date_value=decode_instant(mapping.get("date_value"))
    )


# --------------------------------------------------------------------------
# ApplicabilityExpression tree
# --------------------------------------------------------------------------


def encode_node(node: ApplicabilityNode) -> dict[str, Any]:
    if isinstance(node, ComparisonNode):
        payload: dict[str, Any] = {
            "node_kind": "comparison",
            "operator": node.operator.value,
            "left": encode_operand(node.left),
        }
        if node.operator is ComparisonOperator.MEMBER_OF:
            payload["members"] = [encode_operand(member) for member in node.members]
        else:
            assert node.right is not None
            payload["right"] = encode_operand(node.right)
        return payload
    return {
        "node_kind": "boolean",
        "operator": node.operator.value,
        "children": [encode_node(child) for child in node.children],
    }


def decode_node(data: Any) -> ApplicabilityNode:
    mapping = require_mapping(data, "node")
    node_kind = require_str(mapping.get("node_kind"), "node['node_kind']")
    if node_kind == "comparison":
        operator = require_enum(
            mapping.get("operator"), "node['operator']", ComparisonOperator
        )
        left = decode_operand(mapping.get("left"))
        if operator is ComparisonOperator.MEMBER_OF:
            members_raw = mapping.get("members")
            require(
                isinstance(members_raw, list),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "node['members'] must be a list",
            )
            members = tuple(
                decode_operand(member) for member in cast(list[Any], members_raw)
            )
            return _wrap(ComparisonNode, operator=operator, left=left, members=members)
        right = decode_operand(mapping.get("right"))
        return _wrap(ComparisonNode, operator=operator, left=left, right=right)
    if node_kind == "boolean":
        operator_b = require_enum(
            mapping.get("operator"), "node['operator']", BooleanOperator
        )
        children_raw = mapping.get("children")
        require(
            isinstance(children_raw, list),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "node['children'] must be a list",
        )
        children = tuple(decode_node(child) for child in cast(list[Any], children_raw))
        return _wrap(BooleanNode, operator=operator_b, children=children)
    raise GovernedKnowledgeValidationError(
        GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
        "node['node_kind'] must be 'comparison' or 'boolean'",
    )


def encode_expression(expression: ApplicabilityExpression) -> dict[str, Any]:
    return {"version": expression.version, "root": encode_node(expression.root)}


def decode_expression(data: Any) -> ApplicabilityExpression:
    mapping = require_mapping(data, "expression")
    version = require_str(mapping.get("version"), "expression['version']")
    root = decode_node(mapping.get("root"))
    return _wrap(ApplicabilityExpression, root=root, version=version)


def encode_opt_expression(
    expression: ApplicabilityExpression | None,
) -> dict[str, Any] | None:
    return None if expression is None else encode_expression(expression)


def decode_opt_expression(data: Any) -> ApplicabilityExpression | None:
    return None if data is None else decode_expression(data)


# --------------------------------------------------------------------------
# ApplicabilityResult
# --------------------------------------------------------------------------


def encode_applicability_result(result: ApplicabilityResult) -> dict[str, Any]:
    return {
        "outcome": result.outcome.value,
        "fact_snapshot_ref": result.fact_snapshot_ref,
        "position_version_ref": result.position_version_ref,
        "evaluator_version": result.evaluator_version,
        "authorized_fact_refs": list(result.authorized_fact_refs),
        "reasons": list(result.reasons),
        "fact_provenance_classes": [
            {"fact_ref": fact_ref, "provenance_class": provenance_class}
            for fact_ref, provenance_class in result.fact_provenance_classes
        ],
    }


def decode_applicability_result(data: Any) -> ApplicabilityResult:
    mapping = require_mapping(data, "applicability_result")
    outcome = require_enum(
        mapping.get("outcome"), "applicability_result['outcome']", ApplicabilityOutcome
    )
    fact_snapshot_ref = require_str(
        mapping.get("fact_snapshot_ref"), "applicability_result['fact_snapshot_ref']"
    )
    position_version_ref = require_str(
        mapping.get("position_version_ref"),
        "applicability_result['position_version_ref']",
    )
    evaluator_version = require_str(
        mapping.get("evaluator_version"), "applicability_result['evaluator_version']"
    )
    authorized = require_str_list(
        mapping.get("authorized_fact_refs"),
        "applicability_result['authorized_fact_refs']",
    )
    reasons = require_str_list(
        mapping.get("reasons"), "applicability_result['reasons']"
    )
    provenance_raw = mapping.get("fact_provenance_classes", [])
    require(
        isinstance(provenance_raw, list),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "applicability_result['fact_provenance_classes'] must be a list",
    )
    provenance = []
    for raw in cast(list[Any], provenance_raw):
        item = require_mapping(raw, "fact_provenance_class")
        provenance.append(
            (
                require_str(item.get("fact_ref"), "fact_ref"),
                require_str(item.get("provenance_class"), "provenance_class"),
            )
        )
    return ApplicabilityResult(
        outcome=outcome,
        fact_snapshot_ref=fact_snapshot_ref,
        position_version_ref=position_version_ref,
        evaluator_version=evaluator_version,
        authorized_fact_refs=tuple(authorized),
        reasons=tuple(reasons),
        fact_provenance_classes=tuple(provenance),
    )


__all__ = [
    "decode_applicability_result",
    "decode_expression",
    "decode_instant",
    "decode_interval",
    "decode_node",
    "decode_operand",
    "decode_opt_expression",
    "decode_opt_instant",
    "decode_opt_interval",
    "decode_span",
    "encode_applicability_result",
    "encode_expression",
    "encode_instant",
    "encode_interval",
    "encode_node",
    "encode_operand",
    "encode_opt_expression",
    "encode_opt_instant",
    "encode_opt_interval",
    "encode_span",
    "require_enum",
    "require_mapping",
    "require_opt_str",
    "require_str",
    "require_str_list",
]
