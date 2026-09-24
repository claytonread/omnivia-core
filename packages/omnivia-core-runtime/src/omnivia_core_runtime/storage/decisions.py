"""Durable Local Decisions records (ADR-042, plan PR-3; spec §14, §24).

Every write here runs inside the fenced mutation transaction the service's
mutation coordinator opens, so this module holds no connection, no lease and no
clock of its own: the fenced connection and the settlement context arrive as
arguments, and the guard triggers migrations 0044-0046 carry are what make an
unguarded write impossible. Reads run inside a caller-opened read transaction.

The record families mirror §14.1 exactly and the physical tables are migrations
0044 (settings), 0045 (definitions, qualifications) and 0046 (evaluations,
attempts, results, outcomes, subscriptions, outbox). This module is the only
writer: handlers never spell SQL against these tables themselves.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.storage.memory import IdentifierAllocator, random_identifier

_SETTINGS_TABLE: Final = "omnivia_decision_settings"
_DEFINITIONS_TABLE: Final = "omnivia_decision_definition_versions"
_EVALUATIONS_TABLE: Final = "omnivia_decision_evaluations"
_ATTEMPTS_TABLE: Final = "omnivia_decision_attempts"
_RESULTS_TABLE: Final = "omnivia_decision_results"
_OUTCOMES_TABLE: Final = "omnivia_decision_outcomes"
_OUTBOX_TABLE: Final = "omnivia_decision_outbox"

DEFAULT_PROCESSING: Final = "off"


def _plain(value: Any) -> Any:
    """Decode the contract's immutable containers into JSON-serialisable ones."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def canonical_document(value: Mapping[str, Any] | list[Any]) -> str:
    """One stable JSON spelling, used for both digests and storage."""
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"))


def content_digest(document: str) -> str:
    """The `sha256:`-prefixed digest every decision integrity column carries."""
    import hashlib

    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def read_processing_state(
    connection: sqlite3.Connection, *, workspace_id: str
) -> str:
    """The workspace's processing state, or the specification default.

    An absent settings row is the §28.2 default ('off'), not an error: the
    capability has never been touched.
    """
    row = connection.execute(
        f"SELECT processing FROM {_SETTINGS_TABLE} WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    if row is None:
        return DEFAULT_PROCESSING
    return str(row[0])


def read_decision_settings_revision(
    connection: sqlite3.Connection, *, workspace_id: str
) -> int:
    """The settings CAS revision, or 0 before the row exists (§28.2 default)."""
    row = connection.execute(
        f"SELECT revision FROM {_SETTINGS_TABLE} WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    return 0 if row is None else int(row[0])


def write_decision_settings(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    processing: str,
    subscription_enabled: bool,
    expected_revision: int,
) -> int:
    """Create or compare-and-swap the settings row; returns the new revision.

    `expected_revision` is the caller's compare-and-swap counter: 0 creates the
    singleton row, and a mismatched counter on an existing row is a conflict the
    handler surfaces as such. The next revision is minted here, inside the
    fenced transaction, so two racing updates cannot claim the same one.
    """
    existing = read_decision_settings_revision(
        connection, workspace_id=workspace_id
    )
    if existing == 0 and expected_revision != 0:
        raise LookupError("decision-settings-missing")
    if existing != 0 and expected_revision != existing:
        raise LookupError("decision-settings-conflict")
    new_revision = existing + 1
    if existing == 0:
        connection.execute(
            f"INSERT INTO {_SETTINGS_TABLE} "
            "(workspace_id, singleton, processing, subscription_enabled, "
            "revision, updated_at_us, audit_ref) VALUES (?, 1, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                processing,
                1 if subscription_enabled else 0,
                new_revision,
                settlement.settled_at_us,
                settlement.audit_ref,
            ),
        )
    else:
        connection.execute(
            f"UPDATE {_SETTINGS_TABLE} SET processing = ?, "
            "subscription_enabled = ?, revision = ?, updated_at_us = ?, "
            "audit_ref = ? WHERE workspace_id = ?",
            (
                processing,
                1 if subscription_enabled else 0,
                new_revision,
                settlement.settled_at_us,
                settlement.audit_ref,
                workspace_id,
            ),
        )
    return new_revision


def read_decision_definition(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    definition_id: str,
    version: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT definition_json, definition_digest, enabled FROM "
        f"{_DEFINITIONS_TABLE} WHERE workspace_id = ? AND definition_id = ? "
        "AND version = ?",
        (workspace_id, definition_id, version),
    ).fetchone()
    if row is None:
        return None
    document = json.loads(str(row[0]))
    return {
        "definition": document,
        "digest": str(row[1]),
        "enabled": bool(row[2]),
    }


def list_decision_definitions(
    connection: sqlite3.Connection, *, workspace_id: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT definition_id, version, title, purpose, kind, options_json, "
        f"enabled, definition_digest FROM {_DEFINITIONS_TABLE} "
        "WHERE workspace_id = ? ORDER BY definition_id, version",
        (workspace_id,),
    ).fetchall()
    return [
        {
            "id": str(row[0]),
            "version": str(row[1]),
            "title": str(row[2]),
            "purpose": str(row[3]),
            "kind": str(row[4]),
            "option_count": len(json.loads(str(row[5]))),
            "enabled": bool(row[6]),
            "digest": str(row[7]),
        }
        for row in rows
    ]


def insert_decision_definition(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    definition_id: str,
    version: str,
    title: str,
    kind: str,
    purpose: str,
    options: list[Any],
    recipe: Mapping[str, Any],
    required_sources: int,
    min_source_count: int,
    definition: Mapping[str, Any],
    digest: str,
    published_by: str,
    allocate_identifier: IdentifierAllocator = random_identifier,
) -> None:
    document = canonical_document(dict(definition))
    connection.execute(
        f"INSERT INTO {_DEFINITIONS_TABLE} "
        "(workspace_id, definition_id, version, title, kind, purpose, "
        "options_json, recipe_json, required_sources, min_source_count, "
        "definition_digest, definition_json, enabled, published_by, "
        "published_at_us, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (
            workspace_id,
            definition_id,
            version,
            title,
            kind,
            purpose,
            canonical_document(options),
            canonical_document(dict(recipe)),
            required_sources,
            min_source_count,
            digest,
            document,
            published_by,
            settlement.settled_at_us,
            settlement.audit_ref,
        ),
    )


def set_decision_definition_enabled(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    definition_id: str,
    version: str,
    enabled: bool,
) -> bool:
    """Enable or disable one definition version; `False` means it was absent."""
    cursor = connection.execute(
        f"UPDATE {_DEFINITIONS_TABLE} SET enabled = ? "
        "WHERE workspace_id = ? AND definition_id = ? AND version = ?",
        (1 if enabled else 0, workspace_id, definition_id, version),
    )
    return cursor.rowcount == 1


def definition_digest_matches(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    definition_id: str,
    version: str,
    digest: str,
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {_DEFINITIONS_TABLE} WHERE workspace_id = ? "
        "AND definition_id = ? AND version = ? AND definition_digest = ? "
        "AND enabled = 1",
        (workspace_id, definition_id, version, digest),
    ).fetchone()
    return row is not None


def insert_decision_evaluation(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    evaluation_id: str,
    principal_id: str,
    idempotency_key: str,
    request_digest: str,
    definition_id: str,
    definition_version: str,
    definition_digest: str,
    mode: str,
    subject_refs: list[Mapping[str, Any]],
    source_snapshot: Mapping[str, Any],
    job_id: str,
) -> None:
    connection.execute(
        f"INSERT INTO {_EVALUATIONS_TABLE} "
        "(workspace_id, evaluation_id, principal_id, operation, "
        "idempotency_key, request_digest, definition_id, definition_version, "
        "definition_digest, status, mode, subject_refs_json, "
        "source_snapshot_json, created_at_us, job_id, audit_ref) "
        "VALUES (?, ?, ?, 'decision.evaluate', ?, ?, ?, ?, ?, 'running', ?, "
        "?, ?, ?, ?, ?)",
        (
            workspace_id,
            evaluation_id,
            principal_id,
            idempotency_key,
            request_digest,
            definition_id,
            definition_version,
            definition_digest,
            mode,
            canonical_document(subject_refs),
            canonical_document(dict(source_snapshot)),
            settlement.settled_at_us,
            job_id,
            settlement.audit_ref,
        ),
    )


def settle_decision_evaluation(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    evaluation_id: str,
    status: str,
    abstention_reasons: list[str] | None,
    terminal_at_us: int,
) -> None:
    """Settle one evaluation's lifecycle. A settled evaluation never re-settles."""
    connection.execute(
        f"UPDATE {_EVALUATIONS_TABLE} SET status = ?, terminal_at_us = ?, "
        "abstention_reasons_json = ? WHERE workspace_id = ? AND evaluation_id = ?",
        (
            status,
            terminal_at_us,
            (
                None
                if abstention_reasons is None
                else canonical_document(abstention_reasons)
            ),
            workspace_id,
            evaluation_id,
        ),
    )


def read_decision_evaluation(
    connection: sqlite3.Connection, *, workspace_id: str, evaluation_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT evaluation_id, principal_id, operation, idempotency_key, "
        f"request_digest, definition_id, definition_version, definition_digest, "
        f"status, mode, subject_refs_json, created_at_us, terminal_at_us, "
        f"abstention_reasons_json, job_id FROM {_EVALUATIONS_TABLE} "
        "WHERE workspace_id = ? AND evaluation_id = ?",
        (workspace_id, evaluation_id),
    ).fetchone()
    if row is None:
        return None
    return _evaluation_row(row)


def _evaluation_row(row: tuple[Any, ...]) -> dict[str, Any]:
    abstention = (
        None
        if row[13] is None
        else [str(reason) for reason in json.loads(str(row[13]))]
    )
    return {
        "evaluation_id": str(row[0]),
        "principal_id": str(row[1]),
        "operation": str(row[2]),
        "idempotency_key": str(row[3]),
        "request_digest": str(row[4]),
        "definition_ref": {
            "id": str(row[5]),
            "version": str(row[6]),
        },
        "definition_digest": str(row[7]),
        "status": str(row[8]),
        "mode": str(row[9]),
        "subject_refs": json.loads(str(row[10])),
        "created_at_us": int(row[11]),
        "terminal_at_us": None if row[12] is None else int(row[12]),
        "abstention_reasons": abstention,
        "job_id": str(row[14]),
    }


def list_decision_evaluations(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    definition_id: str | None,
    status: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    clauses = ["workspace_id = ?"]
    parameters: list[Any] = [workspace_id]
    if definition_id is not None:
        clauses.append("definition_id = ?")
        parameters.append(definition_id)
    if status is not None:
        clauses.append("status = ?")
        parameters.append(status)
    parameters.append(limit)
    rows = connection.execute(
        f"SELECT evaluation_id, principal_id, operation, idempotency_key, "
        f"request_digest, definition_id, definition_version, definition_digest, "
        f"status, mode, subject_refs_json, created_at_us, terminal_at_us, "
        f"abstention_reasons_json, job_id FROM {_EVALUATIONS_TABLE} "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at_us DESC, evaluation_id DESC LIMIT ?",
        parameters,
    ).fetchall()
    return [_evaluation_row(row) for row in rows]


def insert_decision_attempt(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    attempt_id: str,
    evaluation_id: str,
    attempt_number: int,
    route: str,
    provider_id: str,
    profile_id: str,
    policy_generation: str,
    status: str,
    failure_code: str | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> None:
    finished = None if status == "claimed" else settlement.settled_at_us
    connection.execute(
        f"INSERT INTO {_ATTEMPTS_TABLE} "
        "(workspace_id, attempt_id, evaluation_id, attempt_number, route, "
        "provider_id, profile_id, policy_generation, status, started_at_us, "
        "finished_at_us, duration_us, forward_passes, failure_code, "
        "diagnostics_json, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            attempt_id,
            evaluation_id,
            attempt_number,
            route,
            provider_id,
            profile_id,
            policy_generation,
            status,
            settlement.settled_at_us,
            finished,
            (
                None
                if finished is None
                else finished - settlement.settled_at_us
            ),
            1 if route == "deterministic" and status == "succeeded" else 0,
            failure_code,
            (
                None
                if diagnostics is None
                else canonical_document(dict(diagnostics))
            ),
            settlement.audit_ref,
        ),
    )


def insert_decision_result(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    result_id: str,
    evaluation_id: str,
    status: str,
    prediction: Mapping[str, Any] | None,
    disposition: Mapping[str, Any],
    quality: Mapping[str, Any],
    execution: Mapping[str, Any],
    abstention_reasons: list[str] | None,
    input_digest: str,
) -> None:
    connection.execute(
        f"INSERT INTO {_RESULTS_TABLE} "
        "(workspace_id, result_id, evaluation_id, schema_version, status, "
        "prediction_json, disposition_json, quality_json, execution_json, "
        "abstention_reasons_json, input_digest, created_at_us, audit_ref) "
        "VALUES (?, ?, ?, 'decision.1', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            result_id,
            evaluation_id,
            status,
            None if prediction is None else canonical_document(dict(prediction)),
            canonical_document(dict(disposition)),
            canonical_document(dict(quality)),
            canonical_document(dict(execution)),
            (
                None
                if abstention_reasons is None
                else canonical_document(abstention_reasons)
            ),
            input_digest,
            settlement.settled_at_us,
            settlement.audit_ref,
        ),
    )


def read_decision_result(
    connection: sqlite3.Connection, *, workspace_id: str, evaluation_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT status, prediction_json, disposition_json, quality_json, "
        f"execution_json, abstention_reasons_json, input_digest, created_at_us "
        f"FROM {_RESULTS_TABLE} WHERE workspace_id = ? AND evaluation_id = ?",
        (workspace_id, evaluation_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "status": str(row[0]),
        "prediction": (
            None if row[1] is None else json.loads(str(row[1]))
        ),
        "disposition": json.loads(str(row[2])),
        "quality": json.loads(str(row[3])),
        "execution": json.loads(str(row[4])),
        "abstention_reasons": (
            None
            if row[5] is None
            else [str(reason) for reason in json.loads(str(row[5]))]
        ),
        "input_digest": str(row[6]),
        "created_at_us": int(row[7]),
    }


def insert_decision_outcome(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    outcome_id: str,
    evaluation_id: str,
    outcome: str,
    corrected_option_id: str | None,
    note: str | None,
    evidence: Mapping[str, Any],
    actor_id: str,
    event_at_us: int,
    superseded_outcome_id: str | None,
) -> None:
    connection.execute(
        f"INSERT INTO {_OUTCOMES_TABLE} "
        "(workspace_id, outcome_id, evaluation_id, outcome, corrected_option_id, "
        "note, evidence_json, actor_id, event_at_us, recorded_at_us, "
        "superseded_outcome_id, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            outcome_id,
            evaluation_id,
            outcome,
            corrected_option_id,
            note,
            canonical_document(dict(evidence)),
            actor_id,
            event_at_us,
            settlement.settled_at_us,
            superseded_outcome_id,
            settlement.audit_ref,
        ),
    )


def append_decision_outbox_event(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    aggregate_id: str,
    outbox_id: str,
    event_kind: str,
    payload: Mapping[str, Any],
    created_at_us: int,
) -> None:
    payload_json = canonical_document(dict(payload))
    row = connection.execute(
        f"SELECT COALESCE(MAX(sequence), 0) + 1 FROM {_OUTBOX_TABLE} "
        "WHERE workspace_id = ? AND aggregate_id = ?",
        (workspace_id, aggregate_id),
    ).fetchone()
    assert row is not None
    connection.execute(
        f"INSERT INTO {_OUTBOX_TABLE} "
        "(workspace_id, aggregate_id, sequence, outbox_id, event_kind, "
        "payload_json, payload_digest, created_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            aggregate_id,
            int(row[0]),
            outbox_id,
            event_kind,
            payload_json,
            content_digest(payload_json),
            created_at_us,
        ),
    )


def read_decision_outbox_events(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    aggregate_id: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT sequence, outbox_id, event_kind, payload_json, created_at_us "
        f"FROM {_OUTBOX_TABLE} WHERE workspace_id = ? AND aggregate_id = ? "
        "ORDER BY sequence",
        (workspace_id, aggregate_id),
    ).fetchall()
    return [
        {
            "sequence": int(row[0]),
            "outbox_id": str(row[1]),
            "event_kind": str(row[2]),
            "payload": json.loads(str(row[3])),
            "created_at_us": int(row[4]),
        }
        for row in rows
    ]
