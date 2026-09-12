"""The Phase 1 Semantic Registry authoring API (SR-101, SPEC-CORE-SEM-001 v0.2).

Manual authoring only, and deliberately so. There is no worker, no model, no
network and no automatic publication anywhere in this module: a person proposes
a change set, a person reviews it, and a person publishes an approved one. What
this layer adds over :mod:`~storage.semantic_registry` is the part that has to
be decided rather than stored -- how a change set applies to a base snapshot,
what version increment it forces, which consumers it would break, and whether a
publication may proceed at all.

*Preview writes nothing.* :meth:`SemanticRegistryService.preview` loads the base
snapshot, applies the supported operations, validates references and
preconditions, derives the canonical candidate snapshot and classifies consumer
impact, all through reads. A reviewer can ask "what would this do" as often as
they like and the answer is the same every time.

*Two digests, because there are two questions.* A candidate's `snapshot_digest`
addresses the element set a change set produces and nothing else, so previewing
the same proposal twice yields the same number before any identity is allocated.
A published version's `content_digest` additionally binds the `version_id` it
was published under, because that is what the immutable row is addressed by.

*Publication is atomic and idempotent, and it is checked in one order.* The
idempotency key is resolved first -- a replay must answer with what actually
happened even though the pointer has moved on since -- and only then are the
model, the pointer generation, the base, the approval's exact digest, the
validation outcome and consumer compatibility checked. Every refusal raises
inside the fenced transaction, so a refused publication leaves no version, no
publication record, no outbox fact and no pointer move behind.

*Phase 1 convenience, not a merged concept.* :meth:`SemanticRegistryService.publish`
activates in the same transaction it publishes in, because a Phase 1 caller has
no reason to hold an inert version. The repository still exposes the two writes
separately, and the activation log remains the only path that moves a pointer.
"""

from __future__ import annotations

import sqlite3
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Any, cast

from omnivia_core.semantic_registry import (
    ActionType,
    Alias,
    ChangeOperation,
    CompatibilityClassification,
    Concept,
    Constraint,
    ConstraintKind,
    ConsumerImpactFinding,
    LifecycleState,
    ModelVersion,
    OperationKind,
    Property,
    PropertyValueKind,
    Relationship,
    ReviewDecision,
    SemanticElement,
    SemanticRegistryError,
    Severity,
    ValidationFinding,
    VersionImpact,
    canonical_bytes,
    change_set_digest,
    classify_change_set,
    content_digest,
    model_version_digest,
    order_operations,
)
from omnivia_core.semantic_registry.ids import DEFAULT_ALLOCATOR, IdAllocator
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.semantic_registry import (
    ELEMENT_TYPES,
    ChangeSetRow,
    ConsumerRow,
    ModelRow,
    SemanticRegistryWriter,
    VersionRow,
    canonical_text,
    coerce_element_fields,
    find_change_set,
    next_sequence,
    project_model,
    read_change_set,
    read_consumers,
    read_model,
    read_pointer,
    read_publication,
    read_review,
    read_version,
    semantic_registry_writer,
    verify_version_digests,
)

#: The label a model's first version is published under.
GENESIS_LABEL = "1.0.0"

#: What the domain's change-set digest is told a genesis proposal's base is.
#: There is no base version and no base digest before the first publication,
#: and the empty string says exactly that in a form the digest can carry.
_NO_BASE = ""

#: The element type each `Add*` operation mints, by operation kind.
_ADDED_ELEMENT_KINDS: Mapping[OperationKind, str] = {
    OperationKind.ADD_CONCEPT: "concept",
    OperationKind.ADD_PROPERTY: "property",
    OperationKind.ADD_RELATIONSHIP: "relationship",
    OperationKind.ADD_CONSTRAINT: "constraint",
    OperationKind.ADD_ALIAS: "alias",
    OperationKind.ADD_VOCABULARY_MEMBER: "vocabulary_member",
    OperationKind.ADD_ACTION_TYPE: "action_type",
}

#: The lifecycle transition each governance operation records. An element is
#: never dropped from the snapshot for one of these when it carries a
#: lifecycle, because a stable ID other elements may still reference has to
#: remain resolvable; see :func:`_apply_lifecycle` for the two kinds that do
#: not carry one.
_LIFECYCLE_BY_KIND: Mapping[OperationKind, LifecycleState] = {
    OperationKind.DEPRECATE_ELEMENT: LifecycleState.DEPRECATED,
    OperationKind.REPLACE_ELEMENT: LifecycleState.REPLACED,
    OperationKind.REMOVE_ELEMENT: LifecycleState.REMOVED,
}

#: The two axiom operations the Phase 1 element model carries no field for. An
#: equivalence or a disjointness is a statement *about* a pair of elements, not
#: a property of one, so it is applied as the constraint element it already is
#: in the standards subset -- at a derived, deterministic stable ID so the same
#: operation applied twice updates one constraint rather than accumulating them.
_AXIOM_KINDS: Mapping[OperationKind, ConstraintKind] = {
    OperationKind.CHANGE_EQUIVALENCE: ConstraintKind.EQUIVALENCE,
    OperationKind.CHANGE_DISJOINTNESS: ConstraintKind.DISJOINTNESS,
}

#: Element fields naming another element, checked against the candidate
#: snapshot once every operation has been applied.
_REFERENCE_FIELDS = (
    "parent_concept_ids",
    "domain_id",
    "range_id",
    "subject_concept_id",
    "object_concept_id",
    "inverse_id",
    "target_element_id",
    "target_concept_id",
    "vocabulary_element_id",
)

#: Constraint parameter keys that name another element.
_REFERENCE_PARAMETERS = ("equivalent_element_id", "disjoint_with_element_id")

#: Sentinel for "this field does not exist on this element", distinct from any
#: real field value a `before` precondition could name.
_MISSING = object()

_BLOCKING = frozenset({Severity.ERROR, Severity.CRITICAL})
_ACCEPTABLE = frozenset(
    {
        CompatibilityClassification.COMPATIBLE,
        CompatibilityClassification.CONDITIONALLY_COMPATIBLE,
    }
)


class SemanticRegistryRefused(Exception):
    """A registry request was refused, with the stable reason it was refused for.

    One exception with a frozen `code`, not a class per refusal: a caller
    branches on the reason, and eleven near-empty subclasses would be eleven
    places to keep a reason list consistent.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"[{code}] {message}")


def _wall_us() -> int:
    return time.time_ns() // 1_000


# --- applying a change set ------------------------------------------------------


def _finding(
    index: int, code: str, message: str, element_id: str | None = None
) -> ValidationFinding:
    return ValidationFinding(
        finding_id=f"finding-{index}-{code}",
        stage="apply",
        severity=Severity.ERROR,
        code=code,
        message=message,
        affected_stable_ids=() if element_id is None else (element_id,),
    )


def _apply_lifecycle(
    element: SemanticElement, state: LifecycleState
) -> SemanticElement | None:
    """The element after a governance transition, or `None` if it leaves.

    An element that carries a lifecycle keeps its stable ID forever and changes
    state, because other elements may still reference that ID and a dangling
    reference is worse than a tombstone. `Alias` and `VocabularyMember` carry no
    lifecycle in the Phase 1 element model and nothing references them by ID, so
    for those two the transition is the row leaving the snapshot -- which is
    also what deprecating a label can honestly mean. Inventing a lifecycle field
    for them here would be inventing schema this layer does not own.
    """
    if not hasattr(element, "lifecycle_state"):
        return None
    return cast(
        SemanticElement,
        replace(cast(Any, element), lifecycle_state=state),
    )


def _axiom_constraint(
    target_element_id: str,
    constraint_kind: ConstraintKind,
    parameters: Mapping[str, Any],
) -> Constraint:
    return Constraint(
        element_id=f"{target_element_id}.{constraint_kind.value}",
        target_element_id=target_element_id,
        constraint_kind=constraint_kind,
        parameters=dict(parameters),
    )


def _referenced_ids(element: SemanticElement) -> tuple[str, ...]:
    referenced: list[str] = []
    for field_name in _REFERENCE_FIELDS:
        value = getattr(element, field_name, None)
        if isinstance(value, str):
            referenced.append(value)
        elif isinstance(value, tuple):
            referenced.extend(value)
    parameters = getattr(element, "parameters", None)
    if isinstance(parameters, Mapping):
        for key in _REFERENCE_PARAMETERS:
            value = parameters.get(key)
            if isinstance(value, str):
                referenced.append(value)
    return tuple(referenced)


def _stale_before_fields(
    element: SemanticElement, before: Mapping[str, Any]
) -> tuple[str, ...]:
    """The `before` fields that no longer match `element`'s current state.

    `before` is a partial canonical precondition (spec 11): only the fields it
    names are checked, and a name that is not a field of `element` at all
    counts as a mismatch, since it plainly cannot equal the current state.
    """
    stale: list[str] = []
    for field_name, expected in coerce_element_fields(before).items():
        actual = getattr(element, field_name, _MISSING)
        if actual is _MISSING or canonical_bytes(actual) != canonical_bytes(expected):
            stale.append(field_name)
    return tuple(stale)


def _alias_collision_findings(
    elements: Mapping[str, SemanticElement], index: int
) -> list[ValidationFinding]:
    """Aliases whose normalised (value, locale, scope) resolves to more than one target."""
    groups: dict[tuple[str, str | None, str], list[Alias]] = {}
    for element in elements.values():
        if not isinstance(element, Alias):
            continue
        key = (
            unicodedata.normalize("NFC", element.value),
            None if element.locale is None else unicodedata.normalize("NFC", element.locale),
            element.scope,
        )
        groups.setdefault(key, []).append(element)

    findings: list[ValidationFinding] = []
    for (value, locale, scope), aliases in groups.items():
        if len({alias.target_element_id for alias in aliases}) > 1:
            for alias in aliases:
                findings.append(
                    _finding(
                        index,
                        "alias_collision",
                        f"alias {value!r} (locale={locale!r}, scope={scope!r}) "
                        "resolves to more than one target",
                        alias.element_id,
                    )
                )
    return findings


def _concept_cycle_findings(
    elements: Mapping[str, SemanticElement], index: int
) -> list[ValidationFinding]:
    """Concepts whose `parent_concept_ids` edges form a cycle among each other.

    A concept cannot name itself as a parent (`Concept.__post_init__`), but two
    or more concepts can each name a valid parent that closes a cycle across
    the snapshot -- only visible once every operation has been applied.
    """
    concepts = {
        element_id: element
        for element_id, element in elements.items()
        if isinstance(element, Concept)
    }
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(concepts, WHITE)
    cyclic: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for parent in concepts[node].parent_concept_ids:
            if parent not in concepts:
                continue
            if color[parent] == GRAY:
                cyclic.update(stack[stack.index(parent) :])
            elif color[parent] == WHITE:
                visit(parent, stack)
        stack.pop()
        color[node] = BLACK

    for element_id in concepts:
        if color[element_id] == WHITE:
            visit(element_id, [])

    return [
        _finding(
            index,
            "concept_parent_cycle",
            f"{element_id!r} is part of a concept parent cycle",
            element_id,
        )
        for element_id in sorted(cyclic)
    ]


def _reference_kind_findings(
    elements: Mapping[str, SemanticElement], index: int
) -> list[ValidationFinding]:
    """Reference fields whose target exists but is the wrong element kind.

    Only the kinds the public types make unambiguous (spec 7.6/10.3): a
    relationship's subject/object and an action type's subject/target concept
    must be `Concept`; a property's domain, and an object property's range,
    must be `Concept`. A missing target is `unknown_reference`'s concern, not
    this one, so a target that does not exist at all is left alone here.
    """
    findings: list[ValidationFinding] = []

    def require_concept(owner: SemanticElement, target_id: str | None, field_name: str) -> None:
        if target_id is None:
            return
        target = elements.get(target_id)
        if target is not None and not isinstance(target, Concept):
            findings.append(
                _finding(
                    index,
                    "invalid_reference_kind",
                    f"{owner.element_id!r}.{field_name} must reference a Concept, "
                    f"not {type(target).__name__}",
                    owner.element_id,
                )
            )

    for element in elements.values():
        if isinstance(element, Relationship):
            require_concept(element, element.subject_concept_id, "subject_concept_id")
            require_concept(element, element.object_concept_id, "object_concept_id")
        elif isinstance(element, ActionType):
            require_concept(element, element.subject_concept_id, "subject_concept_id")
            require_concept(element, element.target_concept_id, "target_concept_id")
        elif isinstance(element, Property):
            require_concept(element, element.domain_id, "domain_id")
            if element.value_kind is PropertyValueKind.OBJECT:
                require_concept(element, element.range_id, "range_id")
    return findings


def apply_operations(
    base: Sequence[SemanticElement], operations: Sequence[ChangeOperation]
) -> tuple[tuple[SemanticElement, ...], tuple[ValidationFinding, ...]]:
    """Apply a change set's operations to a base snapshot, deterministically.

    Every one of the eighteen operation kinds is handled: the seven `Add*`
    kinds mint their element, the six direct `Change*` kinds replace the fields
    they name, the two axiom kinds resolve to a constraint element, and the
    three governance kinds record a lifecycle transition. Nothing here raises
    on a bad operation -- a refusal is a :class:`ValidationFinding`, because a
    reviewer needs to see every problem a candidate has, not only the first.

    Operations are applied in the order given, which callers obtain from
    :func:`order_operations`, so the same change set always yields the same
    snapshot.
    """
    elements: dict[str, SemanticElement] = {
        element.element_id: element for element in base
    }
    findings: list[ValidationFinding] = []

    for index, operation in enumerate(operations):
        if operation.proposed_element_id is not None:
            element_id = operation.proposed_element_id
            if element_id in elements:
                findings.append(
                    _finding(
                        index,
                        "duplicate_id",
                        f"{element_id!r} already exists in the base snapshot",
                        element_id,
                    )
                )
                continue
            element_type = ELEMENT_TYPES[_ADDED_ELEMENT_KINDS[operation.kind]]
            try:
                elements[element_id] = element_type(
                    element_id=element_id, **coerce_element_fields(operation.after)
                )
            except (SemanticRegistryError, TypeError, ValueError) as error:
                findings.append(
                    _finding(index, "invalid_element", str(error), element_id)
                )
            continue

        element_id = str(operation.target_element_id)
        element = elements.get(element_id)
        if element is None:
            findings.append(
                _finding(
                    index,
                    "unknown_reference",
                    f"{element_id!r} is not in the base snapshot",
                    element_id,
                )
            )
            continue
        if operation.base_payload_digest is not None and operation.base_payload_digest != content_digest(element):
            findings.append(
                _finding(
                    index,
                    "stale_base_digest",
                    f"{element_id!r} has changed since this operation was written",
                    element_id,
                )
            )
            continue
        if operation.before is not None:
            stale_fields = _stale_before_fields(element, operation.before)
            if stale_fields:
                findings.append(
                    _finding(
                        index,
                        "stale_before",
                        f"{element_id!r} no longer matches before field(s) "
                        f"{sorted(stale_fields)}",
                        element_id,
                    )
                )
                continue

        if operation.kind in _LIFECYCLE_BY_KIND:
            if operation.kind is OperationKind.REPLACE_ELEMENT:
                replacement = operation.after.get("replacement_element_id")
                if replacement not in elements:
                    findings.append(
                        _finding(
                            index,
                            "unknown_reference",
                            f"replacement {replacement!r} is not in the snapshot",
                            element_id,
                        )
                    )
                    continue
            transitioned = _apply_lifecycle(element, _LIFECYCLE_BY_KIND[operation.kind])
            if transitioned is None:
                del elements[element_id]
            else:
                elements[element_id] = transitioned
            continue

        if operation.kind in _AXIOM_KINDS:
            constraint = _axiom_constraint(
                element_id, _AXIOM_KINDS[operation.kind], operation.after
            )
            elements[constraint.element_id] = constraint
            continue

        try:
            elements[element_id] = replace(
                element, **coerce_element_fields(operation.after)
            )
        except (SemanticRegistryError, TypeError, ValueError) as error:
            findings.append(_finding(index, "invalid_change", str(error), element_id))

    structural_index = len(operations)
    for element in elements.values():
        for referenced in _referenced_ids(element):
            if referenced not in elements:
                findings.append(
                    _finding(
                        structural_index,
                        "unknown_reference",
                        f"{element.element_id!r} references unknown {referenced!r}",
                        element.element_id,
                    )
                )
    findings.extend(_alias_collision_findings(elements, structural_index))
    findings.extend(_concept_cycle_findings(elements, structural_index))
    findings.extend(_reference_kind_findings(elements, structural_index))
    return tuple(sorted(elements.values(), key=lambda e: e.element_id)), tuple(findings)


def next_label(base_label: str | None, impact: VersionImpact) -> str:
    """The version label an increment class produces from the current one."""
    if base_label is None:
        return GENESIS_LABEL
    major, minor, patch = (int(part) for part in base_label.split("."))
    if impact is VersionImpact.MAJOR:
        return f"{major + 1}.0.0"
    if impact is VersionImpact.MINOR:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# --- results -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Proposal:
    """One change set, whether or not this call is what created it."""

    change_set_id: str
    change_set_digest: str
    model_id: str
    base_version_id: str | None
    created: bool
    decision: str | None


@dataclass(frozen=True, slots=True)
class Candidate:
    """What a change set would publish, decided without writing anything."""

    change_set_id: str
    change_set_digest: str
    model_id: str
    base_version_id: str | None
    sequence: int
    label: str
    impact: VersionImpact
    snapshot_digest: str
    elements: tuple[SemanticElement, ...]
    findings: tuple[ValidationFinding, ...]
    consumer_impacts: tuple[ConsumerImpactFinding, ...]
    validation_digest: str

    @property
    def valid(self) -> bool:
        """Whether the candidate applied cleanly to its base."""
        return not any(finding.severity in _BLOCKING for finding in self.findings)

    @property
    def compatible(self) -> bool:
        """Whether every registered consumer of this model could take it."""
        return all(
            impact.classification in _ACCEPTABLE for impact in self.consumer_impacts
        )

    @property
    def publishable(self) -> bool:
        return self.valid and self.compatible


@dataclass(frozen=True, slots=True)
class Publication:
    """One published, activated version -- or the record of the original one."""

    publication_id: str
    version_id: str
    label: str
    sequence: int
    content_digest: str
    generation: int
    replayed: bool


def _consumer_impacts(
    consumers: Sequence[ConsumerRow],
    *,
    impact: VersionImpact,
    sequence: int,
    affected: tuple[str, ...],
) -> tuple[ConsumerImpactFinding, ...]:
    """How each registered consumer of the model would fare under a candidate.

    Dependencies are declared per model in 0026, not per element, so element
    granularity is not available to narrow this: a breaking change to a model
    is breaking for everything that declared it. Reported as `breaking` rather
    than guessed compatible, which is the failing direction that matters.
    """
    findings: list[ConsumerImpactFinding] = []
    for consumer in consumers:
        if impact is VersionImpact.MAJOR:
            classification = CompatibilityClassification.BREAKING
            reason = "the candidate is a breaking change to a model this consumer declares"
        elif sequence < consumer.min_sequence or (
            consumer.max_sequence is not None and sequence > consumer.max_sequence
        ):
            classification = CompatibilityClassification.MIGRATION_REQUIRED
            reason = "the candidate falls outside this consumer's supported range"
        else:
            classification = CompatibilityClassification.COMPATIBLE
            reason = "the candidate is inside this consumer's supported range"
        findings.append(
            ConsumerImpactFinding(
                consumer_id=consumer.consumer_id,
                classification=classification,
                affected_element_ids=affected,
                reason=reason,
            )
        )
    return tuple(findings)


def build_candidate(
    connection: sqlite3.Connection, *, workspace_id: str, change_set: ChangeSetRow
) -> Candidate:
    """Derive everything a publication decision needs, using only reads."""
    base: VersionRow | None = None
    if change_set.base_version_id is not None:
        base = read_version(
            connection,
            workspace_id=workspace_id,
            model_id=change_set.model_id,
            version_id=change_set.base_version_id,
        )
        if base is None:
            raise SemanticRegistryRefused(
                "unknown_base_version",
                f"change set {change_set.change_set_id!r} names a base that is gone",
            )
    elements, findings = apply_operations(
        () if base is None else base.elements, change_set.operations
    )
    impact = classify_change_set(change_set.operations)
    sequence = next_sequence(
        connection, workspace_id=workspace_id, model_id=change_set.model_id
    )
    affected = tuple(
        sorted(
            {
                str(operation.target_element_id or operation.proposed_element_id)
                for operation in change_set.operations
            }
        )
    )
    impacts = _consumer_impacts(
        read_consumers(
            connection, workspace_id=workspace_id, model_id=change_set.model_id
        ),
        impact=impact,
        sequence=sequence,
        affected=affected,
    )
    snapshot_digest = content_digest({"elements": list(elements)})
    return Candidate(
        change_set_id=change_set.change_set_id,
        change_set_digest=change_set.change_set_digest,
        model_id=change_set.model_id,
        base_version_id=change_set.base_version_id,
        sequence=sequence,
        label=next_label(None if base is None else base.label, impact),
        impact=impact,
        snapshot_digest=snapshot_digest,
        elements=elements,
        findings=findings,
        consumer_impacts=impacts,
        validation_digest=content_digest(
            {
                "change_set_digest": change_set.change_set_digest,
                "snapshot_digest": snapshot_digest,
                "findings": list(findings),
                "consumers": list(impacts),
            }
        ),
    )


# --- the service ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SemanticRegistryService:
    """Manual authoring, review and publication against one owned workspace.

    Bound to the authority tuple at construction rather than per call: every
    write below runs under the same lease and fencing generation, and a service
    whose caller could vary them per call would be a service whose writes are
    not obviously one owner's.
    """

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    workspace_id: str
    fencing_generation: int
    clock: Callable[[], int] = _wall_us
    allocator: IdAllocator = DEFAULT_ALLOCATOR

    def _writer(self) -> AbstractContextManager[SemanticRegistryWriter]:
        return semantic_registry_writer(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        )

    def _id(self, prefix: str) -> str:
        return f"{prefix}-{self.allocator.new_id()}"

    # --- models ---------------------------------------------------------------

    def create_model(
        self, model_id: str, *, model_kind: str = "organisational"
    ) -> ModelRow:
        """Register a model, and open its pointer at generation zero with it."""
        now_us = self.clock()
        with self._writer() as writer:
            writer.create_model(
                model_id=model_id, model_kind=model_kind, now_us=now_us
            )
        return ModelRow(
            model_id=model_id, model_kind=model_kind, created_at_us=now_us
        )

    def read_model(self, model_id: str) -> ModelRow | None:
        return read_model(
            self.connection, workspace_id=self.workspace_id, model_id=model_id
        )

    def current_generation(self, model_id: str) -> int:
        pointer = read_pointer(
            self.connection, workspace_id=self.workspace_id, model_id=model_id
        )
        if pointer is None:
            raise SemanticRegistryRefused(
                "unknown_model", f"no semantic model {model_id!r} in this workspace"
            )
        return pointer.generation

    # --- proposals ------------------------------------------------------------

    def propose(
        self, model_id: str, operations: Sequence[ChangeOperation]
    ) -> Proposal:
        """Record one proposal against the model's current version.

        The change-set digest is a function of the base and the canonically
        ordered operations, so re-proposing identical semantic content resolves
        to the proposal that already exists rather than creating a second one --
        including when that proposal was rejected, which is the case worth being
        explicit about: the rejection is retained and returned, so a caller sees
        the decision already made instead of quietly reopening the same work.
        """
        pointer = read_pointer(
            self.connection, workspace_id=self.workspace_id, model_id=model_id
        )
        if pointer is None:
            raise SemanticRegistryRefused(
                "unknown_model", f"no semantic model {model_id!r} in this workspace"
            )
        base_version_id = pointer.current_version_id
        base_digest = _NO_BASE
        if base_version_id is not None:
            base = read_version(
                self.connection,
                workspace_id=self.workspace_id,
                model_id=model_id,
                version_id=base_version_id,
            )
            if base is None:  # pragma: no cover - the pointer's FK forbids it
                raise SemanticRegistryRefused(
                    "unknown_base_version", "the current pointer names no version"
                )
            base_digest = base.content_digest
        ordered = order_operations(operations)
        digest = change_set_digest(base_version_id or _NO_BASE, base_digest, ordered)

        with self._writer() as writer:
            existing = find_change_set(
                self.connection,
                workspace_id=self.workspace_id,
                model_id=model_id,
                change_set_digest=digest,
            )
            if existing is not None:
                review = read_review(
                    self.connection,
                    workspace_id=self.workspace_id,
                    change_set_id=existing.change_set_id,
                )
                return Proposal(
                    change_set_id=existing.change_set_id,
                    change_set_digest=existing.change_set_digest,
                    model_id=model_id,
                    base_version_id=existing.base_version_id,
                    created=False,
                    decision=None if review is None else review.decision,
                )
            change_set_id = self._id("cs")
            writer.create_change_set(
                change_set_id=change_set_id,
                model_id=model_id,
                base_version_id=base_version_id,
                change_set_digest=digest,
                operations=ordered,
                now_us=self.clock(),
            )
        return Proposal(
            change_set_id=change_set_id,
            change_set_digest=digest,
            model_id=model_id,
            base_version_id=base_version_id,
            created=True,
            decision=None,
        )

    def read_change_set(self, change_set_id: str) -> ChangeSetRow:
        change_set = read_change_set(
            self.connection,
            workspace_id=self.workspace_id,
            change_set_id=change_set_id,
        )
        if change_set is None:
            raise SemanticRegistryRefused(
                "unknown_change_set", f"no change set {change_set_id!r}"
            )
        return change_set

    # --- review ---------------------------------------------------------------

    def request_review(self, change_set_id: str) -> str:
        """Submit a change set for review, and return the request's identity.

        One request per change set. 0026 admits a second one -- its uniqueness
        is per request, not per change set -- but a change set with two open
        requests has two places a decision could land and no answer to "what
        was decided", so the second is refused here rather than left to become
        an ambiguity nothing resolves. The check and the append happen inside
        the same fenced (`BEGIN IMMEDIATE`) transaction, so two concurrent
        callers cannot both see "no open review" and both open one: the second
        to acquire the write lock sees what the first committed.
        """
        review_request_id = self._id("rr")
        with self._writer() as writer:
            self.read_change_set(change_set_id)
            existing = read_review(
                self.connection,
                workspace_id=self.workspace_id,
                change_set_id=change_set_id,
            )
            if existing is not None:
                raise SemanticRegistryRefused(
                    "already_in_review",
                    f"change set {change_set_id!r} is already under review as "
                    f"{existing.review_request_id!r}",
                )
            writer.open_review(
                review_request_id=review_request_id,
                change_set_id=change_set_id,
                now_us=self.clock(),
            )
        return review_request_id

    def decide(
        self, change_set_id: str, *, reviewer_id: str, decision: ReviewDecision
    ) -> str:
        """Record the one decision a review request receives.

        An approval is written only for an approved decision, and it is bound to
        the change set's stored digest -- so an approval can never be inherited
        by a proposal with different content, because different content is a
        different change set with a digest of its own.
        """
        review = read_review(
            self.connection,
            workspace_id=self.workspace_id,
            change_set_id=change_set_id,
        )
        if review is None:
            raise SemanticRegistryRefused(
                "no_review_request", f"change set {change_set_id!r} is not in review"
            )
        if review.decision is not None:
            raise SemanticRegistryRefused(
                "already_decided",
                f"change set {change_set_id!r} was already {review.decision}",
            )
        if decision is ReviewDecision.REQUEST_CHANGE:
            # 0026 admits two decision values. A request for changes is answered
            # by proposing a different change set, which has a digest -- and so
            # an approval -- of its own; recording it as a third state here
            # would be recording it nowhere.
            raise SemanticRegistryRefused(
                "unsupported_decision",
                "Phase 1 records an approval or a rejection, not a change request",
            )
        change_set = self.read_change_set(change_set_id)
        review_decision_id = self._id("rd")
        stored = "approved" if decision is ReviewDecision.APPROVE else "rejected"
        with self._writer() as writer:
            writer.record_decision(
                review_decision_id=review_decision_id,
                review_request_id=review.review_request_id,
                reviewer_id=reviewer_id,
                decision=stored,
                now_us=self.clock(),
            )
            if stored == "approved":
                writer.record_approval(
                    approval_id=self._id("ap"),
                    change_set_id=change_set_id,
                    change_set_digest=change_set.change_set_digest,
                    review_decision_id=review_decision_id,
                    now_us=self.clock(),
                )
        return review_decision_id

    # --- consumers ------------------------------------------------------------

    def register_consumer(self, consumer_id: str) -> None:
        with self._writer() as writer:
            writer.register_consumer(consumer_id=consumer_id, now_us=self.clock())

    def declare_dependency(
        self,
        consumer_id: str,
        model_id: str,
        *,
        min_sequence: int = 0,
        max_sequence: int | None = None,
    ) -> None:
        """Declare a consumer's dependency on a model, over a supported range."""
        with self._writer() as writer:
            writer.declare_dependency(
                consumer_id=consumer_id,
                model_id=model_id,
                min_sequence=min_sequence,
                max_sequence=max_sequence,
                now_us=self.clock(),
            )

    def bind(self, consumer_id: str, model_id: str, version_id: str) -> None:
        """Bind a consumer to the exact version it runs against."""
        with self._writer() as writer:
            writer.bind_version(
                consumer_id=consumer_id,
                model_id=model_id,
                version_id=version_id,
                now_us=self.clock(),
            )

    def consumers(self, model_id: str) -> tuple[ConsumerRow, ...]:
        return read_consumers(
            self.connection, workspace_id=self.workspace_id, model_id=model_id
        )

    # --- preview and publication ----------------------------------------------

    def preview(self, change_set_id: str) -> Candidate:
        """What publishing this change set would produce. Writes nothing."""
        return build_candidate(
            self.connection,
            workspace_id=self.workspace_id,
            change_set=self.read_change_set(change_set_id),
        )

    def publish(
        self,
        change_set_id: str,
        *,
        idempotency_key: str,
        expected_generation: int,
        actor_id: str,
    ) -> Publication:
        """Publish an approved change set and activate it, atomically.

        Every check below happens inside the one fenced transaction that would
        do the writing, so a refusal at any of them leaves nothing behind.
        """
        request_digest = content_digest(
            {
                "change_set_id": change_set_id,
                "expected_generation": expected_generation,
            }
        )
        with self._writer() as writer:
            replayed = self._replay(idempotency_key, request_digest)
            if replayed is not None:
                return replayed

            change_set = self.read_change_set(change_set_id)
            pointer = read_pointer(
                self.connection,
                workspace_id=self.workspace_id,
                model_id=change_set.model_id,
            )
            if pointer is None:
                raise SemanticRegistryRefused(
                    "unknown_model", f"no model {change_set.model_id!r}"
                )
            if pointer.generation != expected_generation:
                raise SemanticRegistryRefused(
                    "stale_pointer_generation",
                    f"pointer is at generation {pointer.generation}, "
                    f"not the expected {expected_generation}",
                )
            if change_set.base_version_id != pointer.current_version_id:
                raise SemanticRegistryRefused(
                    "stale_base",
                    "the change set was written against a version that is no "
                    "longer current",
                )
            review = read_review(
                self.connection,
                workspace_id=self.workspace_id,
                change_set_id=change_set_id,
            )
            if review is None or review.decision != "approved":
                raise SemanticRegistryRefused(
                    "not_approved",
                    f"change set {change_set_id!r} carries no approved decision",
                )
            if review.approval_id is None:
                raise SemanticRegistryRefused(
                    "no_approval_record",
                    f"change set {change_set_id!r} was approved but carries no "
                    "approval record to publish under",
                )
            if review.approved_digest != change_set.change_set_digest:
                # Defence in depth, not the binding itself: 0026 checks an
                # approval's digest against its change set's own stored row at
                # insert, and both relations are append-only, so this cannot
                # differ while that guard stands. It is asserted here anyway
                # because publishing content an approval did not cover is the
                # one failure no later check would catch.
                raise SemanticRegistryRefused(
                    "approval_digest_mismatch",
                    "the approval on record does not bind this change set's digest",
                )

            candidate = build_candidate(
                self.connection,
                workspace_id=self.workspace_id,
                change_set=change_set,
            )
            if not candidate.valid:
                raise SemanticRegistryRefused(
                    "validation_failed",
                    "; ".join(finding.message for finding in candidate.findings),
                )
            if not candidate.compatible:
                raise SemanticRegistryRefused(
                    "incompatible_consumer",
                    "; ".join(
                        f"{impact.consumer_id}: {impact.classification.value}"
                        for impact in candidate.consumer_impacts
                        if impact.classification not in _ACCEPTABLE
                    ),
                )

            version = _version_for(candidate, version_id=self._id("mv"))
            publication_id = self._id("pub")
            now_us = self.clock()
            writer.publish_version(
                version=version,
                sequence=candidate.sequence,
                publication_id=publication_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                base_version_id=candidate.base_version_id,
                expected_pointer_generation=expected_generation,
                approval_id=review.approval_id,
                validation_digest=candidate.validation_digest,
                outbox_id=self._id("ob"),
                change_set_digest=candidate.change_set_digest,
                now_us=now_us,
            )
            generation = writer.activate_version(
                model_id=candidate.model_id,
                version_id=version.model_version_id,
                previous_version_id=pointer.current_version_id,
                audit_ref=self._id("aud"),
                actor_id=actor_id,
                now_us=now_us,
            )
        return Publication(
            publication_id=publication_id,
            version_id=version.model_version_id,
            label=version.version_label,
            sequence=candidate.sequence,
            content_digest=version.content_digest,
            generation=generation,
            replayed=False,
        )

    def _replay(
        self, idempotency_key: str, request_digest: str
    ) -> Publication | None:
        """The outcome this key already produced, if it produced one.

        One key means one outcome: the same request answers with the original
        publication, and a different request under the same key is a conflict
        rather than a second publication or an overwrite.
        """
        record = read_publication(
            self.connection,
            workspace_id=self.workspace_id,
            idempotency_key=idempotency_key,
        )
        if record is None:
            return None
        if record.request_digest != request_digest:
            raise SemanticRegistryRefused(
                "idempotency_conflict",
                f"{idempotency_key!r} already names a different publication request",
            )
        version = read_version(
            self.connection,
            workspace_id=self.workspace_id,
            model_id=record.model_id,
            version_id=record.result_version_id,
        )
        if version is None:  # pragma: no cover - the record's FK forbids it
            raise SemanticRegistryRefused(
                "unknown_version", "the recorded publication names no version"
            )
        return Publication(
            publication_id=record.publication_id,
            version_id=version.version_id,
            label=version.label,
            sequence=version.sequence,
            content_digest=version.content_digest,
            generation=record.resulting_pointer_generation,
            replayed=True,
        )

    # --- projection and verification ------------------------------------------

    def project(self, model_id: str) -> dict[str, Any]:
        """A neutral JSON view of a model, rebuilt from authoritative tables."""
        return project_model(
            self.connection, workspace_id=self.workspace_id, model_id=model_id
        )

    def export(self, model_id: str) -> str:
        """The projection as canonical JSON text.

        Text, not a file. Nothing this returns is authoritative: it is a
        rendering of rows that are, and writing it somewhere does not make the
        copy a second source of truth.
        """
        return canonical_text(self.project(model_id))

    def verify(self, model_id: str) -> tuple[str, ...]:
        """The versions whose stored content no longer matches their digest.

        Usable directly after a restore: it recomputes from the restored rows
        and compares against what those rows claim.
        """
        return verify_version_digests(
            self.connection, workspace_id=self.workspace_id, model_id=model_id
        )


def _version_for(candidate: Candidate, *, version_id: str) -> ModelVersion:
    """The immutable version a candidate becomes once it is given an identity.

    The digest is computed after the identity is allocated because a published
    version is addressed by both: two versions with the same elements but
    different identities are different published facts.
    """
    parents = () if candidate.base_version_id is None else (candidate.base_version_id,)
    draft = ModelVersion(
        model_version_id=version_id,
        model_id=candidate.model_id,
        version_sequence=candidate.sequence + 1,
        version_label=candidate.label,
        content_digest=f"sha256:{'0' * 64}",
        parent_version_ids=parents,
        elements=candidate.elements,
    )
    return replace(draft, content_digest=model_version_digest(draft))


__all__ = [
    "GENESIS_LABEL",
    "Candidate",
    "Proposal",
    "Publication",
    "SemanticRegistryRefused",
    "SemanticRegistryService",
    "apply_operations",
    "build_candidate",
    "next_label",
]
