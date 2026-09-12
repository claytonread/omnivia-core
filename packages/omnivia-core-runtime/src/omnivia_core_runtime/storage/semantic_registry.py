"""Authoritative persistence for the Phase 1 Semantic Registry (SR-101).

Migration 0026 owns the invariants and it is the authority, not this module: a
version's `sequence` and an outbox aggregate's `sequence` are contiguous, every
append-only relation refuses UPDATE and DELETE outright, an approval is checked
against the change set's own stored digest rather than the one a caller passes,
one `idempotency_key` yields one publication record, and the current pointer
moves only when a matching `omnivia_semantic_version_activations` row is
inserted. So the Python here allocates nothing structural: it canonicalises
content, issues the statements and reads state back, and it never restates a
rule the schema already enforces -- a second copy of a rule is a second place
for it to be wrong.

*Publication and activation are separate writes.* :meth:`SemanticRegistryWriter.publish_version`
records the immutable version, its elements, its parent edge, the publication
record and the one outbox fact; :meth:`SemanticRegistryWriter.activate_version`
records why the pointer moved and then moves it. They are distinct because they
answer different questions -- "does this version exist" and "is it the one in
force" -- and Phase 2 will want to publish without activating. That the Phase 1
service happens to call both inside one transaction does not merge them.

*The writer issues into a transaction it did not open.* `BEGIN IMMEDIATE` does
not nest, and publication has to commit six relations together or none of them,
so the fence belongs to the composition rather than to each statement:
:func:`semantic_registry_writer` opens exactly one and lends out the writes.

*Elements are stored with their kind.* A `Concept` and a `Relationship` are
different dataclasses whose field names overlap, so `element_json` carries a
`kind` discriminator beside the element's own canonical payload. Without it a
restored row parses into something, but not necessarily into what was written,
and the digest verification below would be checking a guess.

Every read takes the caller's workspace and filters on it in SQL. A workspace
that arrives inside a record is never the one a query runs against.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from omnivia_core.semantic_registry import (
    ActionType,
    Alias,
    ChangeOperation,
    Concept,
    Constraint,
    ConstraintKind,
    LifecycleState,
    ModelVersion,
    OperationKind,
    Property,
    PropertyValueKind,
    Relationship,
    SemanticElement,
    VocabularyMember,
    canonical_bytes,
    content_digest,
    model_version_digest,
    model_version_payload,
    operation_payload,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.semantic_events import SEMANTIC_VERSION_PUBLISHED_V1

#: The versioned wire event an activated publication puts on the outbox.
PUBLICATION_EVENT_KIND = SEMANTIC_VERSION_PUBLISHED_V1

#: What an activation's audit event records. The Phase 1 registry is called
#: in-process with no request envelope, so the activation's own `audit_ref`
#: stands in for the request, correlation and trace identifiers rather than
#: three invented ones.
_ACTIVATION_OPERATION = "semantic.registry.activate"
_ACTIVATION_PURPOSE = "semantic_registry_publication"
_ACTIVATION_AUTHORITY_JSON = '{"scopes":["semantic.publish"]}'

#: The seven element kinds, by the discriminator stored beside each element.
#: Public because the operation-application path above this module mints
#: elements through exactly this table, and two copies of it would be two
#: answers to "what does `AddConcept` create".
ELEMENT_TYPES: Mapping[str, type[SemanticElement]] = {
    "action_type": ActionType,
    "alias": Alias,
    "concept": Concept,
    "constraint": Constraint,
    "property": Property,
    "relationship": Relationship,
    "vocabulary_member": VocabularyMember,
}
_ELEMENT_KINDS: Mapping[type[SemanticElement], str] = {
    element_type: kind for kind, element_type in ELEMENT_TYPES.items()
}

#: The element fields JSON cannot carry faithfully on its own: an enum arrives
#: as its string value and an id set arrives as a list. Keyed by field name
#: rather than by element type because the names are unambiguous across all
#: seven element kinds, and one table is one place to correct.
_FIELD_COERCERS: Mapping[str, Callable[[Any], Any]] = {
    "value_kind": PropertyValueKind,
    "constraint_kind": ConstraintKind,
    "lifecycle_state": LifecycleState,
    "parent_concept_ids": tuple,
    "classifications": tuple,
    "characteristics": tuple,
}


def coerce_element_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """Lower JSON-shaped element field values back to their declared types."""
    return {
        name: _FIELD_COERCERS[name](value)
        if value is not None and name in _FIELD_COERCERS
        else value
        for name, value in values.items()
    }


def _element_document(element: SemanticElement) -> dict[str, Any]:
    kind = _ELEMENT_KINDS.get(type(element))
    if kind is None:  # pragma: no cover - the union is closed
        raise StorageError(f"unknown semantic element type {type(element).__name__}")
    return {"kind": kind, "element": element}


def _element_from_json(document: str) -> SemanticElement:
    parsed = json.loads(document)
    element_type = ELEMENT_TYPES.get(parsed.get("kind"))
    if element_type is None:
        raise StorageError(f"unknown stored element kind {parsed.get('kind')!r}")
    return element_type(**coerce_element_fields(parsed["element"]))


def _operation_from_json(document: str) -> ChangeOperation:
    parsed = json.loads(document)
    parsed["kind"] = OperationKind(parsed["kind"])
    for name in ("depends_on_operation_ids", "evidence_refs"):
        parsed[name] = tuple(parsed[name])
    return ChangeOperation(**parsed)


def canonical_text(value: Any) -> str:
    """Canonical JSON text for `value`, in the form 0026's json() checks accept."""
    return canonical_bytes(value).decode("utf-8")


def version_of(row: VersionRow) -> ModelVersion:
    """The domain version one stored row and its elements reconstruct.

    0026 numbers a model's versions from zero and the domain numbers them from
    one; both mean "the first version", and the offset lives here so nothing
    above this module has to remember which space it is holding.
    """
    return ModelVersion(
        model_version_id=row.version_id,
        model_id=row.model_id,
        version_sequence=row.sequence + 1,
        version_label=row.label,
        content_digest=row.content_digest,
        parent_version_ids=row.parent_version_ids,
        elements=row.elements,
    )


# --- read records --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelRow:
    """One registered model identity."""

    model_id: str
    model_kind: str
    created_at_us: int


@dataclass(frozen=True, slots=True)
class PointerRow:
    """A model's one mutable pointer, and the generation it is at."""

    model_id: str
    current_version_id: str | None
    generation: int


@dataclass(frozen=True, slots=True)
class VersionRow:
    """One immutable published version, with the content it was published as."""

    version_id: str
    model_id: str
    label: str
    sequence: int
    content_digest: str
    content_json: str
    parent_version_ids: tuple[str, ...]
    elements: tuple[SemanticElement, ...]


@dataclass(frozen=True, slots=True)
class ChangeSetRow:
    """One immutable proposal, with its operations in the order they were stored."""

    change_set_id: str
    model_id: str
    base_version_id: str | None
    change_set_digest: str
    operations: tuple[ChangeOperation, ...]


@dataclass(frozen=True, slots=True)
class ReviewRow:
    """A change set's review request and, once it has one, its single decision.

    `approval_id` is set only for an approved decision that was bound to the
    change set's digest, so "approved" and "has a usable approval" are the same
    question asked once.
    """

    review_request_id: str
    change_set_id: str
    review_decision_id: str | None
    reviewer_id: str | None
    decision: str | None
    approval_id: str | None
    approved_digest: str | None


@dataclass(frozen=True, slots=True)
class ConsumerRow:
    """One consumer's declared dependency on a model: its range and its binding."""

    consumer_id: str
    model_id: str
    min_sequence: int
    max_sequence: int | None
    bound_version_id: str | None


@dataclass(frozen=True, slots=True)
class PublicationRow:
    """One publication, addressed by the idempotency key that produced it."""

    publication_id: str
    idempotency_key: str
    request_digest: str
    model_id: str
    base_version_id: str | None
    result_version_id: str
    expected_pointer_generation: int
    resulting_pointer_generation: int
    approval_id: str
    validation_digest: str


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """One queued fact about an aggregate, at its contiguous sequence."""

    outbox_id: str
    aggregate_id: str
    sequence: int
    event_kind: str
    payload: Mapping[str, Any]
    payload_digest: str


# --- reads ---------------------------------------------------------------------


def read_model(
    connection: sqlite3.Connection, *, workspace_id: str, model_id: str
) -> ModelRow | None:
    row = connection.execute(
        "SELECT model_id, model_kind, created_at_us FROM omnivia_semantic_models "
        "WHERE workspace_id = ? AND model_id = ?",
        (workspace_id, model_id),
    ).fetchone()
    return None if row is None else ModelRow(str(row[0]), str(row[1]), int(row[2]))


def read_pointer(
    connection: sqlite3.Connection, *, workspace_id: str, model_id: str
) -> PointerRow | None:
    row = connection.execute(
        "SELECT model_id, current_version_id, generation "
        "FROM omnivia_semantic_current_pointers "
        "WHERE workspace_id = ? AND model_id = ?",
        (workspace_id, model_id),
    ).fetchone()
    if row is None:
        return None
    return PointerRow(
        model_id=str(row[0]),
        current_version_id=None if row[1] is None else str(row[1]),
        generation=int(row[2]),
    )


def _version_row(
    connection: sqlite3.Connection, workspace_id: str, row: Sequence[Any]
) -> VersionRow:
    model_id, version_id = str(row[1]), str(row[0])
    parents = tuple(
        str(parent[0])
        for parent in connection.execute(
            "SELECT parent_version_id FROM omnivia_semantic_version_parents "
            "WHERE workspace_id = ? AND model_id = ? AND version_id = ? "
            "ORDER BY parent_version_id",
            (workspace_id, model_id, version_id),
        )
    )
    elements = tuple(
        _element_from_json(str(element[0]))
        for element in connection.execute(
            "SELECT element_json FROM omnivia_semantic_version_elements "
            "WHERE workspace_id = ? AND model_id = ? AND version_id = ? "
            "ORDER BY element_id",
            (workspace_id, model_id, version_id),
        )
    )
    return VersionRow(
        version_id=version_id,
        model_id=model_id,
        label=str(row[2]),
        sequence=int(row[3]),
        content_digest=str(row[4]),
        content_json=str(row[5]),
        parent_version_ids=parents,
        elements=elements,
    )


_VERSION_COLUMNS = (
    "version_id, model_id, label, sequence, content_digest, content_json"
)


def read_version(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    model_id: str,
    version_id: str,
) -> VersionRow | None:
    row = connection.execute(
        f"SELECT {_VERSION_COLUMNS} FROM omnivia_semantic_model_versions "
        "WHERE workspace_id = ? AND model_id = ? AND version_id = ?",
        (workspace_id, model_id, version_id),
    ).fetchone()
    return None if row is None else _version_row(connection, workspace_id, row)


def read_versions(
    connection: sqlite3.Connection, *, workspace_id: str, model_id: str
) -> tuple[VersionRow, ...]:
    rows = connection.execute(
        f"SELECT {_VERSION_COLUMNS} FROM omnivia_semantic_model_versions "
        "WHERE workspace_id = ? AND model_id = ? ORDER BY sequence",
        (workspace_id, model_id),
    ).fetchall()
    return tuple(_version_row(connection, workspace_id, row) for row in rows)


def next_sequence(
    connection: sqlite3.Connection, *, workspace_id: str, model_id: str
) -> int:
    """The one `sequence` 0026's contiguity guard will accept for this model."""
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_semantic_model_versions "
        "WHERE workspace_id = ? AND model_id = ?",
        (workspace_id, model_id),
    ).fetchone()
    return int(row[0])


def _change_set_row(
    connection: sqlite3.Connection, workspace_id: str, row: Sequence[Any]
) -> ChangeSetRow:
    change_set_id = str(row[0])
    operations = tuple(
        _operation_from_json(str(operation[0]))
        for operation in connection.execute(
            "SELECT operation_json FROM omnivia_semantic_change_operations "
            "WHERE workspace_id = ? AND change_set_id = ? ORDER BY ordinal",
            (workspace_id, change_set_id),
        )
    )
    return ChangeSetRow(
        change_set_id=change_set_id,
        model_id=str(row[1]),
        base_version_id=None if row[2] is None else str(row[2]),
        change_set_digest=str(row[3]),
        operations=operations,
    )


_CHANGE_SET_COLUMNS = "change_set_id, model_id, base_version_id, change_set_digest"


def read_change_set(
    connection: sqlite3.Connection, *, workspace_id: str, change_set_id: str
) -> ChangeSetRow | None:
    row = connection.execute(
        f"SELECT {_CHANGE_SET_COLUMNS} FROM omnivia_semantic_change_sets "
        "WHERE workspace_id = ? AND change_set_id = ?",
        (workspace_id, change_set_id),
    ).fetchone()
    return None if row is None else _change_set_row(connection, workspace_id, row)


def find_change_set(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    model_id: str,
    change_set_digest: str,
) -> ChangeSetRow | None:
    """The proposal this exact semantic content already produced, if any.

    The digest covers the base and the ordered operations, so an equivalent
    retry resolves here instead of becoming a second proposal -- including a
    retry after the first one was rejected, which is why nothing about the
    decision narrows this lookup.
    """
    row = connection.execute(
        f"SELECT {_CHANGE_SET_COLUMNS} FROM omnivia_semantic_change_sets "
        "WHERE workspace_id = ? AND model_id = ? AND change_set_digest = ? "
        "ORDER BY created_at_us, change_set_id LIMIT 1",
        (workspace_id, model_id, change_set_digest),
    ).fetchone()
    return None if row is None else _change_set_row(connection, workspace_id, row)


def read_review(
    connection: sqlite3.Connection, *, workspace_id: str, change_set_id: str
) -> ReviewRow | None:
    row = connection.execute(
        "SELECT r.review_request_id, r.change_set_id, d.review_decision_id, "
        "d.reviewer_id, d.decision, a.approval_id, a.change_set_digest "
        "FROM omnivia_semantic_review_requests r "
        "LEFT JOIN omnivia_semantic_review_decisions d "
        "  ON d.workspace_id = r.workspace_id "
        " AND d.review_request_id = r.review_request_id "
        "LEFT JOIN omnivia_semantic_approval_records a "
        "  ON a.workspace_id = r.workspace_id AND a.change_set_id = r.change_set_id "
        "WHERE r.workspace_id = ? AND r.change_set_id = ? "
        "ORDER BY r.requested_at_us, r.review_request_id LIMIT 1",
        (workspace_id, change_set_id),
    ).fetchone()
    if row is None:
        return None
    return ReviewRow(
        review_request_id=str(row[0]),
        change_set_id=str(row[1]),
        review_decision_id=None if row[2] is None else str(row[2]),
        reviewer_id=None if row[3] is None else str(row[3]),
        decision=None if row[4] is None else str(row[4]),
        approval_id=None if row[5] is None else str(row[5]),
        approved_digest=None if row[6] is None else str(row[6]),
    )


def read_consumers(
    connection: sqlite3.Connection, *, workspace_id: str, model_id: str
) -> tuple[ConsumerRow, ...]:
    """Every consumer that has declared a dependency on this model.

    A consumer's exact-version binding is append-only (spec 15.3): binding a
    later version does not erase the earlier row, so `bound_version_id` is
    resolved as its own scalar subquery -- the latest by `bound_at_us`, tied
    on `version_id` for a deterministic pick -- rather than joined, which
    would fan a consumer out into one row per binding it has ever had.
    """
    rows = connection.execute(
        "SELECT d.consumer_id, d.model_id, s.min_sequence, s.max_sequence, "
        "(SELECT b.version_id FROM omnivia_semantic_consumer_version_bindings b "
        " WHERE b.workspace_id = d.workspace_id AND b.consumer_id = d.consumer_id "
        "   AND b.model_id = d.model_id "
        " ORDER BY b.bound_at_us DESC, b.version_id DESC LIMIT 1) AS bound_version_id "
        "FROM omnivia_semantic_consumer_dependencies d "
        "LEFT JOIN omnivia_semantic_consumer_supported_ranges s "
        "  ON s.workspace_id = d.workspace_id AND s.consumer_id = d.consumer_id "
        " AND s.model_id = d.model_id "
        "WHERE d.workspace_id = ? AND d.model_id = ? ORDER BY d.consumer_id",
        (workspace_id, model_id),
    ).fetchall()
    return tuple(
        ConsumerRow(
            consumer_id=str(row[0]),
            model_id=str(row[1]),
            min_sequence=0 if row[2] is None else int(row[2]),
            max_sequence=None if row[3] is None else int(row[3]),
            bound_version_id=None if row[4] is None else str(row[4]),
        )
        for row in rows
    )


_PUBLICATION_COLUMNS = (
    "publication_id, idempotency_key, request_digest, model_id, base_version_id, "
    "result_version_id, expected_pointer_generation, resulting_pointer_generation, "
    "approval_id, validation_digest"
)


def _publication_row(row: Sequence[Any]) -> PublicationRow:
    return PublicationRow(
        publication_id=str(row[0]),
        idempotency_key=str(row[1]),
        request_digest=str(row[2]),
        model_id=str(row[3]),
        base_version_id=None if row[4] is None else str(row[4]),
        result_version_id=str(row[5]),
        expected_pointer_generation=int(row[6]),
        resulting_pointer_generation=int(row[7]),
        approval_id=str(row[8]),
        validation_digest=str(row[9]),
    )


def read_publication(
    connection: sqlite3.Connection, *, workspace_id: str, idempotency_key: str
) -> PublicationRow | None:
    row = connection.execute(
        f"SELECT {_PUBLICATION_COLUMNS} FROM omnivia_semantic_publication_records "
        "WHERE workspace_id = ? AND idempotency_key = ?",
        (workspace_id, idempotency_key),
    ).fetchone()
    return None if row is None else _publication_row(row)


def read_outbox(
    connection: sqlite3.Connection, *, workspace_id: str, aggregate_id: str
) -> tuple[OutboxRow, ...]:
    rows = connection.execute(
        "SELECT outbox_id, aggregate_id, sequence, event_kind, payload_json, "
        "payload_digest FROM omnivia_semantic_outbox "
        "WHERE workspace_id = ? AND aggregate_id = ? ORDER BY sequence",
        (workspace_id, aggregate_id),
    ).fetchall()
    return tuple(
        OutboxRow(
            outbox_id=str(row[0]),
            aggregate_id=str(row[1]),
            sequence=int(row[2]),
            event_kind=str(row[3]),
            payload=json.loads(str(row[4])),
            payload_digest=str(row[5]),
        )
        for row in rows
    )


def project_model(
    connection: sqlite3.Connection, *, workspace_id: str, model_id: str
) -> dict[str, Any]:
    """A neutral JSON view of one model, rebuilt only from authoritative tables.

    Nothing here is a cache and nothing on a filesystem is authoritative: this
    reads the same rows every other reader reads, in a fixed order, so two
    projections of the same committed state are byte-identical.
    """
    model = read_model(connection, workspace_id=workspace_id, model_id=model_id)
    if model is None:
        raise StorageError(f"no semantic model {model_id!r} in this workspace")
    pointer = read_pointer(connection, workspace_id=workspace_id, model_id=model_id)
    return {
        "model": {
            "model_id": model.model_id,
            "model_kind": model.model_kind,
        },
        "current": {
            "version_id": None if pointer is None else pointer.current_version_id,
            "generation": 0 if pointer is None else pointer.generation,
        },
        "versions": [
            {
                "version_id": version.version_id,
                "label": version.label,
                "sequence": version.sequence,
                "content_digest": version.content_digest,
                "parent_version_ids": list(version.parent_version_ids),
                "content": json.loads(version.content_json),
            }
            for version in read_versions(
                connection, workspace_id=workspace_id, model_id=model_id
            )
        ],
        "consumers": [
            {
                "consumer_id": consumer.consumer_id,
                "min_sequence": consumer.min_sequence,
                "max_sequence": consumer.max_sequence,
                "bound_version_id": consumer.bound_version_id,
            }
            for consumer in read_consumers(
                connection, workspace_id=workspace_id, model_id=model_id
            )
        ],
    }


def verify_version_digests(
    connection: sqlite3.Connection, *, workspace_id: str, model_id: str
) -> tuple[str, ...]:
    """The versions whose stored digest no longer matches what is stored under it.

    Two independent recomputations, because they can fail apart: the stored
    snapshot document is re-digested, and the version is rebuilt from its own
    element rows and re-digested. A restore that dropped or altered an element
    row fails the second even when the first still agrees.
    """
    mismatched: list[str] = []
    for version in read_versions(
        connection, workspace_id=workspace_id, model_id=model_id
    ):
        expected = version.content_digest
        snapshot_digest = content_digest(json.loads(version.content_json))
        rebuilt_digest = model_version_digest(version_of(version))
        if snapshot_digest != expected or rebuilt_digest != expected:
            mismatched.append(version.version_id)
    return tuple(mismatched)


# --- writes --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SemanticRegistryWriter:
    """Every registry write, issued into a transaction that is already open.

    The workspace is bound at construction rather than passed per call: a
    publication is one workspace's work, and threading the identifier through
    each statement would let one composition mix two.
    """

    connection: sqlite3.Connection
    workspace_id: str

    def _next(self, statement: str, parameters: tuple[Any, ...]) -> int:
        """The next contiguous counter, read inside the writing transaction.

        0026 checks contiguity in a trigger, so a number chosen before the
        transaction opened is a number another writer may already have used.
        The one exception is a version's own `sequence`, which its content
        digest is computed over and therefore cannot be discovered afterwards.
        """
        row = self.connection.execute(statement, parameters).fetchone()
        return int(row[0])

    def create_model(self, *, model_id: str, model_kind: str, now_us: int) -> None:
        """Register a model and open its pointer at generation zero.

        Both rows land together. A model whose pointer did not exist could
        never be published to, because 0026 creates no pointer on the way.
        """
        self.connection.execute(
            "INSERT INTO omnivia_semantic_models "
            "(workspace_id, model_id, model_kind, created_at_us, updated_at_us) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.workspace_id, model_id, model_kind, now_us, now_us),
        )
        self.connection.execute(
            "INSERT INTO omnivia_semantic_current_pointers "
            "(workspace_id, model_id, current_version_id, generation, updated_at_us) "
            "VALUES (?, ?, NULL, 0, ?)",
            (self.workspace_id, model_id, now_us),
        )

    def create_change_set(
        self,
        *,
        change_set_id: str,
        model_id: str,
        base_version_id: str | None,
        change_set_digest: str,
        operations: Sequence[ChangeOperation],
        now_us: int,
    ) -> None:
        """Record one proposal and its operations at their canonical ordinals."""
        self.connection.execute(
            "INSERT INTO omnivia_semantic_change_sets "
            "(workspace_id, change_set_id, model_id, base_version_id, "
            "change_set_digest, created_at_us) VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                change_set_id,
                model_id,
                base_version_id,
                change_set_digest,
                now_us,
            ),
        )
        for ordinal, operation in enumerate(operations):
            self.connection.execute(
                "INSERT INTO omnivia_semantic_change_operations "
                "(workspace_id, change_set_id, ordinal, operation_json, "
                "operation_digest) VALUES (?, ?, ?, ?, ?)",
                (
                    self.workspace_id,
                    change_set_id,
                    ordinal,
                    canonical_text(operation),
                    content_digest(operation_payload(operation)),
                ),
            )

    def open_review(
        self, *, review_request_id: str, change_set_id: str, now_us: int
    ) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_semantic_review_requests "
            "(workspace_id, review_request_id, change_set_id, requested_at_us) "
            "VALUES (?, ?, ?, ?)",
            (self.workspace_id, review_request_id, change_set_id, now_us),
        )

    def record_decision(
        self,
        *,
        review_decision_id: str,
        review_request_id: str,
        reviewer_id: str,
        decision: str,
        now_us: int,
    ) -> None:
        """Append the one decision a review request receives."""
        self.connection.execute(
            "INSERT INTO omnivia_semantic_review_decisions "
            "(workspace_id, review_decision_id, review_request_id, reviewer_id, "
            "decision, decided_at_us) VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                review_decision_id,
                review_request_id,
                reviewer_id,
                decision,
                now_us,
            ),
        )

    def record_approval(
        self,
        *,
        approval_id: str,
        change_set_id: str,
        change_set_digest: str,
        review_decision_id: str,
        now_us: int,
    ) -> None:
        """Bind an approval to the exact digest its change set was stored with.

        The digest passed here is checked by 0026 against the change set's own
        row, so an approval can never come to name content its change set does
        not hold -- and a changed proposal is a different change set with no
        approval of its own.
        """
        self.connection.execute(
            "INSERT INTO omnivia_semantic_approval_records "
            "(workspace_id, approval_id, change_set_id, change_set_digest, "
            "review_decision_id, approved_at_us) VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                approval_id,
                change_set_id,
                change_set_digest,
                review_decision_id,
                now_us,
            ),
        )

    def register_consumer(self, *, consumer_id: str, now_us: int) -> None:
        self.connection.execute(
            "INSERT INTO omnivia_semantic_consumers "
            "(workspace_id, consumer_id, created_at_us, updated_at_us) "
            "VALUES (?, ?, ?, ?)",
            (self.workspace_id, consumer_id, now_us, now_us),
        )

    def declare_dependency(
        self,
        *,
        consumer_id: str,
        model_id: str,
        min_sequence: int,
        max_sequence: int | None,
        now_us: int,
    ) -> None:
        """Declare that a consumer depends on a model, over a supported range."""
        self.connection.execute(
            "INSERT INTO omnivia_semantic_consumer_dependencies "
            "(workspace_id, consumer_id, model_id, declared_at_us) "
            "VALUES (?, ?, ?, ?)",
            (self.workspace_id, consumer_id, model_id, now_us),
        )
        self.connection.execute(
            "INSERT INTO omnivia_semantic_consumer_supported_ranges "
            "(workspace_id, consumer_id, model_id, min_sequence, max_sequence, "
            "declared_at_us) VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                consumer_id,
                model_id,
                min_sequence,
                max_sequence,
                now_us,
            ),
        )

    def bind_version(
        self, *, consumer_id: str, model_id: str, version_id: str, now_us: int
    ) -> None:
        """Record the exact version one consumer deployment runs against."""
        self.connection.execute(
            "INSERT INTO omnivia_semantic_consumer_version_bindings "
            "(workspace_id, consumer_id, model_id, version_id, bound_at_us) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.workspace_id, consumer_id, model_id, version_id, now_us),
        )

    def publish_version(
        self,
        *,
        version: ModelVersion,
        sequence: int,
        publication_id: str,
        idempotency_key: str,
        request_digest: str,
        base_version_id: str | None,
        expected_pointer_generation: int,
        approval_id: str,
        validation_digest: str,
        outbox_id: str,
        change_set_digest: str,
        now_us: int,
    ) -> None:
        """Write the immutable version, its content, its provenance and its fact.

        Six relations, one statement family each: the version row, one row per
        element, one parent edge per parent, the publication record that says
        under which approval and idempotency key it happened, and exactly one
        outbox fact. The pointer is not touched here -- that is an activation.

        `sequence` is the caller's because the version's content digest is
        computed over it; the caller must have read it from
        :func:`next_sequence` inside this same transaction.
        """
        self.connection.execute(
            "INSERT INTO omnivia_semantic_model_versions "
            "(workspace_id, model_id, version_id, label, sequence, content_digest, "
            "content_json, created_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                version.model_id,
                version.model_version_id,
                version.version_label,
                sequence,
                version.content_digest,
                canonical_text(model_version_payload(version)),
                now_us,
            ),
        )
        for element in version.elements:
            document = _element_document(element)
            self.connection.execute(
                "INSERT INTO omnivia_semantic_version_elements "
                "(workspace_id, model_id, version_id, element_id, element_json, "
                "element_digest) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.workspace_id,
                    version.model_id,
                    version.model_version_id,
                    element.element_id,
                    canonical_text(document),
                    content_digest(document),
                ),
            )
        for parent_version_id in version.parent_version_ids:
            self.connection.execute(
                "INSERT INTO omnivia_semantic_version_parents "
                "(workspace_id, model_id, version_id, parent_version_id) "
                "VALUES (?, ?, ?, ?)",
                (
                    self.workspace_id,
                    version.model_id,
                    version.model_version_id,
                    parent_version_id,
                ),
            )
        self.connection.execute(
            "INSERT INTO omnivia_semantic_publication_records "
            "(workspace_id, publication_id, idempotency_key, request_digest, "
            "model_id, base_version_id, result_version_id, "
            "expected_pointer_generation, resulting_pointer_generation, approval_id, "
            "validation_digest, published_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                publication_id,
                idempotency_key,
                request_digest,
                version.model_id,
                base_version_id,
                version.model_version_id,
                expected_pointer_generation,
                expected_pointer_generation + 1,
                approval_id,
                validation_digest,
                now_us,
            ),
        )
        self.append_outbox(
            outbox_id=outbox_id,
            aggregate_id=version.model_id,
            event_kind=PUBLICATION_EVENT_KIND,
            payload={
                "model_id": version.model_id,
                "version_id": version.model_version_id,
                "label": version.version_label,
                "sequence": sequence,
                "content_digest": version.content_digest,
                "change_set_digest": change_set_digest,
                "publication_id": publication_id,
            },
            now_us=now_us,
        )

    def append_outbox(
        self,
        *,
        outbox_id: str,
        aggregate_id: str,
        event_kind: str,
        payload: Mapping[str, Any],
        now_us: int,
    ) -> None:
        sequence = self._next(
            "SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_semantic_outbox "
            "WHERE workspace_id = ? AND aggregate_id = ?",
            (self.workspace_id, aggregate_id),
        )
        self.connection.execute(
            "INSERT INTO omnivia_semantic_outbox "
            "(workspace_id, aggregate_id, sequence, outbox_id, event_kind, "
            "payload_json, payload_digest, created_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                aggregate_id,
                sequence,
                outbox_id,
                event_kind,
                canonical_text(payload),
                content_digest(payload),
                now_us,
            ),
        )

    def activate_version(
        self,
        *,
        model_id: str,
        version_id: str,
        previous_version_id: str | None,
        audit_ref: str,
        actor_id: str,
        now_us: int,
    ) -> int:
        """Record why the pointer moves, then move it by the only path there is.

        The audit event precedes the activation so the activation's foreign key
        has something to resolve, exactly as the mutation seam orders its own
        writes. Inserting the activation row is what advances the pointer: 0026's
        AFTER INSERT trigger does it, and no statement here updates the pointer
        directly. Returns the generation the pointer was moved to.
        """
        activation_sequence = self._next(
            "SELECT COALESCE(MAX(activation_sequence), -1) + 1 "
            "FROM omnivia_semantic_version_activations "
            "WHERE workspace_id = ? AND model_id = ?",
            (self.workspace_id, model_id),
        )
        self.connection.execute(
            "INSERT INTO omnivia_application_audit_events "
            "(audit_ref, workspace_id, principal_id, operation, purpose, request_id, "
            "correlation_id, trace_id, granted_authority_json, outcome_class, "
            "error_code, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', NULL, ?)",
            (
                audit_ref,
                self.workspace_id,
                actor_id,
                _ACTIVATION_OPERATION,
                _ACTIVATION_PURPOSE,
                audit_ref,
                audit_ref,
                audit_ref,
                _ACTIVATION_AUTHORITY_JSON,
                now_us,
            ),
        )
        self.connection.execute(
            "INSERT INTO omnivia_semantic_version_activations "
            "(workspace_id, model_id, activation_sequence, version_id, "
            "previous_version_id, generation, activated_at_us, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                model_id,
                activation_sequence,
                version_id,
                previous_version_id,
                activation_sequence + 1,
                now_us,
                audit_ref,
            ),
        )
        return activation_sequence + 1


@contextmanager
def semantic_registry_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[SemanticRegistryWriter]:
    """One fenced transaction, and the registry writes issued into it.

    Authority is validated on entry and again immediately before commit, so a
    publication that lost the generation between the two commits nothing.
    """
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield SemanticRegistryWriter(
            connection=connection, workspace_id=workspace_id
        )


__all__ = [
    "ELEMENT_TYPES",
    "PUBLICATION_EVENT_KIND",
    "ChangeSetRow",
    "ConsumerRow",
    "ModelRow",
    "OutboxRow",
    "PointerRow",
    "PublicationRow",
    "ReviewRow",
    "SemanticRegistryWriter",
    "VersionRow",
    "canonical_text",
    "coerce_element_fields",
    "find_change_set",
    "next_sequence",
    "project_model",
    "read_change_set",
    "read_consumers",
    "read_model",
    "read_outbox",
    "read_pointer",
    "read_publication",
    "read_review",
    "read_version",
    "read_versions",
    "semantic_registry_writer",
    "verify_version_digests",
    "version_of",
]
