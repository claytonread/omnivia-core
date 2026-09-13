"""KI-01 applicability evaluator: a pure, deterministic, versioned three-valued evaluator.

Spec section 6.3-6.5. Bounded Boolean composition (`all`/`any`/`not`, exactly one
operator per node) over typed comparisons (equality, finite-set membership, typed
numeric ordering, typed date ordering) against declared task facts. No arbitrary
code, regex, SQL, model/tool/network/filesystem execution -- every node is a frozen
dataclass and evaluation is a pure function over an in-memory fact snapshot.

Kleene three-valued logic (`TruthValue`) is internal; the public outcome vocabulary
is `ApplicabilityOutcome` (`applicable` / `not_applicable` / `needs_information`),
plus the deterministic `contested_positions` helper for an explicit, authorized
contradiction relation (never freshness/popularity/similarity precedence).

Authorized fact visibility (spec 6.3): a result's `authorized_fact_refs` and
`reasons` may only name a fact reference the caller marked visible in the
`FactSnapshot` it supplied. A fact that is absent, conflicting or marked hidden
never appears by name, enum value or count; it contributes only the generic,
static reason `"Required context is unavailable"`.

Two error regimes, by design:

- **Malformed expression trees** (wrong operand kind for an operator, a `not`
  node without exactly one child, an oversized finite set, a tree past the node/
  depth bounds) raise :class:`GovernedKnowledgeValidationError` at construction
  time -- these are authoring defects, not information the evaluator can reason
  about.
- **Runtime ambiguity against a given fact snapshot** (a fact absent or in
  conflict, a numeric comparison against a mismatched unit, a date comparison
  whose precisions cannot decide strict order) never raises: it resolves to
  `TruthValue.UNKNOWN`, which is what makes `not UNKNOWN == UNKNOWN` and decisive
  Kleene `all`/`any` well defined regardless of which facts a caller happens to
  supply.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from omnivia_core.governed_knowledge.errors import (
    GovernedKnowledgeErrorCode,
    require,
)
from omnivia_core.semantic_registry.temporal import TemporalInstant

APPLICABILITY_CONTRACT_VERSION = "governed-knowledge-applicability-v1"

MAX_NODES = 64
MAX_DEPTH = 8
MAX_SET_MEMBERS = 100

_HIDDEN_CONTEXT_REASON = "Required context is unavailable"


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        GovernedKnowledgeErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


# --------------------------------------------------------------------------
# Truth values and public outcomes
# --------------------------------------------------------------------------


class TruthValue(str, Enum):
    """Internal Kleene three-valued truth. Never part of a public result."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class ApplicabilityOutcome(str, Enum):
    """The public applicability result vocabulary (spec 6.3)."""

    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    NEEDS_INFORMATION = "needs_information"
    CONTESTED = "contested"


class FactProvenanceClass(str, Enum):
    """The evidential class of a supplied task fact (spec 6.4)."""

    USER_ASSUMPTION = "user_assumption"
    VERIFIED_ORGANISATIONAL_RECORD = "verified_organisational_record"
    AUTHENTICATED_INSTRUCTION = "authenticated_instruction"
    EXTERNAL_ATTESTATION = "external_attestation"
    UNKNOWN = "unknown"


def negate(value: TruthValue) -> TruthValue:
    """`not UNKNOWN == UNKNOWN` (spec 6.3), never coerced to a boolean."""
    if value is TruthValue.TRUE:
        return TruthValue.FALSE
    if value is TruthValue.FALSE:
        return TruthValue.TRUE
    return TruthValue.UNKNOWN


def _all_of(values: Sequence[TruthValue]) -> TruthValue:
    """Decisive Kleene AND: one `FALSE` decides even beside an `UNKNOWN`."""
    if any(value is TruthValue.FALSE for value in values):
        return TruthValue.FALSE
    if any(value is TruthValue.UNKNOWN for value in values):
        return TruthValue.UNKNOWN
    return TruthValue.TRUE


def _any_of(values: Sequence[TruthValue]) -> TruthValue:
    """Decisive Kleene OR: one `TRUE` decides even beside an `UNKNOWN`."""
    if any(value is TruthValue.TRUE for value in values):
        return TruthValue.TRUE
    if any(value is TruthValue.UNKNOWN for value in values):
        return TruthValue.UNKNOWN
    return TruthValue.FALSE


# --------------------------------------------------------------------------
# Operands and fact values
# --------------------------------------------------------------------------


class OperandKind(str, Enum):
    """What kind of thing an operand or fact value is."""

    FACT_REF = "fact_ref"
    LITERAL = "literal"
    ENUM = "enum"
    NUMERIC = "numeric"
    DATE = "date"


@dataclass(frozen=True, slots=True)
class Operand:
    """One typed operand: a fact reference, or a literal/enum/numeric/date value.

    Exactly one payload field is populated, matching `kind`. `numeric_value` is a
    `Decimal` -- never a `float` -- so a canonical numeric operand carries no
    binary floating-point rounding, and `unit` is required alongside it: numeric
    comparison never performs implicit unit conversion (spec 6.3).
    """

    kind: OperandKind
    fact_ref: str | None = None
    literal_value: str | bool | None = None
    enum_value: str | None = None
    numeric_value: Decimal | None = None
    unit: str | None = None
    date_value: TemporalInstant | None = None

    def __post_init__(self) -> None:
        require(
            isinstance(self.kind, OperandKind),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "Operand.kind must be an OperandKind",
        )
        populated = {
            OperandKind.FACT_REF: self.fact_ref is not None,
            OperandKind.LITERAL: self.literal_value is not None,
            OperandKind.ENUM: self.enum_value is not None,
            OperandKind.NUMERIC: self.numeric_value is not None,
            OperandKind.DATE: self.date_value is not None,
        }
        require(
            populated[self.kind],
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"Operand.kind {self.kind.value!r} requires its matching value field",
        )
        for kind, is_populated in populated.items():
            if kind is not self.kind:
                require(
                    not is_populated,
                    GovernedKnowledgeErrorCode.INVALID_FIELD,
                    f"Operand.kind {self.kind.value!r} must not carry a "
                    f"{kind.value!r} payload",
                )
        if self.kind is OperandKind.FACT_REF:
            _require_id("Operand.fact_ref", self.fact_ref or "")
        if self.kind is OperandKind.ENUM:
            _require_id("Operand.enum_value", self.enum_value or "")
        if self.kind is OperandKind.NUMERIC:
            require(
                isinstance(self.numeric_value, Decimal),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "Operand.numeric_value must be a Decimal",
            )
            assert self.numeric_value is not None
            require(
                self.numeric_value.is_finite(),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "Operand.numeric_value must be finite",
            )
            _require_id("Operand.unit", self.unit or "")
        if self.kind is OperandKind.DATE:
            require(
                isinstance(self.date_value, TemporalInstant),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "Operand.date_value must be a TemporalInstant",
            )
        if self.kind is OperandKind.LITERAL:
            require(
                isinstance(self.literal_value, (str, bool)),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "Operand.literal_value must be a str or bool",
            )


def fact_ref(name: str) -> Operand:
    """Convenience constructor for a `FACT_REF` operand."""
    return Operand(kind=OperandKind.FACT_REF, fact_ref=name)


def literal(value: str | bool) -> Operand:
    return Operand(kind=OperandKind.LITERAL, literal_value=value)


def enum(value: str) -> Operand:
    return Operand(kind=OperandKind.ENUM, enum_value=value)


def numeric(value: Decimal, unit: str) -> Operand:
    return Operand(kind=OperandKind.NUMERIC, numeric_value=value, unit=unit)


def date(value: TemporalInstant) -> Operand:
    return Operand(kind=OperandKind.DATE, date_value=value)


def _instant_bounds(value: TemporalInstant) -> tuple[datetime, datetime]:
    """Return the half-open interval represented by one declared precision."""
    start = value.value
    precision = value.precision.value
    if precision == "year":
        end = start.replace(year=start.year + 1)
    elif precision == "month":
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    elif precision == "day":
        end = start + timedelta(days=1)
    elif precision == "hour":
        end = start + timedelta(hours=1)
    elif precision == "minute":
        end = start + timedelta(minutes=1)
    else:
        end = start + timedelta(seconds=1)
    return start, end


def _values_equal(left: Operand, right: Operand) -> TruthValue:
    """Typed equality: mismatched kinds are simply not equal, never ambiguous.

    Text equality uses the field's declared normalisation upstream (spec 6.3);
    here it is exact-string comparison, never embedding similarity.
    """
    if left.kind is not right.kind:
        return TruthValue.FALSE
    if left.kind is OperandKind.LITERAL:
        return (
            TruthValue.TRUE
            if left.literal_value == right.literal_value
            else TruthValue.FALSE
        )
    if left.kind is OperandKind.ENUM:
        return (
            TruthValue.TRUE if left.enum_value == right.enum_value else TruthValue.FALSE
        )
    if left.kind is OperandKind.NUMERIC:
        if left.unit != right.unit:
            return TruthValue.UNKNOWN
        return (
            TruthValue.TRUE
            if left.numeric_value == right.numeric_value
            else TruthValue.FALSE
        )
    if left.kind is OperandKind.DATE:
        assert left.date_value is not None and right.date_value is not None
        left_start, left_end = _instant_bounds(left.date_value)
        right_start, right_end = _instant_bounds(right.date_value)
        if left_start == right_start and left_end == right_end:
            return TruthValue.TRUE
        if left_end <= right_start or right_end <= left_start:
            return TruthValue.FALSE
        return TruthValue.UNKNOWN
    raise AssertionError(f"_values_equal: unsupported operand kind {left.kind!r}")


# --------------------------------------------------------------------------
# Fact snapshot
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FactEntry:
    """One fact reference's declared values and outward disclosure permission.

    `values` may hold zero (declared but not supplied), one, or several entries.
    Several *distinct* entries is the "conflicting multiple fact values" case
    (spec 6.3) and always resolves to `TruthValue.UNKNOWN`, never an arbitrary
    pick. `visible` gates only whether this fact's reference/value may appear in
    an outward `ApplicabilityResult`'s `authorized_fact_refs`/`reasons` -- it does
    not gate whether the evaluator uses the value to compute truth, since a
    caller is expected to hand the evaluator only facts it is already authorised
    to compute over (spec 6.4: "the evaluator never grants access to a fact
    merely because a position references it").
    """

    values: tuple[Operand, ...] = ()
    visible: bool = True
    provenance_class: FactProvenanceClass = FactProvenanceClass.UNKNOWN

    def __post_init__(self) -> None:
        require(
            isinstance(self.provenance_class, FactProvenanceClass),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "FactEntry.provenance_class must be a FactProvenanceClass",
        )
        for index, value in enumerate(self.values):
            require(
                isinstance(value, Operand)
                and value.kind
                in (
                    OperandKind.LITERAL,
                    OperandKind.ENUM,
                    OperandKind.NUMERIC,
                    OperandKind.DATE,
                ),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"FactEntry.values[{index}] must be a literal/enum/numeric/date Operand",
            )


@dataclass(frozen=True, slots=True)
class FactSnapshot:
    """An immutable, pinned mapping of fact reference to :class:`FactEntry`."""

    fact_snapshot_ref: str
    facts: Mapping[str, FactEntry]

    def __post_init__(self) -> None:
        _require_id("FactSnapshot.fact_snapshot_ref", self.fact_snapshot_ref)
        require(
            isinstance(self.facts, Mapping),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "FactSnapshot.facts must be a mapping",
        )
        frozen: dict[str, FactEntry] = {}
        for key, entry in self.facts.items():
            require(
                isinstance(key, str) and key.strip() != "",
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "FactSnapshot.facts keys must be non-empty strings",
            )
            require(
                isinstance(entry, FactEntry),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"FactSnapshot.facts[{key!r}] must be a FactEntry",
            )
            frozen[key] = entry
        object.__setattr__(self, "facts", MappingProxyType(frozen))


class _Ledger:
    """Per-evaluation trace of which fact refs were consulted, for safe reasons."""

    __slots__ = (
        "hidden_unresolved",
        "provenance",
        "resolved_visible",
        "unresolved_visible",
    )

    def __init__(self) -> None:
        self.resolved_visible: set[str] = set()
        self.unresolved_visible: set[str] = set()
        self.hidden_unresolved: bool = False
        self.provenance: dict[str, FactProvenanceClass] = {}

    def consult(self, name: str, snapshot: FactSnapshot) -> Operand | None:
        entry = snapshot.facts.get(name)
        if entry is None or len(entry.values) == 0:
            if entry is not None and entry.visible:
                self.provenance[name] = entry.provenance_class
                self.unresolved_visible.add(name)
            else:
                self.hidden_unresolved = True
            return None
        distinct = {
            (
                v.kind,
                v.literal_value,
                v.enum_value,
                v.numeric_value,
                v.unit,
                v.date_value.value if v.date_value is not None else None,
            )
            for v in entry.values
        }
        if len(distinct) > 1:
            if entry.visible:
                self.provenance[name] = entry.provenance_class
                self.unresolved_visible.add(name)
            else:
                self.hidden_unresolved = True
            return None
        if entry.visible:
            self.provenance[name] = entry.provenance_class
            self.resolved_visible.add(name)
        return entry.values[0]


# --------------------------------------------------------------------------
# Expression tree
# --------------------------------------------------------------------------


class BooleanOperator(str, Enum):
    ALL = "all"
    ANY = "any"
    NOT = "not"


class ComparisonOperator(str, Enum):
    EQUALS = "equals"
    MEMBER_OF = "member_of"
    NUMERIC_LT = "numeric_lt"
    NUMERIC_LTE = "numeric_lte"
    NUMERIC_GT = "numeric_gt"
    NUMERIC_GTE = "numeric_gte"
    DATE_LT = "date_lt"
    DATE_LTE = "date_lte"
    DATE_GT = "date_gt"
    DATE_GTE = "date_gte"


_NUMERIC_OPERATORS = frozenset(
    {
        ComparisonOperator.NUMERIC_LT,
        ComparisonOperator.NUMERIC_LTE,
        ComparisonOperator.NUMERIC_GT,
        ComparisonOperator.NUMERIC_GTE,
    }
)
_DATE_OPERATORS = frozenset(
    {
        ComparisonOperator.DATE_LT,
        ComparisonOperator.DATE_LTE,
        ComparisonOperator.DATE_GT,
        ComparisonOperator.DATE_GTE,
    }
)


@dataclass(frozen=True, slots=True)
class ComparisonNode:
    """One typed comparison: equality, finite-set membership, or numeric/date ordering.

    `left` is always a `FACT_REF` operand. `right` carries the comparison value
    for `equals`/numeric/date operators; `members` carries the finite set (each a
    `LITERAL` or `ENUM` operand, at most :data:`MAX_SET_MEMBERS`) for `member_of`.
    """

    operator: ComparisonOperator
    left: Operand
    right: Operand | None = None
    members: tuple[Operand, ...] = ()

    def __post_init__(self) -> None:
        require(
            isinstance(self.operator, ComparisonOperator),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "ComparisonNode.operator must be a ComparisonOperator",
        )
        require(
            isinstance(self.left, Operand) and self.left.kind is OperandKind.FACT_REF,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "ComparisonNode.left must be a fact_ref operand",
        )
        if self.operator is ComparisonOperator.MEMBER_OF:
            require(
                self.right is None,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "member_of must not carry a right operand",
            )
            require(
                1 <= len(self.members) <= MAX_SET_MEMBERS,
                GovernedKnowledgeErrorCode.SET_LIMIT_EXCEEDED,
                f"member_of members must number between 1 and {MAX_SET_MEMBERS}",
            )
            for index, member in enumerate(self.members):
                require(
                    isinstance(member, Operand)
                    and member.kind
                    in (
                        OperandKind.LITERAL,
                        OperandKind.ENUM,
                        OperandKind.NUMERIC,
                        OperandKind.DATE,
                    ),
                    GovernedKnowledgeErrorCode.INVALID_FIELD,
                    f"member_of members[{index}] must be a typed value operand",
                )
            return
        require(
            len(self.members) == 0,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"{self.operator.value} must not carry members",
        )
        require(
            self.right is not None,
            GovernedKnowledgeErrorCode.MISSING_FIELD,
            f"{self.operator.value} requires a right operand",
        )
        right = self.right
        assert right is not None
        if self.operator is ComparisonOperator.EQUALS:
            require(
                right.kind
                in (
                    OperandKind.LITERAL,
                    OperandKind.ENUM,
                    OperandKind.NUMERIC,
                    OperandKind.DATE,
                ),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "equals requires a typed value right operand",
            )
        elif self.operator in _NUMERIC_OPERATORS:
            require(
                right.kind is OperandKind.NUMERIC,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"{self.operator.value} requires a numeric right operand",
            )
        elif self.operator in _DATE_OPERATORS:
            require(
                right.kind is OperandKind.DATE,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"{self.operator.value} requires a date right operand",
            )
        else:  # pragma: no cover - defensive, unreachable via typed enum
            raise AssertionError(f"unhandled comparison operator {self.operator!r}")


@dataclass(frozen=True, slots=True)
class BooleanNode:
    """A Boolean composition node: exactly one of `all`/`any`/`not` (spec 6.3).

    `not` takes exactly one child; `all`/`any` take one or more. `children` may
    mix :class:`BooleanNode` and :class:`ComparisonNode` freely.
    """

    operator: BooleanOperator
    children: tuple[ApplicabilityNode, ...]

    def __post_init__(self) -> None:
        require(
            isinstance(self.operator, BooleanOperator),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "BooleanNode.operator must be a BooleanOperator",
        )
        for index, child in enumerate(self.children):
            require(
                isinstance(child, (BooleanNode, ComparisonNode)),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"BooleanNode.children[{index}] must be a BooleanNode or ComparisonNode",
            )
        if self.operator is BooleanOperator.NOT:
            require(
                len(self.children) == 1,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "not must have exactly one child",
            )
        else:
            require(
                len(self.children) >= 1,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"{self.operator.value} must have at least one child",
            )


ApplicabilityNode = BooleanNode | ComparisonNode


def _count_and_depth(node: ApplicabilityNode) -> tuple[int, int]:
    if isinstance(node, ComparisonNode):
        return 1, 1
    count = 1
    depth = 1
    for child in node.children:
        child_count, child_depth = _count_and_depth(child)
        count += child_count
        depth = max(depth, 1 + child_depth)
    return count, depth


@dataclass(frozen=True, slots=True)
class ApplicabilityExpression:
    """A versioned, bounded applicability expression tree (spec 6.3).

    Bounds are enforced over the *whole* tree here, once, at construction --
    individual nodes are validated locally by their own `__post_init__` and know
    nothing about the tree they end up inside.
    """

    root: ApplicabilityNode
    version: str = APPLICABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require(
            isinstance(self.root, (BooleanNode, ComparisonNode)),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "ApplicabilityExpression.root must be a BooleanNode or ComparisonNode",
        )
        _require_id("ApplicabilityExpression.version", self.version)
        require(
            self.version == APPLICABILITY_CONTRACT_VERSION,
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "ApplicabilityExpression.version is unsupported",
        )
        count, depth = _count_and_depth(self.root)
        require(
            count <= MAX_NODES,
            GovernedKnowledgeErrorCode.NODE_LIMIT_EXCEEDED,
            f"expression has {count} nodes, exceeding the maximum of {MAX_NODES}",
        )
        require(
            depth <= MAX_DEPTH,
            GovernedKnowledgeErrorCode.DEPTH_LIMIT_EXCEEDED,
            f"expression has depth {depth}, exceeding the maximum of {MAX_DEPTH}",
        )


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def _dates_ordered(
    operator: ComparisonOperator, left: TemporalInstant, right: TemporalInstant
) -> TruthValue:
    """Order two instants, rejecting ambiguity rather than inventing precision.

    Two independently truncated UTC instants that differ are unambiguously
    ordered by that difference regardless of their declared precisions. Two that
    land on the same truncated instant are decisively equal only when their
    precisions also agree; otherwise which one is "earlier" within its own
    coarser precision is not determined by the data given, so ordering is
    reported unknown rather than guessed (spec 6.3: "reject ambiguous ... date
    comparisons rather than inventing precision").
    """
    left_start, left_end = _instant_bounds(left)
    right_start, right_end = _instant_bounds(right)
    identical = left_start == right_start and left_end == right_end
    entirely_before = left_end <= right_start
    entirely_after = left_start >= right_end
    if operator is ComparisonOperator.DATE_LT:
        if entirely_before:
            return TruthValue.TRUE
        if entirely_after or identical:
            return TruthValue.FALSE
    elif operator is ComparisonOperator.DATE_LTE:
        if entirely_before or identical:
            return TruthValue.TRUE
        if entirely_after:
            return TruthValue.FALSE
    elif operator is ComparisonOperator.DATE_GT:
        if entirely_after:
            return TruthValue.TRUE
        if entirely_before or identical:
            return TruthValue.FALSE
    else:  # DATE_GTE
        if entirely_after or identical:
            return TruthValue.TRUE
        if entirely_before:
            return TruthValue.FALSE
    return TruthValue.UNKNOWN


def _evaluate_comparison(
    node: ComparisonNode, snapshot: FactSnapshot, ledger: _Ledger
) -> TruthValue:
    assert node.left.fact_ref is not None
    value = ledger.consult(node.left.fact_ref, snapshot)
    if value is None:
        return TruthValue.UNKNOWN

    if node.operator is ComparisonOperator.EQUALS:
        assert node.right is not None
        return _values_equal(value, node.right)

    if node.operator is ComparisonOperator.MEMBER_OF:
        matches = [_values_equal(value, member) for member in node.members]
        return _any_of(matches)

    right = node.right
    assert right is not None
    if node.operator in _NUMERIC_OPERATORS:
        if value.kind is not OperandKind.NUMERIC or value.unit != right.unit:
            return TruthValue.UNKNOWN
        assert value.numeric_value is not None and right.numeric_value is not None
        if node.operator is ComparisonOperator.NUMERIC_LT:
            result = value.numeric_value < right.numeric_value
        elif node.operator is ComparisonOperator.NUMERIC_LTE:
            result = value.numeric_value <= right.numeric_value
        elif node.operator is ComparisonOperator.NUMERIC_GT:
            result = value.numeric_value > right.numeric_value
        else:
            result = value.numeric_value >= right.numeric_value
        return TruthValue.TRUE if result else TruthValue.FALSE

    if node.operator in _DATE_OPERATORS:
        if value.kind is not OperandKind.DATE:
            return TruthValue.UNKNOWN
        assert value.date_value is not None and right.date_value is not None
        return _dates_ordered(node.operator, value.date_value, right.date_value)

    raise AssertionError(
        f"unhandled comparison operator {node.operator!r}"
    )  # pragma: no cover


def _evaluate_node(
    node: ApplicabilityNode, snapshot: FactSnapshot, ledger: _Ledger
) -> TruthValue:
    if isinstance(node, ComparisonNode):
        return _evaluate_comparison(node, snapshot, ledger)
    child_values = [_evaluate_node(child, snapshot, ledger) for child in node.children]
    if node.operator is BooleanOperator.NOT:
        return negate(child_values[0])
    if node.operator is BooleanOperator.ALL:
        return _all_of(child_values)
    return _any_of(child_values)  # ANY


@dataclass(frozen=True, slots=True)
class ApplicabilityResult:
    """One applicability evaluation's outward-safe result (spec 6.3-6.4).

    `authorized_fact_refs` and `reasons` never name a fact the caller's
    `FactSnapshot` marked hidden or omitted -- see the module docstring.
    """

    outcome: ApplicabilityOutcome
    fact_snapshot_ref: str
    position_version_ref: str
    evaluator_version: str
    authorized_fact_refs: tuple[str, ...]
    reasons: tuple[str, ...]
    fact_provenance_classes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        require(
            isinstance(self.outcome, ApplicabilityOutcome),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "ApplicabilityResult.outcome must be an ApplicabilityOutcome",
        )
        require(
            tuple(sorted(self.fact_provenance_classes)) == self.fact_provenance_classes,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "fact_provenance_classes must be sorted by fact reference",
        )
        require(
            all(
                ref in self.authorized_fact_refs
                for ref, _ in self.fact_provenance_classes
            ),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "fact provenance may only name an authorised fact reference",
        )
        for name in (
            "fact_snapshot_ref",
            "position_version_ref",
            "evaluator_version",
        ):
            _require_id(f"ApplicabilityResult.{name}", getattr(self, name))
        require(
            self.evaluator_version == APPLICABILITY_CONTRACT_VERSION,
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "ApplicabilityResult.evaluator_version is unsupported",
        )


def evaluate_applicability(
    *,
    required: ApplicabilityExpression,
    exceptions: Sequence[ApplicabilityExpression] = (),
    facts: FactSnapshot,
    position_version_ref: str,
) -> ApplicabilityResult:
    """Evaluate one position's required conditions and exclusion exceptions.

    Outcome table (spec 6.3):

    - required proven false -> `not_applicable`, independent of any exception;
    - any exception proven true -> `not_applicable` (conclusive exclusion);
    - required unknown, or any exception unknown -> `needs_information`;
    - required true and every exception false -> `applicable`.
    """
    _require_id("position_version_ref", position_version_ref)
    for exception in exceptions:
        require(
            exception.version == required.version,
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "all applicability expressions must use the same evaluator version",
        )
    ledger = _Ledger()
    required_truth = _evaluate_node(required.root, facts, ledger)

    if required_truth is TruthValue.FALSE:
        return _build_result(
            ApplicabilityOutcome.NOT_APPLICABLE,
            facts.fact_snapshot_ref,
            position_version_ref,
            required.version,
            ledger,
            base_reason="A required condition is not satisfied.",
        )

    exception_truths = [_evaluate_node(exc.root, facts, ledger) for exc in exceptions]
    if any(truth is TruthValue.TRUE for truth in exception_truths):
        return _build_result(
            ApplicabilityOutcome.NOT_APPLICABLE,
            facts.fact_snapshot_ref,
            position_version_ref,
            required.version,
            ledger,
            base_reason="An excluding exception applies.",
        )

    if required_truth is TruthValue.UNKNOWN or any(
        truth is TruthValue.UNKNOWN for truth in exception_truths
    ):
        return _build_result(
            ApplicabilityOutcome.NEEDS_INFORMATION,
            facts.fact_snapshot_ref,
            position_version_ref,
            required.version,
            ledger,
        )

    return _build_result(
        ApplicabilityOutcome.APPLICABLE,
        facts.fact_snapshot_ref,
        position_version_ref,
        required.version,
        ledger,
        base_reason="All required conditions are satisfied and no exception applies.",
    )


def _build_result(
    outcome: ApplicabilityOutcome,
    fact_snapshot_ref: str,
    position_version_ref: str,
    evaluator_version: str,
    ledger: _Ledger,
    base_reason: str | None = None,
) -> ApplicabilityResult:
    authorized = tuple(sorted(ledger.resolved_visible | ledger.unresolved_visible))
    reasons: list[str] = []
    if base_reason is not None:
        reasons.append(base_reason)
    for ref in sorted(ledger.unresolved_visible):
        reasons.append(f"Required fact {ref!r} could not be resolved.")
    if ledger.hidden_unresolved:
        reasons.append(_HIDDEN_CONTEXT_REASON)
    return ApplicabilityResult(
        outcome=outcome,
        fact_snapshot_ref=fact_snapshot_ref,
        position_version_ref=position_version_ref,
        evaluator_version=evaluator_version,
        authorized_fact_refs=authorized,
        reasons=tuple(reasons),
        fact_provenance_classes=tuple(
            (ref, ledger.provenance[ref].value)
            for ref in sorted(ledger.provenance)
            if ref in authorized
        ),
    )


def contested_positions(
    applicable_position_ids: Iterable[str],
    contradictions: Iterable[tuple[str, str]],
) -> frozenset[str]:
    """Mark independently applicable positions `contested` via an explicit relation.

    Both members of a pair must already be independently `applicable`
    (`applicable_position_ids`, the caller's own evaluation results). No
    freshness, popularity, similarity or model-confidence signal is consulted or
    accepted here -- the only input is the caller-supplied, authorized
    `contradictions` relation (spec 6.5).
    """
    applicable = frozenset(applicable_position_ids)
    contested: set[str] = set()
    for left, right in contradictions:
        if left in applicable and right in applicable:
            contested.add(left)
            contested.add(right)
    return frozenset(contested)


__all__ = [
    "APPLICABILITY_CONTRACT_VERSION",
    "MAX_DEPTH",
    "MAX_NODES",
    "MAX_SET_MEMBERS",
    "ApplicabilityExpression",
    "ApplicabilityNode",
    "ApplicabilityOutcome",
    "ApplicabilityResult",
    "BooleanNode",
    "BooleanOperator",
    "ComparisonNode",
    "ComparisonOperator",
    "FactEntry",
    "FactProvenanceClass",
    "FactSnapshot",
    "Operand",
    "OperandKind",
    "TruthValue",
    "contested_positions",
    "date",
    "enum",
    "evaluate_applicability",
    "fact_ref",
    "literal",
    "negate",
    "numeric",
]
