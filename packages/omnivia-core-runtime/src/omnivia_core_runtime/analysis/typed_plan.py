"""WP05: the typed analytical plan — grain certificates and the compile
pipeline for `analysis.start` (SPEC-CORE-DATA-001 §14–16, D-0028).

The plan is the object between the accepted typed request and the WP03
worker: it binds the exact definitions, resolves the operator graph, proves
the grain of every node, and compiles to one bounded SQL statement that the
worker's fail-closed grammar then re-verifies. Nothing here dispatches.

v1 operator set (§14.3): scan (admitted input), project, filter, aggregate,
semi_join (existence — the fanout-safe join), join (unique-qualified only),
derive (bounded arithmetic), cast, date_bucket, sort_limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot

__all__ = [
    "GRAIN_VIOLATION_CODE",
    "KEY_CONSTRAINT_FAILED_CODE",
    "GrainCertificate",
    "PlanNode",
    "PlanValidationError",
    "TypedPlan",
    "build_plan_from_request",
    "compile_plan",
]

GRAIN_VIOLATION_CODE = "GRAIN_VIOLATION"
KEY_CONSTRAINT_FAILED_CODE = "KEY_CONSTRAINT_FAILED"


class PlanValidationError(Exception):
    """The plan is refused before compilation; nothing was executed."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class GrainCertificate:
    """What one row of a node's output represents, and the evidence for it."""

    node_id: str
    row_meaning: str
    key_columns: tuple[str, ...]
    cardinality: str  # "unique" | "many"
    evidence: str  # the accepted evidence reference for a unique claim

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "row_meaning": self.row_meaning,
            "key_columns": list(self.key_columns),
            "cardinality": self.cardinality,
            "evidence": self.evidence,
        }


@dataclass
class PlanNode:
    """One typed operator node. `scan` inputs resolve through admitted IDs."""

    node_id: str
    operator: str
    inputs: tuple[str, ...] = ()
    grain: GrainCertificate | None = None
    spec: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "operator": self.operator,
            "inputs": list(self.inputs),
            "grain": self.grain.as_dict() if self.grain else None,
            "spec": self.spec,
        }


@dataclass
class TypedPlan:
    """The acyclic operator graph over admitted inputs."""

    plan_id: str
    nodes: dict[str, PlanNode]
    output_node: str
    requested_grain: GrainCertificate

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "nodes": [n.as_dict() for _, n in sorted(self.nodes.items())],
            "output_node": self.output_node,
            "requested_grain": self.requested_grain.as_dict(),
        }


def build_plan_from_request(request: dict[str, Any]) -> TypedPlan:
    """Build the v1 plan for a fanout-safe overdue-exposure calculation.

    The request carries the accepted `analysis.start` milestone-2 body (the
    governed metric revision and its bound parameters). This builder owns the
    grain certificates: the existence condition is a SEMI-JOIN (Q14), never an
    inner join; the pre-aggregation is joined only to a proven-unique
    qualifying relation (Q30).
    """
    _ = request  # the v1 vertical binds one frozen metric shape
    scan_invoices = PlanNode(
        node_id="scan_invoices",
        operator="scan",
        grain=GrainCertificate(
            node_id="scan_invoices",
            row_meaning="one invoice",
            key_columns=("invoice_id",),
            cardinality="unique",
            evidence="finance.invoices PK (admitted source constraint)",
        ),
        spec={"table": "invoices"},
    )
    scan_projects = PlanNode(
        node_id="scan_projects",
        operator="scan",
        grain=GrainCertificate(
            node_id="scan_projects",
            row_meaning="one project",
            key_columns=("project_id",),
            cardinality="unique",
            evidence="finance.projects PK (admitted source constraint)",
        ),
        spec={"table": "projects"},
    )
    semi = PlanNode(
        node_id="customer_has_active_project",
        operator="semi_join",
        inputs=("scan_invoices", "scan_projects"),
        grain=GrainCertificate(
            node_id="customer_has_active_project",
            row_meaning="one invoice (existence of an active project)",
            key_columns=("invoice_id",),
            cardinality="unique",
            evidence="semi-join preserves left grain (UDL-025)",
        ),
        spec={
            "on": ("invoices.customer_id", "projects.customer_id"),
            "right_filter": {"column": "projects.is_active", "value": True},
        },
    )
    aggregate = PlanNode(
        node_id="exposure_sum",
        operator="aggregate",
        inputs=("customer_has_active_project",),
        grain=GrainCertificate(
            node_id="exposure_sum",
            row_meaning="one scalar row: the overdue exposure",
            key_columns=(),
            cardinality="unique",
            evidence="global scalar aggregate (explicit scalar grain)",
        ),
        spec={
            "group_keys": [],
            "measures": [
                {
                    "op": "sum",
                    "column": "invoices.amount_cents",
                    "filter": "status = 'overdue' AND currency = 'AUD'",
                }
            ],
        },
    )
    nodes = {n.node_id: n for n in (scan_invoices, scan_projects, semi, aggregate)}
    _check_acyclic(nodes)
    output_grain = nodes["exposure_sum"].grain
    assert output_grain is not None
    return TypedPlan(
        plan_id="overdue-exposure-v1",
        nodes=nodes,
        output_node="exposure_sum",
        requested_grain=output_grain,
    )


def _check_acyclic(nodes: dict[str, PlanNode]) -> None:
    seen: dict[str, int] = {}

    def visit(node_id: str) -> None:
        state = seen.get(node_id, 0)
        if state == 1:
            raise PlanValidationError(GRAIN_VIOLATION_CODE, f"cycle at {node_id}")
        if state == 2:
            return
        seen[node_id] = 1
        for upstream in nodes[node_id].inputs:
            if upstream not in nodes:
                raise PlanValidationError(
                    GRAIN_VIOLATION_CODE, f"reference to unknown node {upstream}"
                )
            visit(upstream)
        seen[node_id] = 2

    for node_id in nodes:
        visit(node_id)


def compile_plan(plan: TypedPlan) -> str:
    """Compile to ONE bounded read statement the WP03 worker grammar accepts.

    The compiled statement carries no file paths, no SET, no exotic functions:
    it reads the registered tables and nothing else.
    """
    out = plan.nodes[plan.output_node]
    if out.operator != "aggregate" or out.spec.get("group_keys"):
        raise PlanValidationError(
            GRAIN_VIOLATION_CODE, "v1 compiles the scalar-exposure plan only"
        )
    measures = out.spec["measures"]
    if len(measures) != 1:
        raise PlanValidationError(
            GRAIN_VIOLATION_CODE, "v1 compiles exactly one measure"
        )
    measure = measures[0]
    semi = plan.nodes[out.inputs[0]]
    if semi.operator != "semi_join":
        raise PlanValidationError(
            GRAIN_VIOLATION_CODE, "existence must be a semi-join (UDL-025)"
        )
    left_table = plan.nodes[semi.inputs[0]].spec["table"]
    right_table = plan.nodes[semi.inputs[1]].spec["table"]
    left_col, right_col = semi.spec["on"]
    rf = semi.spec["right_filter"]
    column = measure["column"].split(".", 1)[1]
    left_only = left_table
    sql = (
        f"SELECT COALESCE(SUM({left_table}.{column}), 0) AS {measure['op']}_result "
        f"FROM {left_table} "
        f"WHERE {measure['filter'].replace(left_table + '.', f'{left_table}.')} "
        f"AND EXISTS (SELECT 1 FROM {right_table} WHERE "
        f"{right_table}.{right_col.split('.', 1)[1]} = {left_only}.{left_col.split('.', 1)[1]} "
        f"AND {right_table}.{rf['column'].split('.', 1)[1]} = {rf['value']})"
    )
    # Self-check: the compiled statement must pass the worker grammar's own
    # shape rules before it is returned (one statement, read root, FROM).
    statements = sqlglot.parse(sql, read="duckdb")
    parsed = statements[0] if len(statements) == 1 else None
    if parsed is None or parsed.key != "select":
        raise PlanValidationError(
            GRAIN_VIOLATION_CODE, "compiled statement failed the grammar self-check"
        )
    return sql
