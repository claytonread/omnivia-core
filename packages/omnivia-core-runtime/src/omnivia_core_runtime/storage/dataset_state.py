"""Structured-data DatasetState observations (SPEC-CORE-DATA-001 WP07; migration 0062).

Persistence only, in the shape of `storage/connectors.py`: every function takes an
open connection, and `record_observation` expects its caller to already be inside the
`fenced_transaction` that wrote the observation's application audit event -- the
mutation executor's own order (`service/mutation.py`). This module owns no
connection, lease, clock or authority lookup. The settlement context supplies the
audit reference and the instant, and migration 0062's INSERT guard binds the row to
exactly that successful audit.

An observation keeps the DatasetState dimensions independent, and nothing here
derives one from another. `empty` content is not `complete` coverage, and a healthy
source with available evidence is not thereby fresh: freshness is recorded as
evidence -- scope digest, source observation, optional deadline, verification and
recording instants -- and no reader turns a clock value into a currentness verdict.
`observed_authority_epoch` is evidence for a later evaluation, never a grant.

A dataset's current state is its highest `state_generation`, read through the
`omnivia_analysis_dataset_state_current` view, so it is the log's own replay and has
nothing to rebuild.

Evidence is a closed shape. `coverage` and `source_observation` each carry exactly
the fields their shape names (`_COVERAGE_SHAPE`, `_SOURCE_SHAPE`), and every field is
an identifier, a digest, a bounded integer, an instant, a word from its vocabulary or
a list of unique identifiers. The writer checks that shape and the cross-bindings
before it canonicalises. The reader checks the storage bound and the digest before it
decodes, then the same shape, so a stored row that does not verify is refused the way a
written one is. The checks below refuse early, naming fields but never values; the
schema's CHECKs and triggers stay the final boundary for every writer.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, TypeAlias

from omnivia_core.contracts.v1 import is_identifier, to_canonical_json
from omnivia_core_runtime.service.mutation import MutationSettlementContext

INITIAL_READINESS: Final = frozenset(
    {"not_started", "initialising", "catching_up", "ready", "blocked"}
)
COMPLETENESS: Final = frozenset({"complete", "partial", "unknown"})
CONTINUITY: Final = frozenset({"verified", "gap_detected", "unknown", "not_applicable"})
OPERATIONAL_HEALTH: Final = frozenset(
    {"healthy", "degraded", "unavailable", "error", "unknown"}
)
SCHEMA_COMPATIBILITY: Final = frozenset(
    {"compatible", "requires_review", "incompatible", "unknown"}
)
EVIDENCE_AVAILABILITY: Final = frozenset({"available", "limited", "unavailable"})
CONTENT_OBSERVATION: Final = frozenset({"empty", "nonempty", "unknown"})
PROOF_KINDS: Final = frozenset(
    {"complete_enumeration", "consistent_snapshot", "contiguous_log", "bounded_observation", "none"}
)
EVIDENCE_KINDS: Final = frozenset(
    {"snapshot", "stream_caught_up", "cursor_poll", "complete_reconcile", "captured_query", "none"}
)

#: The schema's byte bound on each canonical evidence document.
EVIDENCE_MAX_BYTES: Final = 8192
#: The most identifiers one evidence list holds.
EVIDENCE_REFS_MAX: Final = 64

_OBSERVATIONS: Final = "omnivia_analysis_dataset_state_observations"
_CURRENT: Final = "omnivia_analysis_dataset_state_current"
_INT64_MAX: Final = 2**63 - 1

#: The columns an observation states as given, by field name.
_OBSERVATION_COLUMNS: Final = (
    "dataset_id",
    "dataset_revision",
    "dataset_incarnation",
    "manifest_id",
    "manifest_revision",
    "manifest_digest",
    "initial_readiness",
    "completeness",
    "continuity",
    "operational_health",
    "schema_compatibility",
    "content_observation",
    "evidence_availability",
    "observed_authority_epoch",
    "scope_digest",
    "freshness_deadline_at_us",
    "verified_at_us",
)
_COLUMNS: Final = (
    "workspace_id",
    "state_generation",
    *_OBSERVATION_COLUMNS,
    "coverage_json",
    "coverage_digest",
    "source_observation_json",
    "source_observation_digest",
    "recorded_at_us",
    "audit_ref",
)
_SELECT: Final = ", ".join(_COLUMNS)
_INSERT: Final = (
    f"INSERT INTO {_OBSERVATIONS} ({_SELECT}) "
    f"VALUES ({', '.join(':' + column for column in _COLUMNS)})"
)


class DatasetStateInvalid(ValueError):
    """An observation, or a stored row read back, is not a valid DatasetState."""


@dataclass(frozen=True, slots=True)
class DatasetStateObservation:
    """One dataset's state as a producer observed it, every dimension independent.

    `coverage` and `source_observation` are closed evidence documents: each holds
    exactly the fields its shape names, and every field is an identifier, a digest, a
    bounded integer, an instant, a word from its vocabulary or a list of unique
    identifiers. An identifier is an opaque reference. This module checks its grammar,
    not what it refers to.
    """

    dataset_id: str
    dataset_revision: str
    dataset_incarnation: str
    initial_readiness: str
    completeness: str
    continuity: str
    operational_health: str
    schema_compatibility: str
    content_observation: str
    evidence_availability: str
    observed_authority_epoch: str
    scope_digest: str
    coverage: Mapping[str, Any]
    source_observation: Mapping[str, Any]
    verified_at_us: int
    freshness_deadline_at_us: int | None = None
    manifest_id: str | None = None
    manifest_revision: str | None = None
    manifest_digest: str | None = None


@dataclass(frozen=True, slots=True)
class DatasetStateRecord:
    """One stored observation, read back with both evidence digests verified."""

    workspace_id: str
    state_generation: int
    observation: DatasetStateObservation
    coverage_digest: str
    source_observation_digest: str
    recorded_at_us: int
    audit_ref: str


def record_observation(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    observation: DatasetStateObservation,
) -> int:
    """Append `observation` as its dataset's next state generation and return it.

    The generation is minted here, inside the caller's fenced transaction, and the
    INSERT guard refuses anything but the next one. `recorded_at_us` is the
    settlement instant, which the guard requires to be the instant of the
    settlement's own successful audit event.
    """
    _validate(observation)
    coverage_json = _evidence_json(observation.coverage)
    source_json = _evidence_json(observation.source_observation)
    row = connection.execute(
        f"SELECT COALESCE(MAX(state_generation), 0) + 1 FROM {_OBSERVATIONS} "
        "WHERE workspace_id = ? AND dataset_id = ?",
        (workspace_id, observation.dataset_id),
    ).fetchone()
    generation = int(row[0])
    values: dict[str, object] = {
        column: getattr(observation, column) for column in _OBSERVATION_COLUMNS
    }
    values.update(
        workspace_id=workspace_id,
        state_generation=generation,
        coverage_json=coverage_json,
        coverage_digest=_digest(coverage_json),
        source_observation_json=source_json,
        source_observation_digest=_digest(source_json),
        recorded_at_us=settlement.settled_at_us,
        audit_ref=settlement.audit_ref,
    )
    connection.execute(_INSERT, values)
    return generation


def read_state_history(
    connection: sqlite3.Connection, *, workspace_id: str, dataset_id: str
) -> tuple[DatasetStateRecord, ...]:
    """Every observation of one dataset, lowest generation first."""
    rows = connection.execute(
        f"SELECT {_SELECT} FROM {_OBSERVATIONS} "
        "WHERE workspace_id = ? AND dataset_id = ? ORDER BY state_generation",
        (workspace_id, dataset_id),
    ).fetchall()
    return tuple(_record(row) for row in rows)


def read_current_state(
    connection: sqlite3.Connection, *, workspace_id: str, dataset_id: str
) -> DatasetStateRecord | None:
    """The dataset's observation at its highest generation, or `None` if unobserved."""
    row = connection.execute(
        f"SELECT {_SELECT} FROM {_CURRENT} WHERE workspace_id = ? AND dataset_id = ?",
        (workspace_id, dataset_id),
    ).fetchone()
    return None if row is None else _record(row)


_Check: TypeAlias = Callable[[object], bool]


def _validate(observation: DatasetStateObservation) -> None:
    manifest = (
        observation.manifest_id,
        observation.manifest_revision,
        observation.manifest_digest,
    )
    deadline = observation.freshness_deadline_at_us
    checks = {
        "dataset_id": is_identifier(observation.dataset_id),
        "dataset_revision": is_identifier(observation.dataset_revision),
        "dataset_incarnation": is_identifier(observation.dataset_incarnation),
        "manifest": manifest == (None, None, None)
        or (
            is_identifier(manifest[0])
            and is_identifier(manifest[1])
            and _is_digest(manifest[2])
        ),
        "initial_readiness": _member(observation.initial_readiness, INITIAL_READINESS),
        "completeness": _member(observation.completeness, COMPLETENESS),
        "continuity": _member(observation.continuity, CONTINUITY),
        "operational_health": _member(observation.operational_health, OPERATIONAL_HEALTH),
        "schema_compatibility": _member(
            observation.schema_compatibility, SCHEMA_COMPATIBILITY
        ),
        "content_observation": _member(observation.content_observation, CONTENT_OBSERVATION),
        "evidence_availability": _member(
            observation.evidence_availability, EVIDENCE_AVAILABILITY
        ),
        "observed_authority_epoch": is_identifier(observation.observed_authority_epoch),
        "scope_digest": _is_digest(observation.scope_digest),
        "verified_at_us": _instant(observation.verified_at_us),
        "freshness_deadline_at_us": deadline is None or _instant(deadline),
    }
    invalid = [field for field, valid in checks.items() if not valid]
    shape = _shape_findings(
        "coverage", observation.coverage, _COVERAGE_SHAPE
    ) + _shape_findings("source_observation", observation.source_observation, _SOURCE_SHAPE)
    invalid += shape
    if not shape:
        invalid += _bindings(observation)
    if invalid:
        raise DatasetStateInvalid(f"invalid dataset state fields: {', '.join(invalid)}")


def _member(value: object, vocabulary: frozenset[str]) -> bool:
    return isinstance(value, str) and value in vocabulary


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _integer(value: object, *, least: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and least <= value <= _INT64_MAX


def _count(value: object) -> bool:
    return _integer(value, least=0)


def _instant(value: object) -> bool:
    return _integer(value, least=1)


def _nullable(check: _Check) -> _Check:
    return lambda value: value is None or check(value)


def _one_of(vocabulary: frozenset[str]) -> _Check:
    return lambda value: isinstance(value, str) and value in vocabulary


def _identifiers(value: object) -> bool:
    """A list of at most `EVIDENCE_REFS_MAX` distinct identifiers."""
    if not isinstance(value, (list, tuple)) or len(value) > EVIDENCE_REFS_MAX:
        return False
    return all(is_identifier(item) for item in value) and len(set(value)) == len(value)


def _closed(shape: Mapping[str, _Check]) -> _Check:
    """An object holding exactly the fields `shape` names, each passing its check."""

    def check(value: object) -> bool:
        return (
            isinstance(value, Mapping)
            and set(value) == set(shape)
            and all(test(value[name]) for name, test in shape.items())
        )

    return check


_INTERVAL: Final = _closed({"start_inclusive_at_us": _instant, "end_exclusive_at_us": _instant})


def _ordered_interval(value: object) -> bool:
    if not (isinstance(value, Mapping) and _INTERVAL(value)):
        return False
    return bool(value["start_inclusive_at_us"] < value["end_exclusive_at_us"])


#: The coverage document: exactly these fields, each checked by its own rule.
_COVERAGE_SHAPE: Final[Mapping[str, _Check]] = {
    "scope_digest": _is_digest,
    "accepted_rows": _count,
    "rejected_rows": _count,
    "conflicting_rows": _count,
    "deduplicated_rows": _count,
    "expected_source_rows": _nullable(_count),
    "proof_kind": _one_of(PROOF_KINDS),
    "proof_refs": _identifiers,
}

#: The source observation document: exactly these fields, each checked by its own rule.
_SOURCE_SHAPE: Final[Mapping[str, _Check]] = {
    "source_ref": _closed({"id": is_identifier, "revision_id": is_identifier}),
    "source_incarnation": _nullable(is_identifier),
    "observation_interval": _ordered_interval,
    "source_cutoff_at_us": _nullable(_instant),
    "verification_at_us": _instant,
    "evidence_kind": _one_of(EVIDENCE_KINDS),
    "snapshot_token_ref": _nullable(is_identifier),
    "applied_checkpoint_ref": _nullable(is_identifier),
    "scope_digest": _is_digest,
    "evidence_refs": _identifiers,
}


def _shape_findings(label: str, document: object, shape: Mapping[str, _Check]) -> list[str]:
    """The fields of `document` that break `shape`, named by path and never by value."""
    if not isinstance(document, Mapping):
        return [label]
    findings = [
        f"{label}.{name}"
        for name, test in shape.items()
        if name not in document or not test(document[name])
    ]
    if set(document) != set(shape):
        findings.append(f"{label}.keys")
    return findings


def _bindings(observation: DatasetStateObservation) -> list[str]:
    """Cross-document agreement, checked only once both documents hold their shape."""
    findings: list[str] = []
    if observation.coverage["scope_digest"] != observation.scope_digest:
        findings.append("coverage.scope_digest")
    if observation.source_observation["scope_digest"] != observation.scope_digest:
        findings.append("source_observation.scope_digest")
    if observation.source_observation["verification_at_us"] != observation.verified_at_us:
        findings.append("source_observation.verification_at_us")
    return findings


def _evidence_json(document: Mapping[str, Any]) -> str:
    """The canonical text of one validated evidence document, within its byte bound."""
    text = to_canonical_json(_plain(document))
    if len(text.encode("utf-8")) > EVIDENCE_MAX_BYTES:
        raise DatasetStateInvalid("dataset state evidence exceeds its byte bound")
    return text


def _plain(value: object) -> Any:
    """A plain JSON copy of a validated closed document."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _digest(document: str) -> str:
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


def _record(row: tuple[Any, ...]) -> DatasetStateRecord:
    values = dict(zip(_COLUMNS, row, strict=True))
    observation = DatasetStateObservation(
        **{column: values[column] for column in _OBSERVATION_COLUMNS},
        coverage=_stored_evidence(values["coverage_json"], values["coverage_digest"]),
        source_observation=_stored_evidence(
            values["source_observation_json"], values["source_observation_digest"]
        ),
    )
    _validate(observation)
    return DatasetStateRecord(
        workspace_id=values["workspace_id"],
        state_generation=values["state_generation"],
        observation=observation,
        coverage_digest=values["coverage_digest"],
        source_observation_digest=values["source_observation_digest"],
        recorded_at_us=values["recorded_at_us"],
        audit_ref=values["audit_ref"],
    )


def _stored_evidence(text: object, digest: str) -> dict[str, Any]:
    """Decode one stored evidence document, refusing bytes its digest does not name."""
    # The storage bound is read from the stored bytes before anything decodes them, the same
    # 2 to 8192 bytes the schema's CHECK admits, so a row written past it is refused on read.
    if not isinstance(text, str):
        raise DatasetStateInvalid("stored dataset state evidence is not text")
    if not 2 <= len(text.encode("utf-8")) <= EVIDENCE_MAX_BYTES:
        raise DatasetStateInvalid("stored dataset state evidence is outside its byte bound")
    # A closed shape is shallow, so nesting this interpreter cannot decode is refused as
    # invalid rather than leaking its RecursionError.
    try:
        document = json.loads(text)
        canonical = to_canonical_json(document) if isinstance(document, dict) else None
    except (ValueError, RecursionError) as error:
        raise DatasetStateInvalid("stored dataset state evidence does not decode") from error
    if not isinstance(document, dict) or canonical != text or _digest(text) != digest:
        raise DatasetStateInvalid("stored dataset state evidence does not verify")
    return document


__all__ = [
    "COMPLETENESS",
    "CONTENT_OBSERVATION",
    "CONTINUITY",
    "EVIDENCE_AVAILABILITY",
    "EVIDENCE_KINDS",
    "EVIDENCE_MAX_BYTES",
    "EVIDENCE_REFS_MAX",
    "INITIAL_READINESS",
    "OPERATIONAL_HEALTH",
    "PROOF_KINDS",
    "SCHEMA_COMPATIBILITY",
    "DatasetStateInvalid",
    "DatasetStateObservation",
    "DatasetStateRecord",
    "read_current_state",
    "read_state_history",
    "record_observation",
]
