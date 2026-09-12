"""Acceptance tests for the Phase 1 Semantic Registry repository (SR-101).

Focused on what the repository is responsible for and migration 0037 is not: that a model
and its generation-zero pointer land together, that every one of the seven
element kinds survives the round trip through storage as the type it was, that
operations keep their canonical ordinals, that a publication writes its six
relations and exactly one outbox fact while leaving the pointer alone, that the
activation is the only thing that moves the pointer, and that a stored version's
digest can be recomputed from the rows it was stored as.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "phase3" / "runtime"))

from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.semantic_registry import (
    PUBLICATION_EVENT_KIND,
    canonical_text,
    find_change_set,
    next_sequence,
    project_model,
    read_change_set,
    read_consumers,
    read_model,
    read_outbox,
    read_pointer,
    read_publication,
    read_review,
    read_version,
    read_versions,
    semantic_registry_writer,
    verify_version_digests,
    version_of,
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
    ModelVersion,
    Property,
    PropertyValueKind,
    Relationship,
    VocabularyMember,
    add_concept,
    add_property,
    change_set_digest,
    content_digest,
    model_version_digest,
    operation_payload,
    order_operations,
)

WORKSPACE_ID = "ws-sr-repo-0001"
MODEL_ID = "model-orders"
NOW = 1_700_000_000_000_000

EVERY_ELEMENT_KIND = (
    Concept(element_id="order", label="Order", classifications=("core",)),
    Concept(element_id="line", label="Line", parent_concept_ids=("order",)),
    Property(
        element_id="order.total",
        label="Total",
        value_kind=PropertyValueKind.DATA,
        domain_id="order",
        min_cardinality=0,
        max_cardinality=1,
    ),
    Relationship(
        element_id="order.has-line",
        label="Has line",
        subject_concept_id="order",
        object_concept_id="line",
        characteristics=("functional",),
    ),
    Constraint(
        element_id="order.total.required",
        target_element_id="order.total",
        constraint_kind=ConstraintKind.REQUIRED,
        parameters={"min": 1},
    ),
    Alias(element_id="order.alias", target_element_id="order", value="Purchase"),
    VocabularyMember(
        element_id="status.open", vocabulary_element_id="order", value="open"
    ),
    ActionType(
        element_id="order.submit",
        label="Submit",
        subject_concept_id="order",
        parameter_schema_digest=f"sha256:{'1' * 64}",
        lifecycle_state=LifecycleState.ACTIVE,
    ),
)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = take_ownership(path, workspace_id=WORKSPACE_ID)
    yield holder
    holder.connection.close()


def writer(holder: Owned):
    return semantic_registry_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def version_for(
    *, version_id: str, sequence: int, label: str, elements=(), parents=()
) -> ModelVersion:
    draft = ModelVersion(
        model_version_id=version_id,
        model_id=MODEL_ID,
        version_sequence=sequence + 1,
        version_label=label,
        content_digest=f"sha256:{'0' * 64}",
        parent_version_ids=parents,
        elements=elements,
    )
    return ModelVersion(
        model_version_id=draft.model_version_id,
        model_id=draft.model_id,
        version_sequence=draft.version_sequence,
        version_label=draft.version_label,
        content_digest=model_version_digest(draft),
        parent_version_ids=draft.parent_version_ids,
        elements=draft.elements,
    )


def seed_model(holder: Owned) -> None:
    with writer(holder) as write:
        write.create_model(model_id=MODEL_ID, model_kind="organisational", now_us=NOW)


def publish_genesis(holder: Owned, elements=EVERY_ELEMENT_KIND) -> ModelVersion:
    """One approved change set, published and activated, the way a service does."""
    operations = order_operations(
        (add_concept("op-1", "order", {"label": "Order"}),)
    )
    digest = change_set_digest("", "", operations)
    with writer(holder) as write:
        write.create_change_set(
            change_set_id="cs-1",
            model_id=MODEL_ID,
            base_version_id=None,
            change_set_digest=digest,
            operations=operations,
            now_us=NOW + 1,
        )
        write.open_review(
            review_request_id="rr-1", change_set_id="cs-1", now_us=NOW + 2
        )
        write.record_decision(
            review_decision_id="rd-1",
            review_request_id="rr-1",
            reviewer_id="reviewer-1",
            decision="approved",
            now_us=NOW + 3,
        )
        write.record_approval(
            approval_id="ap-1",
            change_set_id="cs-1",
            change_set_digest=digest,
            review_decision_id="rd-1",
            now_us=NOW + 4,
        )
    version = version_for(
        version_id="mv-1", sequence=0, label="1.0.0", elements=elements
    )
    with writer(holder) as write:
        write.publish_version(
            version=version,
            sequence=0,
            publication_id="pub-1",
            idempotency_key="key-1",
            request_digest=f"sha256:{'2' * 64}",
            base_version_id=None,
            expected_pointer_generation=0,
            approval_id="ap-1",
            validation_digest=f"sha256:{'3' * 64}",
            outbox_id="ob-1",
            change_set_digest=digest,
            now_us=NOW + 5,
        )
        write.activate_version(
            model_id=MODEL_ID,
            version_id="mv-1",
            previous_version_id=None,
            audit_ref="aud-1",
            actor_id="publisher-1",
            now_us=NOW + 5,
        )
    return version


# --- models and pointers --------------------------------------------------------


def test_a_model_and_its_generation_zero_pointer_land_together(owned: Owned) -> None:
    seed_model(owned)

    model = read_model(owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID)
    pointer = read_pointer(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert model is not None and model.model_kind == "organisational"
    assert pointer is not None
    assert (pointer.current_version_id, pointer.generation) == (None, 0)


def test_a_model_write_outside_the_fence_is_refused(owned: Owned) -> None:
    """The repository weakens nothing: the guard still refuses an unfenced write."""
    from omnivia_core_runtime.storage.semantic_registry import SemanticRegistryWriter

    unfenced = SemanticRegistryWriter(
        connection=owned.connection, workspace_id=WORKSPACE_ID
    )
    with pytest.raises(sqlite3.DatabaseError):
        unfenced.create_model(
            model_id="model-sneaky", model_kind="organisational", now_us=NOW
        )
    assert count(owned.connection, "omnivia_semantic_models") == 0


# --- change sets ----------------------------------------------------------------


def test_operations_keep_their_ordinals_and_resolve_by_digest(owned: Owned) -> None:
    seed_model(owned)
    operations = order_operations(
        (
            add_property(
                "op-2",
                "order.total",
                {"label": "Total", "value_kind": "data", "domain_id": "order"},
                depends_on_operation_ids=("op-1",),
            ),
            add_concept("op-1", "order", {"label": "Order"}),
        )
    )
    assert [operation.operation_id for operation in operations] == ["op-1", "op-2"]
    digest = change_set_digest("", "", operations)

    with writer(owned) as write:
        write.create_change_set(
            change_set_id="cs-1",
            model_id=MODEL_ID,
            base_version_id=None,
            change_set_digest=digest,
            operations=operations,
            now_us=NOW + 1,
        )

    stored = read_change_set(
        owned.connection, workspace_id=WORKSPACE_ID, change_set_id="cs-1"
    )
    assert stored is not None
    assert [operation.operation_id for operation in stored.operations] == [
        "op-1",
        "op-2",
    ]
    assert stored.operations == operations
    assert stored.base_version_id is None

    found = find_change_set(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        model_id=MODEL_ID,
        change_set_digest=digest,
    )
    assert found is not None and found.change_set_id == "cs-1"


def test_operation_digest_excludes_rationale_but_operation_json_keeps_it(
    owned: Owned,
) -> None:
    seed_model(owned)
    with_rationale = add_concept(
        "op-1", "order", {"label": "Order"}, rationale="because reasons"
    )
    without_rationale = add_concept("op-1", "order", {"label": "Order"})
    operations = order_operations((with_rationale,))
    digest = change_set_digest("", "", operations)

    with writer(owned) as write:
        write.create_change_set(
            change_set_id="cs-1",
            model_id=MODEL_ID,
            base_version_id=None,
            change_set_digest=digest,
            operations=operations,
            now_us=NOW + 1,
        )

    stored_digest = owned.connection.execute(
        "SELECT operation_digest FROM omnivia_semantic_change_operations "
        "WHERE workspace_id = ? AND change_set_id = ? AND ordinal = 0",
        (WORKSPACE_ID, "cs-1"),
    ).fetchone()[0]
    assert stored_digest == content_digest(operation_payload(without_rationale))
    assert stored_digest != content_digest(with_rationale)

    stored = read_change_set(
        owned.connection, workspace_id=WORKSPACE_ID, change_set_id="cs-1"
    )
    assert stored is not None
    assert stored.operations[0].rationale == "because reasons"


def test_an_approval_binding_the_wrong_digest_is_refused(owned: Owned) -> None:
    seed_model(owned)
    operations = order_operations((add_concept("op-1", "order", {"label": "Order"}),))
    digest = change_set_digest("", "", operations)
    with writer(owned) as write:
        write.create_change_set(
            change_set_id="cs-1",
            model_id=MODEL_ID,
            base_version_id=None,
            change_set_digest=digest,
            operations=operations,
            now_us=NOW + 1,
        )
        write.open_review(
            review_request_id="rr-1", change_set_id="cs-1", now_us=NOW + 2
        )
        write.record_decision(
            review_decision_id="rd-1",
            review_request_id="rr-1",
            reviewer_id="reviewer-1",
            decision="approved",
            now_us=NOW + 3,
        )

    with pytest.raises(sqlite3.DatabaseError), writer(owned) as write:
        write.record_approval(
            approval_id="ap-1",
            change_set_id="cs-1",
            change_set_digest=f"sha256:{'9' * 64}",
            review_decision_id="rd-1",
            now_us=NOW + 4,
        )
    assert count(owned.connection, "omnivia_semantic_approval_records") == 0

    review = read_review(
        owned.connection, workspace_id=WORKSPACE_ID, change_set_id="cs-1"
    )
    assert review is not None
    assert (review.decision, review.approval_id) == ("approved", None)


# --- publication and activation -------------------------------------------------


def test_publication_writes_its_relations_and_exactly_one_outbox_fact(
    owned: Owned,
) -> None:
    seed_model(owned)
    version = publish_genesis(owned)

    stored = read_version(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        model_id=MODEL_ID,
        version_id="mv-1",
    )
    assert stored is not None
    assert (stored.label, stored.sequence) == ("1.0.0", 0)
    assert stored.content_digest == version.content_digest
    assert len(stored.elements) == len(EVERY_ELEMENT_KIND)

    publication = read_publication(
        owned.connection, workspace_id=WORKSPACE_ID, idempotency_key="key-1"
    )
    assert publication is not None
    assert publication.result_version_id == "mv-1"
    assert publication.expected_pointer_generation == 0
    assert publication.resulting_pointer_generation == 1

    outbox = read_outbox(
        owned.connection, workspace_id=WORKSPACE_ID, aggregate_id=MODEL_ID
    )
    assert len(outbox) == 1
    assert outbox[0].sequence == 0
    assert outbox[0].event_kind == PUBLICATION_EVENT_KIND
    assert outbox[0].payload["version_id"] == "mv-1"
    assert next_sequence(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    ) == 1


def test_every_element_kind_survives_the_round_trip_as_the_type_it_was(
    owned: Owned,
) -> None:
    seed_model(owned)
    publish_genesis(owned)

    stored = read_version(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        model_id=MODEL_ID,
        version_id="mv-1",
    )
    assert stored is not None
    assert stored.elements == tuple(
        sorted(EVERY_ELEMENT_KIND, key=lambda element: element.element_id)
    )
    for element in stored.elements:
        original = next(
            candidate
            for candidate in EVERY_ELEMENT_KIND
            if candidate.element_id == element.element_id
        )
        assert type(element) is type(original)


def test_the_activation_is_what_moves_the_pointer(owned: Owned) -> None:
    seed_model(owned)
    version = version_for(version_id="mv-1", sequence=0, label="1.0.0")

    with pytest.raises(sqlite3.DatabaseError), writer(owned) as write:
        write.publish_version(
            version=version,
            sequence=0,
            publication_id="pub-1",
            idempotency_key="key-1",
            request_digest=f"sha256:{'2' * 64}",
            base_version_id=None,
            expected_pointer_generation=0,
            approval_id="ap-1",
            validation_digest=f"sha256:{'3' * 64}",
            outbox_id="ob-1",
            change_set_digest=f"sha256:{'4' * 64}",
            now_us=NOW + 5,
        )

    # No approval row exists, so the publication record's own guard refused it and
    # the whole transaction rolled back -- version included.
    assert count(owned.connection, "omnivia_semantic_model_versions") == 0

    publish_genesis(owned)
    pointer = read_pointer(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert pointer is not None
    assert (pointer.current_version_id, pointer.generation) == ("mv-1", 1)


def test_a_second_activation_advances_the_generation_by_exactly_one(
    owned: Owned,
) -> None:
    seed_model(owned)
    publish_genesis(owned)
    second = version_for(
        version_id="mv-2", sequence=1, label="1.0.1", parents=("mv-1",)
    )
    with writer(owned) as write:
        write.publish_version(
            version=second,
            sequence=1,
            publication_id="pub-2",
            idempotency_key="key-2",
            request_digest=f"sha256:{'5' * 64}",
            base_version_id="mv-1",
            expected_pointer_generation=1,
            approval_id="ap-1",
            validation_digest=f"sha256:{'6' * 64}",
            outbox_id="ob-2",
            change_set_digest=f"sha256:{'7' * 64}",
            now_us=NOW + 6,
        )
        generation = write.activate_version(
            model_id=MODEL_ID,
            version_id="mv-2",
            previous_version_id="mv-1",
            audit_ref="aud-2",
            actor_id="publisher-1",
            now_us=NOW + 6,
        )

    assert generation == 2
    pointer = read_pointer(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert pointer is not None
    assert (pointer.current_version_id, pointer.generation) == ("mv-2", 2)
    versions = read_versions(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert [version.version_id for version in versions] == ["mv-1", "mv-2"]
    assert versions[1].parent_version_ids == ("mv-1",)
    assert len(read_outbox(
        owned.connection, workspace_id=WORKSPACE_ID, aggregate_id=MODEL_ID
    )) == 2


def test_a_stale_previous_version_refuses_the_activation(owned: Owned) -> None:
    seed_model(owned)
    publish_genesis(owned)
    with pytest.raises(sqlite3.DatabaseError), writer(owned) as write:
        write.activate_version(
            model_id=MODEL_ID,
            version_id="mv-1",
            previous_version_id=None,
            audit_ref="aud-2",
            actor_id="publisher-1",
            now_us=NOW + 9,
        )
    pointer = read_pointer(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert pointer is not None and pointer.generation == 1
    assert count(owned.connection, "omnivia_semantic_version_activations") == 1


# --- consumers ------------------------------------------------------------------


def test_a_consumer_declares_a_range_and_binds_an_exact_version(owned: Owned) -> None:
    seed_model(owned)
    publish_genesis(owned)
    with writer(owned) as write:
        write.register_consumer(consumer_id="app-orders", now_us=NOW + 6)
        write.declare_dependency(
            consumer_id="app-orders",
            model_id=MODEL_ID,
            min_sequence=0,
            max_sequence=None,
            now_us=NOW + 7,
        )
        write.bind_version(
            consumer_id="app-orders",
            model_id=MODEL_ID,
            version_id="mv-1",
            now_us=NOW + 8,
        )

    consumers = read_consumers(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert len(consumers) == 1
    assert consumers[0].consumer_id == "app-orders"
    assert consumers[0].min_sequence == 0
    assert consumers[0].max_sequence is None
    assert consumers[0].bound_version_id == "mv-1"


# --- projection and digest verification -----------------------------------------


def test_binding_a_second_exact_version_preserves_history_and_reports_the_latest(
    owned: Owned,
) -> None:
    seed_model(owned)
    publish_genesis(owned)
    second = version_for(
        version_id="mv-2", sequence=1, label="1.0.1", parents=("mv-1",)
    )
    with writer(owned) as write:
        write.publish_version(
            version=second,
            sequence=1,
            publication_id="pub-2",
            idempotency_key="key-2",
            request_digest=f"sha256:{'5' * 64}",
            base_version_id="mv-1",
            expected_pointer_generation=1,
            approval_id="ap-1",
            validation_digest=f"sha256:{'6' * 64}",
            outbox_id="ob-2",
            change_set_digest=f"sha256:{'7' * 64}",
            now_us=NOW + 6,
        )
        write.activate_version(
            model_id=MODEL_ID,
            version_id="mv-2",
            previous_version_id="mv-1",
            audit_ref="aud-2",
            actor_id="publisher-1",
            now_us=NOW + 6,
        )
        write.register_consumer(consumer_id="app-orders", now_us=NOW + 7)
        write.declare_dependency(
            consumer_id="app-orders",
            model_id=MODEL_ID,
            min_sequence=0,
            max_sequence=None,
            now_us=NOW + 8,
        )
        write.bind_version(
            consumer_id="app-orders",
            model_id=MODEL_ID,
            version_id="mv-1",
            now_us=NOW + 9,
        )
        write.bind_version(
            consumer_id="app-orders",
            model_id=MODEL_ID,
            version_id="mv-2",
            now_us=NOW + 10,
        )

    consumers = read_consumers(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert len(consumers) == 1
    assert consumers[0].bound_version_id == "mv-2"

    history = owned.connection.execute(
        "SELECT version_id FROM omnivia_semantic_consumer_version_bindings "
        "WHERE workspace_id = ? AND consumer_id = ? ORDER BY bound_at_us",
        (WORKSPACE_ID, "app-orders"),
    ).fetchall()
    assert [row[0] for row in history] == ["mv-1", "mv-2"]


def test_the_projection_is_rebuilt_from_tables_and_is_deterministic(
    owned: Owned,
) -> None:
    seed_model(owned)
    publish_genesis(owned)
    with writer(owned) as write:
        write.register_consumer(consumer_id="app-orders", now_us=NOW + 6)
        write.declare_dependency(
            consumer_id="app-orders",
            model_id=MODEL_ID,
            min_sequence=0,
            max_sequence=3,
            now_us=NOW + 7,
        )

    first = project_model(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    second = project_model(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    )
    assert canonical_text(first) == canonical_text(second)
    assert first["current"] == {"version_id": "mv-1", "generation": 1}
    assert first["versions"][0]["label"] == "1.0.0"
    assert first["consumers"] == [
        {
            "consumer_id": "app-orders",
            "min_sequence": 0,
            "max_sequence": 3,
            "bound_version_id": None,
        }
    ]


def test_a_stored_version_digest_recomputes_from_the_rows_it_was_stored_as(
    owned: Owned,
) -> None:
    seed_model(owned)
    version = publish_genesis(owned)

    assert verify_version_digests(
        owned.connection, workspace_id=WORKSPACE_ID, model_id=MODEL_ID
    ) == ()

    stored = read_version(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        model_id=MODEL_ID,
        version_id="mv-1",
    )
    assert stored is not None
    assert model_version_digest(version_of(stored)) == version.content_digest
    # The stored snapshot carries stable model identity and elements, never the
    # version's own generated identity or version-framing (spec 7.5).
    content = json.loads(stored.content_json)
    assert content["model_id"] == MODEL_ID
    assert "version_label" not in content
    assert "model_version_id" not in content
