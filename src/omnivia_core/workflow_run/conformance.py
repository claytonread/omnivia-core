"""Loading the accepted Workflow Run boundary corpus (V06-8, A8-N1).

Standard library only, and execution-free. This module reads
`docs/quality/fixtures/core-workflow-run-memory/workflow-run-cases.json` into
deeply immutable value objects, and refuses to return anything at all when the
bytes on disk are not the accepted corpus: a shortened, reordered, duplicated,
extended or otherwise altered corpus is a different corpus, whatever its
`fixture_id` says.

"Not the accepted corpus" is decided by digest, not by how well an edit hides.
Structural validation runs first so a doctored corpus still names its own
defect, and the SHA-256 of the bytes read is checked last: an edit that keeps
the structure legal -- a retitled case, a restated authority commit, reworded
prose -- is still refused, because none of those fields is something this
loader could otherwise constrain.

Two things it deliberately does not do. It does not execute a case, map it onto
an ingress recipe, or decide whether it passed -- that is
:mod:`~omnivia_core.workflow_run.ingress` and
:mod:`~omnivia_core.workflow_run.runner`. And it derives no outcome: everything
here is what the accepted fixture *expects*, held apart from what the harness
observes so the two can be compared rather than conflated.

Structural validation rather than JSON Schema validation, for the reason the
provider-SPI loader gives: this package declares no third-party dependency, and
a loader that imported an analyser would put it on the import path of every
consumer of the public contract. The corpus ships its own `schema.json`, which
the test suite validates against `jsonschema`.

Operation names and error codes are checked against the frozen catalogue and
the frozen taxonomy rather than against a restated list, so a corpus naming an
operation or a code this Core does not have is refused here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeVar, cast

from omnivia_core.contracts.v1.generated import FROZEN_ERROR_CODES
from omnivia_core.workflow_run.boundary import (
    CATALOGUE,
    BranchId,
    Disposition,
    MutationAuthority,
    PrincipalKind,
)

#: The SHA-256 of the accepted `workflow-run-cases.json`. This is the whole of
#: "exactly": every other constant below states *why* a corpus is refused, and
#: this one refuses the rest.
ACCEPTED_CORPUS_SHA256: Final = (
    "b6f5603061580ff1fae86176b08a89df99e473b3f9d612b1b9dea038941151db"
)

#: The corpus this loader accepts, exactly.
EXPECTED_FIXTURE_ID: Final = "core-workflow-run-memory-v1"
EXPECTED_SCHEMA_VERSION: Final = "1"
EXPECTED_STATUS: Final = "preparation_only"
EXPECTED_CASE_COUNT: Final = 24

#: The identifiers the accepted corpus fixes, in order.
EXPECTED_CASE_IDS: Final[tuple[str, ...]] = tuple(
    f"RUN-V-{index:02d}" for index in range(1, EXPECTED_CASE_COUNT + 1)
)

#: The category population the accepted corpus carries. A corpus whose totals
#: had drifted is a different corpus even if it still holds twenty-four cases.
EXPECTED_CATEGORY_TOTALS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "accepted_outcome_traceability": 1,
        "approval": 3,
        "cancellation_race": 2,
        "compression_invariance": 2,
        "cross_workspace_denial": 2,
        "idempotency": 2,
        "lineage_bounds": 1,
        "non_mutation": 6,
        "oversized_result": 2,
        "replay": 1,
        "retention_deletion": 2,
    }
)

#: The actor population. The load-bearing claim of the lane is stated over it:
#: twenty agent-actor cases, four with a human caller principal, and no agent
#: case reporting a canonical mutation.
EXPECTED_ACTOR_TOTALS: Final[Mapping[PrincipalKind, int]] = MappingProxyType(
    {PrincipalKind.AGENT: 20, PrincipalKind.HUMAN: 4}
)

#: Every safety flag the accepted corpus asserts, and the value it asserts.
#: Required, all of them: an omitted flag would read as a silent "no" rather
#: than as the corpus fixing an answer.
REQUIRED_SAFETY: Final[Mapping[str, bool]] = MappingProxyType(
    {
        "credentials_or_key_material": False,
        "executable_payloads": False,
        "expected_outcomes_are_fail_closed": True,
        "live_urls": False,
        "network_dependency": False,
        "private_or_customer_content": False,
        "synthetic_identifiers_only": True,
    }
)

_AUTHORITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "architecture_document",
        "boundary_document",
        "core_checkpoint_commit",
        "error_code_source",
        "operation_catalogue_source",
        "pm_base_commit",
    }
)

_ROOT_KEYS: Final[frozenset[str]] = frozenset(
    {"authority", "cases", "fixture_id", "safety", "schema_version", "status"}
)

_CASE_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "case_id",
        "category",
        "expected",
        "requirements",
        "run_context",
        "stimulus",
        "title",
    }
)

_CASE_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset(
    {"boundary_probe", "ingress", "provisional_dependencies"}
)

_RUN_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "agent_id",
        "bound_workspace_id",
        "caller_principal_kind",
        "run_id",
        "session_id",
        "task_node_id",
    }
)

_INGRESS_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"granted_capabilities", "operation", "purpose"}
)
_INGRESS_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset(
    {"idempotency_key", "requested_workspace_id"}
)

_PROBE_KEYS: Final[frozenset[str]] = frozenset(
    {"bound_octets", "field", "note", "octet_length"}
)

_BRANCH_KEYS: Final[frozenset[str]] = frozenset(
    {"branch_id", "canonical_mutation", "core_audit_event", "core_mutation", "note"}
)

_EXPECTED_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "canonical_mutation",
        "core_audit_event",
        "core_mutation",
        "disposition",
        "fail_closed",
        "mutation_authority",
        "notes",
    }
)
_EXPECTED_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset(
    {"error_code", "reconciliation_branches"}
)

_CASE_ID = re.compile(r"^RUN-V-\d{2}$")
_REQUIREMENT_ID = re.compile(r"^RUN-R-\d{2}$")
_DEPENDENCY_ID = re.compile(r"^RUN-P-\d{2}$")

_T = TypeVar("_T")
_E = TypeVar("_E", bound=Enum)


class CorpusError(ValueError):
    """The corpus on disk is not the corpus this loader accepts."""


def _freeze(value: object, field: str = "value") -> object:
    """Return `value` with every nested container replaced by an immutable one.

    Sets are refused rather than frozen: a `set` has no order to preserve, so
    any tuple this returned would be an order this function invented.
    """
    if isinstance(value, Mapping):
        items: Mapping[Any, Any] = value
        return MappingProxyType(
            {str(key): _freeze(item, field) for key, item in items.items()}
        )
    if isinstance(value, frozenset | set):
        raise TypeError(f"{field} must not hold a set: a set has no fixed order")
    if isinstance(value, list | tuple):
        entries: Sequence[Any] = value
        return tuple(_freeze(entry, field) for entry in entries)
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _flag(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean")
    return value


def _sequence(value: object, kind: type[_T], field: str) -> tuple[_T, ...]:
    # A `str` is a `Sequence[str]`, so it would pass a per-entry check silently.
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise TypeError(f"{field} must be a sequence")
    entries = tuple(cast("Sequence[object]", value))
    if not all(isinstance(entry, kind) for entry in entries):
        raise TypeError(f"{field} must hold only {kind.__name__} entries")
    return cast("tuple[_T, ...]", entries)


def _frozen(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    items: Mapping[Any, Any] = value
    return MappingProxyType(
        {str(key): _freeze(item, field) for key, item in items.items()}
    )


@dataclass(frozen=True, slots=True)
class RunContext:
    """Run-owned identity. Every field is runtime-owned and opaque to Core
    except `bound_workspace_id`, which Core validates."""

    run_id: str
    task_node_id: str
    agent_id: str
    session_id: str
    caller_principal_kind: PrincipalKind
    bound_workspace_id: str

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "task_node_id",
            "agent_id",
            "session_id",
            "bound_workspace_id",
        ):
            _text(getattr(self, name), name)
        if not isinstance(self.caller_principal_kind, PrincipalKind):
            raise TypeError("caller_principal_kind must be a PrincipalKind")


@dataclass(frozen=True, slots=True)
class Ingress:
    """One application-contract request the run makes into Core."""

    operation: str
    granted_capabilities: tuple[str, ...]
    purpose: str
    requested_workspace_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if _text(self.operation, "operation") not in CATALOGUE:
            raise ValueError(f"operation is not in the catalogue: {self.operation!r}")
        _text(self.purpose, "purpose")
        object.__setattr__(
            self,
            "granted_capabilities",
            _sequence(self.granted_capabilities, str, "granted_capabilities"),
        )
        for name in ("requested_workspace_id", "idempotency_key"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)


@dataclass(frozen=True, slots=True)
class BoundaryProbe:
    """An out-of-bounds value described rather than embedded."""

    field: str
    octet_length: int
    bound_octets: int
    note: str

    def __post_init__(self) -> None:
        _text(self.field, "field")
        _text(self.note, "note")
        for name in ("octet_length", "bound_octets"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ReconciliationBranch:
    """One legitimate outcome an indeterminate case resolves to."""

    branch_id: BranchId
    core_mutation: bool
    canonical_mutation: bool
    core_audit_event: bool
    note: str

    def __post_init__(self) -> None:
        if not isinstance(self.branch_id, BranchId):
            raise TypeError("branch_id must be a BranchId")
        for name in ("core_mutation", "canonical_mutation", "core_audit_event"):
            _flag(getattr(self, name), name)
        _text(self.note, "note")


@dataclass(frozen=True, slots=True)
class ExpectedOutcome:
    """What the accepted corpus fixes as the right answer for one case."""

    disposition: Disposition
    core_mutation: bool | None
    canonical_mutation: bool
    mutation_authority: MutationAuthority
    core_audit_event: bool | None
    fail_closed: bool
    notes: str
    error_code: str | None = None
    reconciliation_branches: tuple[ReconciliationBranch, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, Disposition):
            raise TypeError("disposition must be a Disposition")
        if not isinstance(self.mutation_authority, MutationAuthority):
            raise TypeError("mutation_authority must be a MutationAuthority")
        for name in ("core_mutation", "core_audit_event"):
            value = getattr(self, name)
            if value is not None:
                _flag(value, name)
        _flag(self.canonical_mutation, "canonical_mutation")
        if not _flag(self.fail_closed, "fail_closed"):
            raise ValueError("every accepted case asserts fail_closed")
        _text(self.notes, "notes")
        if self.error_code is not None and (
            _text(self.error_code, "error_code") not in FROZEN_ERROR_CODES
        ):
            raise ValueError(f"error_code is not frozen: {self.error_code!r}")
        object.__setattr__(
            self,
            "reconciliation_branches",
            _sequence(
                self.reconciliation_branches,
                ReconciliationBranch,
                "reconciliation_branches",
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowRunCase:
    """One declarative boundary case, exactly as the corpus states it."""

    case_id: str
    title: str
    category: str
    requirements: tuple[str, ...]
    run_context: RunContext
    stimulus: str
    expected: ExpectedOutcome
    provisional_dependencies: tuple[str, ...] = ()
    ingress: Ingress | None = None
    boundary_probe: BoundaryProbe | None = None

    def __post_init__(self) -> None:
        if not _CASE_ID.match(_text(self.case_id, "case_id")):
            raise ValueError(f"case_id is malformed: {self.case_id!r}")
        _text(self.title, "title")
        _text(self.stimulus, "stimulus")
        if _text(self.category, "category") not in EXPECTED_CATEGORY_TOTALS:
            raise ValueError(f"category is unknown: {self.category!r}")
        if not isinstance(self.run_context, RunContext):
            raise TypeError("run_context must be a RunContext")
        if not isinstance(self.expected, ExpectedOutcome):
            raise TypeError("expected must be an ExpectedOutcome")
        if self.ingress is not None and not isinstance(self.ingress, Ingress):
            raise TypeError("ingress must be an Ingress or None")
        if self.boundary_probe is not None and not isinstance(
            self.boundary_probe, BoundaryProbe
        ):
            raise TypeError("boundary_probe must be a BoundaryProbe or None")
        object.__setattr__(
            self, "requirements", _sequence(self.requirements, str, "requirements")
        )
        object.__setattr__(
            self,
            "provisional_dependencies",
            _sequence(self.provisional_dependencies, str, "provisional_dependencies"),
        )
        for value in self.requirements:
            if not _REQUIREMENT_ID.match(value):
                raise ValueError(f"requirement is malformed: {value!r}")
        for value in self.provisional_dependencies:
            if not _DEPENDENCY_ID.match(value):
                raise ValueError(f"dependency is malformed: {value!r}")


@dataclass(frozen=True, slots=True)
class WorkflowRunCorpus:
    """The loaded corpus, structurally validated."""

    fixture_id: str
    schema_version: str
    status: str
    authority: Mapping[str, Any]
    safety: Mapping[str, bool]
    cases: tuple[WorkflowRunCase, ...]

    def __post_init__(self) -> None:
        for name in ("fixture_id", "schema_version", "status"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "authority", _frozen(self.authority, "authority"))
        safety = _frozen(self.safety, "safety")
        if not all(isinstance(value, bool) for value in safety.values()):
            raise TypeError("every safety flag must be a boolean")
        object.__setattr__(self, "safety", safety)
        object.__setattr__(
            self, "cases", _sequence(self.cases, WorkflowRunCase, "cases")
        )

    def case(self, case_id: str) -> WorkflowRunCase:
        for candidate in self.cases:
            if candidate.case_id == case_id:
                return candidate
        raise KeyError(case_id)

    @property
    def category_totals(self) -> Mapping[str, int]:
        totals: dict[str, int] = {}
        for case in self.cases:
            totals[case.category] = totals.get(case.category, 0) + 1
        return MappingProxyType(totals)

    @property
    def actor_totals(self) -> Mapping[PrincipalKind, int]:
        totals: dict[PrincipalKind, int] = {}
        for case in self.cases:
            kind = case.run_context.caller_principal_kind
            totals[kind] = totals.get(kind, 0) + 1
        return MappingProxyType(totals)


def _mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CorpusError(f"{where} is not an object")
    return {str(key): item for key, item in value.items()}


def _keys(source: Mapping[str, Any], required: frozenset[str], where: str) -> None:
    missing = required - set(source)
    if missing:
        raise CorpusError(f"{where} is missing {sorted(missing)}")


def _no_unknown_keys(
    source: Mapping[str, Any], allowed: frozenset[str], where: str
) -> None:
    unknown = set(source) - allowed
    if unknown:
        raise CorpusError(f"{where} states unknown keys: {sorted(unknown)}")


def _string(source: Mapping[str, Any], key: str, where: str) -> str:
    value = source.get(key)
    if not isinstance(value, str):
        raise CorpusError(f"{where} has no string {key!r}")
    return value


def _optional_string(source: Mapping[str, Any], key: str, where: str) -> str | None:
    if key not in source:
        return None
    value = source[key]
    if not isinstance(value, str):
        raise CorpusError(f"{where} has a non-string {key!r}")
    return value


def _boolean(source: Mapping[str, Any], key: str, where: str) -> bool:
    value = source.get(key)
    if not isinstance(value, bool):
        raise CorpusError(f"{where} has no boolean {key!r}")
    return value


def _nullable_boolean(source: Mapping[str, Any], key: str, where: str) -> bool | None:
    if key not in source:
        raise CorpusError(f"{where} is missing {key!r}")
    value = source[key]
    if value is None or isinstance(value, bool):
        return value
    raise CorpusError(f"{where} has a non-boolean {key!r}")


def _integer(source: Mapping[str, Any], key: str, where: str) -> int:
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CorpusError(f"{where} has no integer {key!r}")
    return value


def _string_list(source: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = source.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CorpusError(f"{where} does not state {key!r} as strings")
    entries = tuple(str(item) for item in value)
    if len(set(entries)) != len(entries):
        raise CorpusError(f"{where} repeats an entry in {key!r}")
    return entries


def _enum(kind: type[_E], value: str, where: str, what: str) -> _E:
    try:
        return kind(value)
    except ValueError as error:
        raise CorpusError(f"{where} names unknown {what} {value!r}") from error


def _load_run_context(raw: object, where: str) -> RunContext:
    source = _mapping(raw, where)
    _keys(source, _RUN_CONTEXT_KEYS, where)
    _no_unknown_keys(source, _RUN_CONTEXT_KEYS, where)
    return RunContext(
        run_id=_string(source, "run_id", where),
        task_node_id=_string(source, "task_node_id", where),
        agent_id=_string(source, "agent_id", where),
        session_id=_string(source, "session_id", where),
        caller_principal_kind=_enum(
            PrincipalKind,
            _string(source, "caller_principal_kind", where),
            where,
            "principal kind",
        ),
        bound_workspace_id=_string(source, "bound_workspace_id", where),
    )


def _load_ingress(raw: object, where: str) -> Ingress:
    source = _mapping(raw, where)
    _keys(source, _INGRESS_REQUIRED_KEYS, where)
    _no_unknown_keys(source, _INGRESS_REQUIRED_KEYS | _INGRESS_OPTIONAL_KEYS, where)
    operation = _string(source, "operation", where)
    if operation not in CATALOGUE:
        raise CorpusError(f"{where} names an operation outside the catalogue")
    return Ingress(
        operation=operation,
        granted_capabilities=_string_list(source, "granted_capabilities", where),
        purpose=_string(source, "purpose", where),
        requested_workspace_id=_optional_string(
            source, "requested_workspace_id", where
        ),
        idempotency_key=_optional_string(source, "idempotency_key", where),
    )


def _load_probe(raw: object, where: str) -> BoundaryProbe:
    source = _mapping(raw, where)
    _keys(source, _PROBE_KEYS, where)
    _no_unknown_keys(source, _PROBE_KEYS, where)
    return BoundaryProbe(
        field=_string(source, "field", where),
        octet_length=_integer(source, "octet_length", where),
        bound_octets=_integer(source, "bound_octets", where),
        note=_string(source, "note", where),
    )


def _load_branch(raw: object, where: str) -> ReconciliationBranch:
    source = _mapping(raw, where)
    _keys(source, _BRANCH_KEYS, where)
    _no_unknown_keys(source, _BRANCH_KEYS, where)
    return ReconciliationBranch(
        branch_id=_enum(BranchId, _string(source, "branch_id", where), where, "branch"),
        core_mutation=_boolean(source, "core_mutation", where),
        canonical_mutation=_boolean(source, "canonical_mutation", where),
        core_audit_event=_boolean(source, "core_audit_event", where),
        note=_string(source, "note", where),
    )


def _load_expected(raw: object, where: str) -> ExpectedOutcome:
    source = _mapping(raw, where)
    _keys(source, _EXPECTED_REQUIRED_KEYS, where)
    _no_unknown_keys(source, _EXPECTED_REQUIRED_KEYS | _EXPECTED_OPTIONAL_KEYS, where)
    disposition = _enum(
        Disposition, _string(source, "disposition", where), where, "disposition"
    )
    error_code = _optional_string(source, "error_code", where)
    if (disposition is Disposition.REJECTED) != (error_code is not None):
        raise CorpusError(f"{where} names an error code only when it rejects")
    raw_branches = source.get("reconciliation_branches", [])
    if not isinstance(raw_branches, list):
        raise CorpusError(f"{where} does not state reconciliation branches as a list")
    branches = tuple(
        _load_branch(entry, f"{where} branch {position}")
        for position, entry in enumerate(raw_branches)
    )
    indeterminate = disposition is Disposition.INDETERMINATE_UNTIL_RECONCILED
    if indeterminate:
        if tuple(branch.branch_id for branch in branches) != (
            BranchId.COMMITTED_ONCE,
            BranchId.NOT_COMMITTED,
        ):
            raise CorpusError(
                f"{where} must enumerate committed_once then not_committed"
            )
    elif branches:
        raise CorpusError(f"{where} carries branches without being indeterminate")
    core_mutation = _nullable_boolean(source, "core_mutation", where)
    core_audit_event = _nullable_boolean(source, "core_audit_event", where)
    if indeterminate != (core_mutation is None):
        raise CorpusError(f"{where} fixes a Core-effect boolean it must not fix")
    if indeterminate != (core_audit_event is None):
        raise CorpusError(f"{where} fixes an audit boolean it must not fix")
    return ExpectedOutcome(
        disposition=disposition,
        core_mutation=core_mutation,
        canonical_mutation=_boolean(source, "canonical_mutation", where),
        mutation_authority=_enum(
            MutationAuthority,
            _string(source, "mutation_authority", where),
            where,
            "mutation authority",
        ),
        core_audit_event=core_audit_event,
        fail_closed=_boolean(source, "fail_closed", where),
        notes=_string(source, "notes", where),
        error_code=error_code,
        reconciliation_branches=branches,
    )


def _check_invariants(case: WorkflowRunCase) -> None:
    """The two claims the corpus must carry structurally, not in prose."""
    expected = case.expected
    if case.run_context.caller_principal_kind is PrincipalKind.AGENT:
        if expected.canonical_mutation:
            raise CorpusError(f"{case.case_id} lets an agent mutate canonical state")
        if expected.mutation_authority is not MutationAuthority.NONE:
            raise CorpusError(f"{case.case_id} gives an agent mutation authority")
        for branch in expected.reconciliation_branches:
            if branch.canonical_mutation:
                raise CorpusError(
                    f"{case.case_id} branch {branch.branch_id.value} mutates canonical"
                )
    if expected.canonical_mutation and (
        expected.mutation_authority is not MutationAuthority.VALIDATED_HUMAN_PRINCIPAL
        or expected.core_mutation is not True
    ):
        raise CorpusError(
            f"{case.case_id} changes canonical state without validated human authority"
        )
    if case.ingress is None and (
        expected.core_mutation is not False or expected.canonical_mutation
    ):
        raise CorpusError(f"{case.case_id} is run-local yet claims a Core effect")


def _load_case(raw: object, position: int) -> WorkflowRunCase:
    """Load one case, reporting a DTO's own refusal as a corpus refusal.

    The value objects guard themselves, and those guards raise `TypeError` and
    `ValueError`. A caller asking "is this the corpus we accept?" should get one
    exception type back, so they are re-raised here rather than leaking.
    """
    try:
        return _build_case(raw, position)
    except CorpusError:
        raise
    except (TypeError, ValueError) as error:
        raise CorpusError(f"case {position} is malformed: {error}") from error


def _build_case(raw: object, position: int) -> WorkflowRunCase:
    where = f"case {position}"
    source = _mapping(raw, where)
    _keys(source, _CASE_REQUIRED_KEYS, where)
    _no_unknown_keys(source, _CASE_REQUIRED_KEYS | _CASE_OPTIONAL_KEYS, where)
    case_id = _string(source, "case_id", where)
    if not _CASE_ID.match(case_id):
        raise CorpusError(f"{where} has a malformed case id {case_id!r}")
    case = WorkflowRunCase(
        case_id=case_id,
        title=_string(source, "title", where),
        category=_string(source, "category", where),
        requirements=_string_list(source, "requirements", case_id),
        run_context=_load_run_context(source["run_context"], f"{case_id} run_context"),
        stimulus=_string(source, "stimulus", where),
        expected=_load_expected(source["expected"], f"{case_id} expected"),
        provisional_dependencies=(
            _string_list(source, "provisional_dependencies", case_id)
            if "provisional_dependencies" in source
            else ()
        ),
        ingress=(
            _load_ingress(source["ingress"], f"{case_id} ingress")
            if "ingress" in source
            else None
        ),
        boundary_probe=(
            _load_probe(source["boundary_probe"], f"{case_id} boundary_probe")
            if "boundary_probe" in source
            else None
        ),
    )
    _check_invariants(case)
    return case


def load_corpus(path: Path) -> WorkflowRunCorpus:
    """Load and structurally validate the corpus file at `path`.

    Every rejection is a :class:`CorpusError`, an unreadable or unparseable file
    included: a caller handling "this is not the corpus we accept" should not
    also have to catch `OSError` and `JSONDecodeError` for the same question.

    The bytes are read once and both hashed and parsed, so the digest gate at
    the end cannot be answering about a different read than the one validated.
    """
    try:
        payload = path.read_bytes()
        raw = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError(f"the corpus at {path} could not be read: {error}") from error
    document = _mapping(raw, "the corpus")
    _keys(document, _ROOT_KEYS, "the corpus")
    _no_unknown_keys(document, _ROOT_KEYS, "the corpus")
    fixture_id = _string(document, "fixture_id", "the corpus")
    if fixture_id != EXPECTED_FIXTURE_ID:
        raise CorpusError(f"the corpus is {fixture_id!r}, not {EXPECTED_FIXTURE_ID!r}")
    schema_version = _string(document, "schema_version", "the corpus")
    if schema_version != EXPECTED_SCHEMA_VERSION:
        raise CorpusError(
            f"the corpus is schema version {schema_version!r},"
            f" not {EXPECTED_SCHEMA_VERSION!r}"
        )
    status = _string(document, "status", "the corpus")
    if status != EXPECTED_STATUS:
        raise CorpusError(f"the corpus is {status!r}, not {EXPECTED_STATUS!r}")

    authority = _mapping(document["authority"], "the corpus authority")
    _keys(authority, _AUTHORITY_KEYS, "the corpus authority")
    _no_unknown_keys(authority, _AUTHORITY_KEYS, "the corpus authority")

    safety = _mapping(document["safety"], "the corpus safety")
    _keys(safety, frozenset(REQUIRED_SAFETY), "the corpus safety")
    _no_unknown_keys(safety, frozenset(REQUIRED_SAFETY), "the corpus safety")
    for flag, required in REQUIRED_SAFETY.items():
        if _boolean(safety, flag, "the corpus safety") is not required:
            raise CorpusError(f"the corpus safety flag {flag!r} is not {required}")

    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        raise CorpusError("the corpus does not state cases as a list")
    cases = tuple(
        _load_case(entry, position) for position, entry in enumerate(raw_cases)
    )
    identifiers = tuple(case.case_id for case in cases)
    if len(set(identifiers)) != len(identifiers):
        raise CorpusError("the corpus repeats a case identifier")
    if identifiers != EXPECTED_CASE_IDS:
        raise CorpusError(
            "the corpus does not carry exactly RUN-V-01..RUN-V-24 in order"
        )

    corpus = WorkflowRunCorpus(
        fixture_id=fixture_id,
        schema_version=schema_version,
        status=status,
        authority=authority,
        safety={key: bool(value) for key, value in safety.items()},
        cases=cases,
    )
    if dict(corpus.category_totals) != dict(EXPECTED_CATEGORY_TOTALS):
        raise CorpusError("the corpus does not carry the accepted category totals")
    if dict(corpus.actor_totals) != dict(EXPECTED_ACTOR_TOTALS):
        raise CorpusError("the corpus does not carry the accepted actor totals")

    # Last, so a doctored corpus reports the rule it broke rather than only
    # "the digest differs": structurally legal is necessary, not sufficient.
    digest = hashlib.sha256(payload).hexdigest()
    if digest != ACCEPTED_CORPUS_SHA256:
        raise CorpusError(
            f"the corpus at {path} hashes to {digest},"
            f" not the accepted {ACCEPTED_CORPUS_SHA256}"
        )
    return corpus


__all__ = [
    "ACCEPTED_CORPUS_SHA256",
    "EXPECTED_ACTOR_TOTALS",
    "EXPECTED_CASE_COUNT",
    "EXPECTED_CASE_IDS",
    "EXPECTED_CATEGORY_TOTALS",
    "EXPECTED_FIXTURE_ID",
    "EXPECTED_SCHEMA_VERSION",
    "EXPECTED_STATUS",
    "REQUIRED_SAFETY",
    "BoundaryProbe",
    "CorpusError",
    "ExpectedOutcome",
    "Ingress",
    "ReconciliationBranch",
    "RunContext",
    "WorkflowRunCase",
    "WorkflowRunCorpus",
    "load_corpus",
]
