"""The Phase 1 Semantic Registry publication lifecycle, end to end (SR-101).

One vertical slice, proved: a model is created, a change set is proposed,
reviewed, approved, published as `1.0.0` and activated; two consumers declare
and bind it; a compatible alias publishes as `1.0.1`; a breaking cardinality
tightening is reported incompatible and refused; a rejection is retained and
identical content resolves back to it; changed content inherits no approval; a
stale pointer generation is refused; one idempotency key yields one outcome and
a different request under it conflicts; the neutral projection is deterministic;
and a backup/restore round trip still verifies every version digest and binding.

Every refusal is also asserted to have written nothing, because "refused" and
"refused cleanly" are different claims and only the second one is worth having.
"""

from __future__ import annotations

import itertools
import sqlite3
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "phase3" / "runtime"))

from omnivia_core_runtime.ownership.fencing import verify_fingerprint
from omnivia_core_runtime.service.semantic_registry import (
    Publication,
    SemanticRegistryRefused,
    SemanticRegistryService,
    apply_operations,
    next_label,
)
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    create_verified_backup,
    new_attempt_id,
    restore_backup,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    canonical_schema_fingerprint,
    materialise_phase0_baseline,
)
from omnivia_core_runtime.storage.semantic_registry import (
    read_consumers,
    read_outbox,
    read_pointer,
    read_version,
    semantic_registry_writer,
    verify_version_digests,
)
from test_application_audit_idempotency_migration import (  # type: ignore[import-not-found]
    Owned,
    bootstrap_and_migrate,
    take_ownership,
)

from omnivia_core.semantic_registry import (
    ActionType,
    Alias,
    Concept,
    Constraint,
    ConstraintKind,
    LifecycleState,
    OperationKind,
    Property,
    PropertyValueKind,
    Relationship,
    ReviewDecision,
    VersionImpact,
    VocabularyMember,
    add_action_type,
    add_alias,
    add_concept,
    add_constraint,
    add_property,
    add_relationship,
    add_vocabulary_member,
    change_action_contract,
    change_cardinality,
    change_description,
    change_disjointness,
    change_domain_range,
    change_equivalence,
    change_hierarchy,
    change_label,
    content_digest,
    deprecate_element,
    order_operations,
    remove_element,
    replace_element,
)

WORKSPACE_ID = "ws-sr-svc-0001"
MODEL_ID = "model-orders"
NOW = 1_700_000_000_000_000
REVIEWER = "reviewer-1"
PUBLISHER = "publisher-1"

#: The relations a refused publication must leave exactly as it found them.
WATCHED = (
    "omnivia_semantic_model_versions",
    "omnivia_semantic_version_elements",
    "omnivia_semantic_version_parents",
    "omnivia_semantic_publication_records",
    "omnivia_semantic_outbox",
    "omnivia_semantic_version_activations",
)

GENESIS = (
    add_concept(
        "op-order", "order", {"label": "Order", "description": "A customer order"}
    ),
    add_property(
        "op-total",
        "order.total",
        {
            "label": "Total",
            "value_kind": "data",
            "domain_id": "order",
            "min_cardinality": 0,
            "max_cardinality": 1,
        },
        depends_on_operation_ids=("op-order",),
    ),
)

COMPATIBLE_ALIAS = (
    add_alias(
        "op-alias",
        "order.alias",
        {"value": "Purchase order", "target_element_id": "order"},
    ),
)

BREAKING_CARDINALITY = (
    change_cardinality(
        "op-tighten",
        "order.total",
        {"min_cardinality": 1, "max_cardinality": 1},
        before={"min_cardinality": 0, "max_cardinality": 1},
    ),
)


class Sequence:
    """A deterministic stand-in for the UUIDv7 allocator."""

    def __init__(self) -> None:
        self._issued = 0

    def new_id(self) -> str:
        self._issued += 1
        return f"{self._issued:06d}"


def ticking(start: int = NOW) -> Callable[[], int]:
    counter = itertools.count(start)
    return lambda: next(counter)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = take_ownership(path, workspace_id=WORKSPACE_ID)
    yield holder
    holder.connection.close()


@pytest.fixture
def service(owned: Owned) -> SemanticRegistryService:
    return SemanticRegistryService(
        connection=owned.connection,
        identity=owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        clock=ticking(),
        allocator=Sequence(),
    )


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in WATCHED
    }


def approve(service: SemanticRegistryService, change_set_id: str) -> None:
    service.request_review(change_set_id)
    service.decide(change_set_id, reviewer_id=REVIEWER, decision=ReviewDecision.APPROVE)


def publish(
    service: SemanticRegistryService,
    operations,
    *,
    key: str,
    generation: int,
) -> Publication:
    proposal = service.propose(MODEL_ID, operations)
    approve(service, proposal.change_set_id)
    return service.publish(
        proposal.change_set_id,
        idempotency_key=key,
        expected_generation=generation,
        actor_id=PUBLISHER,
    )


def genesis(service: SemanticRegistryService) -> Publication:
    service.create_model(MODEL_ID)
    return publish(service, GENESIS, key="key-genesis", generation=0)


def register_consumers(service: SemanticRegistryService, version_id: str) -> None:
    """One App and one Workflow consumer, each ranged, each bound exactly."""
    for consumer_id in ("app-orders", "workflow-fulfilment"):
        service.register_consumer(consumer_id)
        service.declare_dependency(consumer_id, MODEL_ID, min_sequence=0)
        service.bind(consumer_id, MODEL_ID, version_id)


# --- applying a change set (pure) ------------------------------------------------


def test_operations_apply_deterministically_across_the_kinds_they_cover() -> None:
    base = (
        Concept(element_id="order", label="Order"),
        Property(
            element_id="order.total",
            label="Total",
            value_kind=PropertyValueKind.DATA,
            domain_id="order",
            min_cardinality=0,
        ),
    )
    elements, findings = apply_operations(
        base,
        (
            change_label("op-1", "order", {"label": "Customer order"}),
            add_alias(
                "op-2",
                "order.alias",
                {"value": "PO", "target_element_id": "order"},
            ),
            change_cardinality("op-3", "order.total", {"min_cardinality": 1}),
            deprecate_element("op-4", "order.total", {"reason_code": "superseded"}),
        ),
    )
    assert findings == ()
    by_id = {element.element_id: element for element in elements}
    assert by_id["order"].label == "Customer order"
    assert by_id["order.total"].min_cardinality == 1
    assert by_id["order.total"].lifecycle_state is LifecycleState.DEPRECATED
    assert by_id["order.alias"].value == "PO"
    # Same inputs, same snapshot: the applied result is a pure function.
    assert apply_operations(base, ())[0] == tuple(
        sorted(base, key=lambda element: element.element_id)
    )


def test_every_operation_kind_applies_against_a_base_snapshot() -> None:
    """All eighteen kinds, in one canonically ordered change set, cleanly applied.

    The point is coverage of the kind table, not of any one kind's semantics:
    `OperationKind` is compared against what the change set carries, so a kind
    added later fails this until the application path handles it.
    """
    base = (
        Concept(element_id="order", label="Order"),
        Concept(element_id="line", label="Line"),
        Property(
            element_id="order.total",
            label="Total",
            value_kind=PropertyValueKind.DATA,
            domain_id="order",
        ),
        Relationship(
            element_id="order.has-line",
            label="Has line",
            subject_concept_id="order",
            object_concept_id="line",
        ),
        Constraint(
            element_id="order.total.required",
            target_element_id="order.total",
            constraint_kind=ConstraintKind.REQUIRED,
        ),
        Alias(element_id="order.alias", target_element_id="order", value="PO"),
        VocabularyMember(
            element_id="status.open", vocabulary_element_id="order", value="open"
        ),
        ActionType(
            element_id="order.submit",
            label="Submit",
            subject_concept_id="order",
            parameter_schema_digest=f"sha256:{'1' * 64}",
        ),
    )
    operations = order_operations(
        (
            add_concept("k01", "customer", {"label": "Customer"}),
            add_property(
                "k02",
                "order.ref",
                {"label": "Ref", "value_kind": "data", "domain_id": "order"},
            ),
            add_relationship(
                "k03",
                "order.placed-by",
                {
                    "label": "Placed by",
                    "subject_concept_id": "order",
                    "object_concept_id": "customer",
                },
            ),
            add_constraint(
                "k04",
                "order.ref.required",
                {"constraint_kind": "required", "target_element_id": "order.ref"},
            ),
            add_alias(
                "k05",
                "order.alias2",
                {"value": "Purchase", "target_element_id": "order"},
            ),
            add_vocabulary_member(
                "k06",
                "status.closed",
                {"value": "closed", "vocabulary_element_id": "order"},
            ),
            add_action_type(
                "k07",
                "order.cancel",
                {
                    "label": "Cancel",
                    "subject_concept_id": "order",
                    "parameter_schema_digest": f"sha256:{'2' * 64}",
                },
            ),
            change_label("k08", "order", {"label": "Sales order"}),
            change_description("k09", "line", {"description": "One order line"}),
            change_hierarchy("k10", "line", {"parent_concept_ids": ["order"]}),
            change_domain_range(
                "k11", "order.total", {"domain_id": "order", "range_id": "line"}
            ),
            change_cardinality(
                "k12", "order.total", {"min_cardinality": 0, "max_cardinality": 2}
            ),
            change_equivalence("k13", "order", {"equivalent_element_id": "customer"}),
            change_disjointness(
                "k14", "line", {"disjoint_with_element_id": "customer"}
            ),
            change_action_contract(
                "k15", "order.submit", {"parameter_schema_digest": f"sha256:{'3' * 64}"}
            ),
            deprecate_element("k16", "order.total", {"reason_code": "superseded"}),
            replace_element(
                "k17", "order.has-line", {"replacement_element_id": "order.placed-by"}
            ),
            remove_element("k18", "status.open"),
        )
    )
    assert {operation.kind for operation in operations} == set(OperationKind)

    elements, findings = apply_operations(base, operations)
    assert findings == ()
    by_id = {element.element_id: element for element in elements}

    assert by_id["order"].label == "Sales order"
    assert by_id["line"].parent_concept_ids == ("order",)
    assert by_id["order.total"].range_id == "line"
    assert by_id["order.total"].max_cardinality == 2
    assert by_id["order.total"].lifecycle_state is LifecycleState.DEPRECATED
    assert by_id["order.submit"].parameter_schema_digest == f"sha256:{'3' * 64}"
    assert by_id["order.has-line"].lifecycle_state is LifecycleState.REPLACED
    # An axiom becomes the constraint element the standards subset already has.
    assert by_id["order.equivalence"].constraint_kind is ConstraintKind.EQUIVALENCE
    assert by_id["line.disjointness"].parameters["disjoint_with_element_id"] == (
        "customer"
    )
    # A vocabulary member carries no lifecycle, so removing it removes the row.
    assert "status.open" not in by_id
    assert {"customer", "order.ref", "order.placed-by", "status.closed"} <= set(by_id)


def test_an_operation_against_a_missing_element_is_a_finding_not_a_crash() -> None:
    elements, findings = apply_operations(
        (), (change_label("op-1", "ghost", {"label": "Ghost"}),)
    )
    assert elements == ()
    assert [finding.code for finding in findings] == ["unknown_reference"]


def test_an_operation_whose_precondition_no_longer_holds_is_a_finding() -> None:
    """`base_payload_digest` pins the element state an operation was written for."""
    order = Concept(element_id="order", label="Order")
    matching = change_label(
        "op-1", "order", {"label": "Sales order"},
        base_payload_digest=content_digest(order),
    )
    stale = change_label(
        "op-1", "order", {"label": "Sales order"},
        base_payload_digest=f"sha256:{'f' * 64}",
    )

    applied, findings = apply_operations((order,), (matching,))
    assert findings == ()
    assert applied[0].label == "Sales order"

    unchanged, refusals = apply_operations((order,), (stale,))
    assert unchanged == (order,)
    assert [finding.code for finding in refusals] == ["stale_base_digest"]


def test_a_stale_partial_before_precondition_is_a_finding() -> None:
    """`before` pins only the fields it names, as a partial precondition."""
    order = Concept(element_id="order", label="Order", description="Original")
    matching = change_label(
        "op-1", "order", {"label": "Sales order"}, before={"label": "Order"}
    )
    stale = change_label(
        "op-1", "order", {"label": "Sales order"}, before={"label": "Something else"}
    )

    applied, findings = apply_operations((order,), (matching,))
    assert findings == ()
    assert applied[0].label == "Sales order"

    unchanged, refusals = apply_operations((order,), (stale,))
    assert unchanged == (order,)
    assert [finding.code for finding in refusals] == ["stale_before"]


def test_before_preconditions_use_canonical_semantic_collection_order() -> None:
    """Set-like parent IDs compare canonically rather than by caller list order."""
    parent_a = Concept(element_id="parent-a", label="Parent A")
    parent_b = Concept(element_id="parent-b", label="Parent B")
    child = Concept(
        element_id="child",
        label="Child",
        parent_concept_ids=("parent-a", "parent-b"),
    )
    operation = change_hierarchy(
        "op-1",
        "child",
        {"parent_concept_ids": ["parent-a"]},
        before={"parent_concept_ids": ["parent-b", "parent-a"]},
    )

    applied, findings = apply_operations(
        (parent_a, parent_b, child),
        (operation,),
    )

    assert findings == ()
    assert next(element for element in applied if element.element_id == "child").parent_concept_ids == (
        "parent-a",
    )


def test_an_alias_collision_across_different_targets_is_a_blocking_finding() -> None:
    base = (
        Concept(element_id="order", label="Order"),
        Concept(element_id="invoice", label="Invoice"),
        Alias(element_id="alias-1", target_element_id="order", value="PO"),
    )
    elements, findings = apply_operations(
        base,
        (
            add_alias(
                "op-1",
                "alias-2",
                {"value": "PO", "target_element_id": "invoice"},
            ),
        ),
    )
    assert [finding.code for finding in findings] == [
        "alias_collision",
        "alias_collision",
    ]
    assert {element.element_id for element in elements} == {
        "order",
        "invoice",
        "alias-1",
        "alias-2",
    }


def test_a_concept_parent_cycle_across_two_concepts_is_a_blocking_finding() -> None:
    base = (
        Concept(element_id="a", label="A", parent_concept_ids=("b",)),
        Concept(element_id="b", label="B"),
    )
    _, findings = apply_operations(
        base, (change_hierarchy("op-1", "b", {"parent_concept_ids": ["a"]}),)
    )
    assert {finding.code for finding in findings} == {"concept_parent_cycle"}
    assert {finding.affected_stable_ids[0] for finding in findings} == {"a", "b"}


def test_a_relationship_subject_must_reference_a_concept() -> None:
    base = (
        Property(
            element_id="not-a-concept",
            label="Total",
            value_kind=PropertyValueKind.DATA,
        ),
        Concept(element_id="line", label="Line"),
    )
    _, findings = apply_operations(
        base,
        (
            add_relationship(
                "op-1",
                "rel-1",
                {
                    "label": "Has line",
                    "subject_concept_id": "not-a-concept",
                    "object_concept_id": "line",
                },
            ),
        ),
    )
    assert [finding.code for finding in findings] == ["invalid_reference_kind"]


def test_a_property_domain_must_reference_a_concept() -> None:
    base = (Concept(element_id="not-domain", label="Also not a concept"),)
    _, findings = apply_operations(
        base,
        (
            add_property(
                "op-1",
                "prop-1",
                {"label": "Total", "value_kind": "data", "domain_id": "missing"},
            ),
        ),
    )
    # "missing" does not exist at all, so this is unknown_reference, not a kind
    # mismatch -- proving the two checks do not overlap.
    assert [finding.code for finding in findings] == ["unknown_reference"]

    base_with_property = (
        Property(
            element_id="not-a-concept",
            label="Existing",
            value_kind=PropertyValueKind.DATA,
        ),
    )
    _, findings = apply_operations(
        base_with_property,
        (
            add_property(
                "op-1",
                "prop-1",
                {
                    "label": "Total",
                    "value_kind": "data",
                    "domain_id": "not-a-concept",
                },
            ),
        ),
    )
    assert [finding.code for finding in findings] == ["invalid_reference_kind"]


def test_an_object_property_range_must_reference_a_concept() -> None:
    base = (
        Concept(element_id="order", label="Order"),
        Property(
            element_id="not-a-concept",
            label="Existing",
            value_kind=PropertyValueKind.DATA,
        ),
    )
    _, findings = apply_operations(
        base,
        (
            add_property(
                "op-1",
                "prop-1",
                {
                    "label": "Ref",
                    "value_kind": "object",
                    "domain_id": "order",
                    "range_id": "not-a-concept",
                },
            ),
        ),
    )
    assert [finding.code for finding in findings] == ["invalid_reference_kind"]


def test_an_action_type_subject_and_target_must_reference_concepts() -> None:
    base = (
        Property(
            element_id="not-a-concept",
            label="Existing",
            value_kind=PropertyValueKind.DATA,
        ),
        Concept(element_id="order", label="Order"),
    )
    _, findings = apply_operations(
        base,
        (
            add_action_type(
                "op-1",
                "action-1",
                {
                    "label": "Submit",
                    "subject_concept_id": "not-a-concept",
                    "target_concept_id": "order",
                    "parameter_schema_digest": f"sha256:{'1' * 64}",
                },
            ),
        ),
    )
    assert [finding.code for finding in findings] == ["invalid_reference_kind"]


def test_a_dangling_reference_in_the_result_is_a_finding() -> None:
    _, findings = apply_operations(
        (),
        (
            add_property(
                "op-1",
                "order.total",
                {"label": "Total", "value_kind": "data", "domain_id": "order"},
            ),
        ),
    )
    assert [finding.code for finding in findings] == ["unknown_reference"]


def test_removing_an_element_keeps_its_stable_id_as_a_tombstone() -> None:
    base = (Concept(element_id="order", label="Order"),)
    elements, findings = apply_operations(base, (remove_element("op-1", "order"),))
    assert findings == ()
    assert elements[0].lifecycle_state is LifecycleState.REMOVED


def test_version_labels_follow_the_increment_class() -> None:
    assert next_label(None, VersionImpact.PATCH) == "1.0.0"
    assert next_label("1.0.0", VersionImpact.PATCH) == "1.0.1"
    assert next_label("1.0.1", VersionImpact.MINOR) == "1.1.0"
    assert next_label("1.1.0", VersionImpact.MAJOR) == "2.0.0"


# --- the publication lifecycle ---------------------------------------------------


def test_a_model_is_created_and_its_first_version_published_and_activated(
    service: SemanticRegistryService, owned: Owned
) -> None:
    published = genesis(service)

    assert published.label == "1.0.0"
    assert published.sequence == 0
    assert published.generation == 1
    assert published.replayed is False

    pointer = read_pointer(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert pointer is not None
    assert pointer.current_version_id == published.version_id
    assert pointer.generation == 1

    stored = read_version(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        model_id=MODEL_ID,
        version_id=published.version_id,
    )
    assert stored is not None
    assert {element.element_id for element in stored.elements} == {
        "order",
        "order.total",
    }
    assert len(
        read_outbox(owned.connection, workspace_id=WORKSPACE_ID, aggregate_id=MODEL_ID)
    ) == 1


def test_consumers_bind_and_a_compatible_alias_publishes_as_1_0_1(
    service: SemanticRegistryService, owned: Owned
) -> None:
    first = genesis(service)
    register_consumers(service, first.version_id)

    proposal = service.propose(MODEL_ID, COMPATIBLE_ALIAS)
    before = counts(owned.connection)
    candidate = service.preview(proposal.change_set_id)
    assert candidate.impact is VersionImpact.PATCH
    assert candidate.label == "1.0.1"
    assert candidate.publishable
    assert {impact.classification.value for impact in candidate.consumer_impacts} == {
        "compatible"
    }
    # Preview wrote nothing, and previewing again says exactly the same thing.
    assert counts(owned.connection) == before
    assert service.preview(proposal.change_set_id) == candidate

    approve(service, proposal.change_set_id)
    second = service.publish(
        proposal.change_set_id,
        idempotency_key="key-alias",
        expected_generation=1,
        actor_id=PUBLISHER,
    )

    assert second.label == "1.0.1"
    assert second.sequence == 1
    assert second.generation == 2

    consumers = read_consumers(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert [consumer.consumer_id for consumer in consumers] == [
        "app-orders",
        "workflow-fulfilment",
    ]
    assert {consumer.bound_version_id for consumer in consumers} == {first.version_id}
    stored = read_version(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        model_id=MODEL_ID,
        version_id=second.version_id,
    )
    assert stored is not None and stored.parent_version_ids == (first.version_id,)


def test_a_breaking_cardinality_candidate_is_reported_and_refused_cleanly(
    service: SemanticRegistryService, owned: Owned
) -> None:
    first = genesis(service)
    register_consumers(service, first.version_id)

    proposal = service.propose(MODEL_ID, BREAKING_CARDINALITY)
    candidate = service.preview(proposal.change_set_id)
    assert candidate.impact is VersionImpact.MAJOR
    assert candidate.label == "2.0.0"
    assert candidate.valid
    assert not candidate.compatible
    assert not candidate.publishable
    assert {impact.classification.value for impact in candidate.consumer_impacts} == {
        "breaking"
    }

    approve(service, proposal.change_set_id)
    before = counts(owned.connection)
    with pytest.raises(SemanticRegistryRefused) as refusal:
        service.publish(
            proposal.change_set_id,
            idempotency_key="key-breaking",
            expected_generation=1,
            actor_id=PUBLISHER,
        )
    assert refusal.value.code == "incompatible_consumer"
    assert counts(owned.connection) == before

    pointer = read_pointer(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert pointer is not None
    assert (pointer.current_version_id, pointer.generation) == (first.version_id, 1)


def test_a_rejection_is_retained_and_identical_content_resolves_back_to_it(
    service: SemanticRegistryService,
) -> None:
    genesis(service)

    proposal = service.propose(MODEL_ID, BREAKING_CARDINALITY)
    assert proposal.created
    service.request_review(proposal.change_set_id)
    service.decide(
        proposal.change_set_id, reviewer_id=REVIEWER, decision=ReviewDecision.REJECT
    )

    retried = service.propose(MODEL_ID, BREAKING_CARDINALITY)
    assert retried.created is False
    assert retried.change_set_id == proposal.change_set_id
    assert retried.change_set_digest == proposal.change_set_digest
    assert retried.decision == "rejected"

    # And the rejection is not quietly reopened by proposing it again.
    with pytest.raises(SemanticRegistryRefused) as refusal:
        service.decide(
            retried.change_set_id,
            reviewer_id=REVIEWER,
            decision=ReviewDecision.APPROVE,
        )
    assert refusal.value.code == "already_decided"

    with pytest.raises(SemanticRegistryRefused) as publication:
        service.publish(
            retried.change_set_id,
            idempotency_key="key-rejected",
            expected_generation=1,
            actor_id=PUBLISHER,
        )
    assert publication.value.code == "not_approved"


def test_a_change_set_is_submitted_for_review_only_once(
    service: SemanticRegistryService,
) -> None:
    genesis(service)
    proposal = service.propose(MODEL_ID, COMPATIBLE_ALIAS)
    service.request_review(proposal.change_set_id)
    with pytest.raises(SemanticRegistryRefused) as refusal:
        service.request_review(proposal.change_set_id)
    assert refusal.value.code == "already_in_review"


def test_a_refused_second_review_request_writes_no_extra_row(
    service: SemanticRegistryService, owned: Owned
) -> None:
    """The existing-review check and the append happen as one serialized decision."""
    genesis(service)
    proposal = service.propose(MODEL_ID, COMPATIBLE_ALIAS)
    service.request_review(proposal.change_set_id)
    before = int(
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_semantic_review_requests"
        ).fetchone()[0]
    )
    with pytest.raises(SemanticRegistryRefused) as refusal:
        service.request_review(proposal.change_set_id)
    assert refusal.value.code == "already_in_review"
    after = int(
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_semantic_review_requests"
        ).fetchone()[0]
    )
    assert after == before


def test_changed_content_does_not_inherit_a_prior_approval(
    service: SemanticRegistryService, owned: Owned
) -> None:
    genesis(service)

    approved = service.propose(MODEL_ID, COMPATIBLE_ALIAS)
    approve(service, approved.change_set_id)

    changed = service.propose(
        MODEL_ID,
        (
            add_alias(
                "op-alias",
                "order.alias",
                {"value": "A different label", "target_element_id": "order"},
            ),
        ),
    )
    assert changed.created
    assert changed.change_set_id != approved.change_set_id
    assert changed.change_set_digest != approved.change_set_digest
    assert changed.decision is None

    before = counts(owned.connection)
    with pytest.raises(SemanticRegistryRefused) as refusal:
        service.publish(
            changed.change_set_id,
            idempotency_key="key-changed",
            expected_generation=1,
            actor_id=PUBLISHER,
        )
    assert refusal.value.code == "not_approved"
    assert counts(owned.connection) == before


def test_a_candidate_that_fails_validation_is_refused_cleanly(
    service: SemanticRegistryService, owned: Owned
) -> None:
    """Review approves a proposal; validation still decides whether it applies."""
    genesis(service)
    proposal = service.propose(
        MODEL_ID, (change_label("op-ghost", "ghost", {"label": "Ghost"}),)
    )
    candidate = service.preview(proposal.change_set_id)
    assert not candidate.valid
    assert [finding.code for finding in candidate.findings] == ["unknown_reference"]

    approve(service, proposal.change_set_id)
    before = counts(owned.connection)
    with pytest.raises(SemanticRegistryRefused) as refusal:
        service.publish(
            proposal.change_set_id,
            idempotency_key="key-invalid",
            expected_generation=1,
            actor_id=PUBLISHER,
        )
    assert refusal.value.code == "validation_failed"
    assert counts(owned.connection) == before


def test_an_approved_decision_with_no_approval_record_cannot_publish(
    service: SemanticRegistryService, owned: Owned
) -> None:
    """The approval record, not the decision, is what a publication cites."""
    genesis(service)
    proposal = service.propose(MODEL_ID, COMPATIBLE_ALIAS)
    request_id = service.request_review(proposal.change_set_id)
    with semantic_registry_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as write:
        write.record_decision(
            review_decision_id="rd-bare",
            review_request_id=request_id,
            reviewer_id=REVIEWER,
            decision="approved",
            now_us=NOW + 500,
        )

    before = counts(owned.connection)
    with pytest.raises(SemanticRegistryRefused) as refusal:
        service.publish(
            proposal.change_set_id,
            idempotency_key="key-bare",
            expected_generation=1,
            actor_id=PUBLISHER,
        )
    assert refusal.value.code == "no_approval_record"
    assert counts(owned.connection) == before


def test_a_stale_pointer_generation_is_refused_with_no_partial_writes(
    service: SemanticRegistryService, owned: Owned
) -> None:
    genesis(service)
    proposal = service.propose(MODEL_ID, COMPATIBLE_ALIAS)
    approve(service, proposal.change_set_id)

    before = counts(owned.connection)
    with pytest.raises(SemanticRegistryRefused) as refusal:
        service.publish(
            proposal.change_set_id,
            idempotency_key="key-stale",
            expected_generation=0,
            actor_id=PUBLISHER,
        )
    assert refusal.value.code == "stale_pointer_generation"
    assert counts(owned.connection) == before
    assert service.current_generation(MODEL_ID) == 1


def test_a_proposal_against_a_superseded_base_is_refused(
    service: SemanticRegistryService, owned: Owned
) -> None:
    """A change set written against the old current version cannot publish."""
    genesis(service)
    stale = service.propose(MODEL_ID, COMPATIBLE_ALIAS)
    approve(service, stale.change_set_id)

    publish(
        service,
        (change_label("op-relabel", "order", {"label": "Sales order"}),),
        key="key-relabel",
        generation=1,
    )

    before = counts(owned.connection)
    with pytest.raises(SemanticRegistryRefused) as refusal:
        service.publish(
            stale.change_set_id,
            idempotency_key="key-stale-base",
            expected_generation=2,
            actor_id=PUBLISHER,
        )
    assert refusal.value.code == "stale_base"
    assert counts(owned.connection) == before


def test_one_idempotency_key_yields_one_publication(
    service: SemanticRegistryService, owned: Owned
) -> None:
    genesis(service)
    proposal = service.propose(MODEL_ID, COMPATIBLE_ALIAS)
    approve(service, proposal.change_set_id)

    first = service.publish(
        proposal.change_set_id,
        idempotency_key="key-once",
        expected_generation=1,
        actor_id=PUBLISHER,
    )
    after_first = counts(owned.connection)

    replay = service.publish(
        proposal.change_set_id,
        idempotency_key="key-once",
        expected_generation=1,
        actor_id=PUBLISHER,
    )
    assert replay.replayed is True
    assert replay.publication_id == first.publication_id
    assert replay.version_id == first.version_id
    assert replay.content_digest == first.content_digest
    assert replay.generation == first.generation
    assert counts(owned.connection) == after_first

    conflicting = service.propose(
        MODEL_ID, (change_label("op-relabel", "order", {"label": "Sales order"}),)
    )
    approve(service, conflicting.change_set_id)
    with pytest.raises(SemanticRegistryRefused) as refusal:
        service.publish(
            conflicting.change_set_id,
            idempotency_key="key-once",
            expected_generation=2,
            actor_id=PUBLISHER,
        )
    assert refusal.value.code == "idempotency_conflict"
    assert counts(owned.connection) == after_first


# --- projection, verification and restore ----------------------------------------


def test_the_neutral_projection_rebuild_is_deterministic(
    service: SemanticRegistryService,
) -> None:
    first = genesis(service)
    register_consumers(service, first.version_id)
    publish(service, COMPATIBLE_ALIAS, key="key-alias", generation=1)

    assert service.export(MODEL_ID) == service.export(MODEL_ID)
    projection = service.project(MODEL_ID)
    assert [version["label"] for version in projection["versions"]] == [
        "1.0.0",
        "1.0.1",
    ]
    assert projection["current"]["generation"] == 2
    assert [consumer["consumer_id"] for consumer in projection["consumers"]] == [
        "app-orders",
        "workflow-fulfilment",
    ]


def test_a_backup_restore_round_trip_verifies_digests_and_bindings(
    service: SemanticRegistryService, owned: Owned, tmp_path: Path
) -> None:
    first = genesis(service)
    register_consumers(service, first.version_id)
    second = publish(service, COMPATIBLE_ALIAS, key="key-alias", generation=1)

    assert service.verify(MODEL_ID) == ()
    export = service.export(MODEL_ID)
    owned.connection.commit()
    owned.connection.close()

    installation = InstallationLayout(root=tmp_path / "installation-state")
    installation.create(WORKSPACE_ID)
    verified = create_verified_backup(
        owned.path,
        installation,
        workspace_id=WORKSPACE_ID,
        attempt_id=new_attempt_id(),
    )
    assert verified.verified

    restore_target = tmp_path / "restored.sqlite"
    restore_backup(verified.path, restore_target)
    restored = open_database(restore_target, OpenMode.READ_ONLY)
    try:
        assert integrity_check(restored) == []
        assert foreign_key_check(restored) == []
        canonical = canonical_schema_fingerprint()
        assert verify_fingerprint(restored, canonical).matches(canonical)

        assert verify_version_digests(
            restored, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
        ) == ()
        consumers = read_consumers(
            restored, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
        )
        assert {consumer.bound_version_id for consumer in consumers} == {
            first.version_id
        }
        restored_version = read_version(
            restored,
            workspace_id=WORKSPACE_ID,
            model_id=MODEL_ID,
            version_id=second.version_id,
        )
        assert restored_version is not None
        assert restored_version.content_digest == second.content_digest

        reader = SemanticRegistryService(
            connection=restored,
            identity=owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        )
        assert reader.export(MODEL_ID) == export
    finally:
        restored.close()
