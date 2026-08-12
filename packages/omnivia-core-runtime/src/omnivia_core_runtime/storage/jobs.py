"""Authoritative persistence for the V06-5 S3 durable-job operation family."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ApiError,
    ImportStartInput,
    JobAttempt,
    JobCancellationOutcome,
    JobControl,
    JobHandle,
    JobIdentity,
    JobProgress,
    JobTerminalCancellation,
    JobTerminalFailure,
    JobTerminalSuccess,
    to_canonical_json,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.service.operations import OperationError
from omnivia_core_runtime.storage.memory import IdentifierAllocator, random_identifier

_MESSAGE_SOURCE_UNAVAILABLE: Final = (
    "the staged import source is not available with the declared immutable facts"
)
_MESSAGE_JOB_NOT_FOUND: Final = "the requested job was not found"
_RECOVERY_ERROR: Final[dict[str, object]] = {
    "code": "internal_recoverable",
    "message": "the previous worker lost its fencing authority",
    "retry_class": "retryable",
}


def _timestamp(value: int) -> str:
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def _now_us(clock: Clock) -> int:
    return int(clock.wall_time().timestamp() * 1_000_000)


def _digest(document: str) -> str:
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


def _document(value: Mapping[str, object]) -> str:
    return to_canonical_json(dict(value))


def _next_number(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    *,
    workspace_id: str,
    job_id: str,
    base: int,
) -> int:
    row = connection.execute(
        f"SELECT COALESCE(MAX({column}), ?) + 1 FROM {table} "
        "WHERE workspace_id = ? AND job_id = ?",
        (base, workspace_id, job_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _public_state(value: str) -> str:
    return "running" if value == "claimed" else value


def _load_json(value: object) -> dict[str, Any]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise TypeError("stored job document is not an object")
    return decoded


def _attempts(
    connection: sqlite3.Connection, *, workspace_id: str, job_id: str
) -> tuple[JobAttempt, ...]:
    rows = connection.execute(
        "SELECT attempt_number, started_at_us, finished_at_us, state, error_json "
        "FROM omnivia_job_attempts WHERE workspace_id = ? AND job_id = ? "
        "ORDER BY attempt_number",
        (workspace_id, job_id),
    ).fetchall()
    values: list[JobAttempt] = []
    for number, started, finished, state, error_json in rows:
        values.append(
            JobAttempt(
                attempt_number=int(number),
                started_at=_timestamp(int(started)),
                finished_at=None if finished is None else _timestamp(int(finished)),
                state=str(state),
                error=(
                    None
                    if error_json is None
                    else ApiError.from_wire(_load_json(error_json))
                ),
            )
        )
    return tuple(values)


def read_accepted_import_source(
    connection: sqlite3.Connection, *, workspace_id: str, job_id: str
) -> Mapping[str, object] | None:
    row = connection.execute(
        "SELECT staged_source_ref, source_kind, content_checksum, "
        "content_length_bytes, media_type, source_version "
        "FROM omnivia_application_import_claims "
        "WHERE workspace_id = ? AND job_id = ?",
        (workspace_id, job_id),
    ).fetchone()
    if row is None:
        return None
    source: dict[str, object] = {
        "staged_source_ref": str(row[0]),
        "source_kind": str(row[1]),
        "content_checksum": str(row[2]),
        "content_length_bytes": int(row[3]),
        "media_type": str(row[4]),
    }
    if row[5] is not None:
        source["source_version"] = str(row[5])
    return source


def read_job_id_by_origin_audit(
    connection: sqlite3.Connection, *, workspace_id: str, audit_ref: str
) -> str | None:
    row = connection.execute(
        "SELECT job_id FROM omnivia_application_import_claims "
        "WHERE workspace_id = ? AND audit_ref = ?",
        (workspace_id, audit_ref),
    ).fetchone()
    return None if row is None else str(row[0])


def _control(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    job_id: str,
    state: str,
    attempts: tuple[JobAttempt, ...],
    supports_checkpoint_resume: bool,
    max_attempts: int,
) -> JobControl:
    if state == "cancelled":
        cancellation = "cancelled"
    elif state in {"succeeded", "failed"}:
        cancellation = "not_cancellable"
    else:
        pending = connection.execute(
            "SELECT 1 FROM omnivia_application_job_controls c "
            "WHERE c.workspace_id = ? AND c.job_id = ? "
            "AND c.operation = 'job.cancel' "
            "AND c.disposition = 'cancellation_requested' "
            "AND c.settled_at_us > COALESCE((SELECT MAX(o.finished_at_us) "
            "FROM omnivia_job_terminal_observations o "
            "WHERE o.workspace_id = c.workspace_id AND o.job_id = c.job_id), 0) "
            "LIMIT 1",
            (workspace_id, job_id),
        ).fetchone()
        cancellation = "cancellation_pending" if pending is not None else "cancellable"

    recovery = "not_retryable"
    latest = attempts[-1] if attempts else None
    if state == "failed" and latest is not None and len(attempts) < max_attempts:
        if latest.error is not None and latest.error.retry_class == "retryable":
            recovery = "retryable"
    elif state == "cancelled" and supports_checkpoint_resume and len(attempts) < max_attempts:
        checkpoint = connection.execute(
            "SELECT 1 FROM omnivia_job_checkpoints WHERE workspace_id = ? "
            "AND job_id = ? LIMIT 1",
            (workspace_id, job_id),
        ).fetchone()
        if checkpoint is not None:
            recovery = "resumable"
    return JobControl(cancellation=cancellation, recovery=recovery)


def read_application_job_snapshot(
    connection: sqlite3.Connection, *, workspace_id: str, job_id: str
) -> dict[str, object] | None:
    row = connection.execute(
        "SELECT j.state, m.job_kind, m.originating_operation, m.audit_ref, "
        "m.created_at_us, m.supports_checkpoint_resume, m.max_attempts, "
        "CAST(strftime('%s', j.updated_at) AS INTEGER) * 1000000 "
        "FROM omnivia_durable_jobs j "
        "JOIN omnivia_job_application_metadata m ON m.job_id = j.job_id "
        "WHERE m.workspace_id = ? AND m.job_id = ?",
        (workspace_id, job_id),
    ).fetchone()
    if row is None:
        return None
    state = _public_state(str(row[0]))
    identity = JobIdentity(
        job_id=job_id,
        job_kind=str(row[1]),
        originating_operation=str(row[2]),
        audit_reference=str(row[3]),
        workspace_id=workspace_id,
    )
    created_at_us = int(row[4])
    history = _attempts(connection, workspace_id=workspace_id, job_id=job_id)
    progress_row = connection.execute(
        "SELECT unit, completed_units, total_units, message "
        "FROM omnivia_job_progress_events WHERE workspace_id = ? AND job_id = ? "
        "ORDER BY progress_sequence DESC LIMIT 1",
        (workspace_id, job_id),
    ).fetchone()
    progress = (
        None
        if progress_row is None
        else JobProgress(
            unit=str(progress_row[0]),
            completed_units=int(progress_row[1]),
            total_units=(None if progress_row[2] is None else int(progress_row[2])),
            message=None if progress_row[3] is None else str(progress_row[3]),
        )
    )
    event_time = connection.execute(
        "SELECT MAX(occurred_at_us) FROM omnivia_job_events "
        "WHERE workspace_id = ? AND job_id = ?",
        (workspace_id, job_id),
    ).fetchone()
    updated_at_us = max(
        created_at_us,
        0 if row[7] is None else int(row[7]),
        0 if event_time is None or event_time[0] is None else int(event_time[0]),
    )
    handle = JobHandle(
        identity=identity,
        state=state,
        created_at=_timestamp(created_at_us),
        updated_at=_timestamp(updated_at_us),
        control=_control(
            connection,
            workspace_id=workspace_id,
            job_id=job_id,
            state=state,
            attempts=history,
            supports_checkpoint_resume=bool(row[5]),
            max_attempts=int(row[6]),
        ),
        progress=progress,
        latest_attempt=history[-1] if history else None,
    )
    terminal_row = connection.execute(
        "SELECT terminal_state, finished_at_us, result_kind, result_json, "
        "error_json, cancellation_reason FROM omnivia_job_terminal_observations "
        "WHERE workspace_id = ? AND job_id = ? "
        "ORDER BY terminal_observation_number DESC LIMIT 1",
        (workspace_id, job_id),
    ).fetchone()
    terminal: JobTerminalSuccess | JobTerminalFailure | JobTerminalCancellation | None = None
    if terminal_row is not None and str(terminal_row[0]) == state:
        finished = _timestamp(int(terminal_row[1]))
        if state == "succeeded":
            terminal = JobTerminalSuccess(
                identity=identity,
                state=state,
                finished_at=finished,
                attempts=history,
                result_kind=str(terminal_row[2]),
                result=_load_json(terminal_row[3]),
            )
        elif state == "failed":
            terminal = JobTerminalFailure(
                identity=identity,
                state=state,
                finished_at=finished,
                attempts=history,
                error=ApiError.from_wire(_load_json(terminal_row[4])),
            )
        else:
            terminal = JobTerminalCancellation(
                identity=identity,
                state=state,
                finished_at=finished,
                attempts=history,
                cancellation=JobCancellationOutcome(reason=str(terminal_row[5])),
            )
    result: dict[str, object] = {"job": handle.to_wire()}
    if terminal is not None:
        result["terminal_result"] = terminal.to_wire()
    return result


def require_staged_import_source(
    connection: sqlite3.Connection, *, workspace_id: str, claim: ImportStartInput
) -> None:
    source = claim.source
    row = connection.execute(
        "SELECT 1 FROM omnivia_staged_sources s WHERE s.workspace_id = ? "
        "AND s.staged_source_ref = ? AND s.source_kind = ? "
        "AND s.declared_checksum = ? AND s.content_length_bytes = ? "
        "AND s.media_type = ? AND s.source_version IS ? "
        "AND s.staging_outcome = 'verified' AND s.blob_workspace_id = ? "
        "AND s.blob_content_digest = ?",
        (
            workspace_id,
            source.staged_source_ref,
            source.source_kind,
            source.content_checksum,
            source.content_length_bytes,
            source.media_type,
            source.source_version,
            workspace_id,
            source.content_checksum,
        ),
    ).fetchone()
    if row is None:
        raise OperationError(
            "dependency_unavailable",
            _MESSAGE_SOURCE_UNAVAILABLE,
            retry_class="retryable_after_delay",
        )


def start_import_job(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    claim: ImportStartInput,
    fencing_generation: int,
    claimed_by_service_instance: str,
    allocate_identifier: IdentifierAllocator = random_identifier,
) -> dict[str, object]:
    require_staged_import_source(connection, workspace_id=workspace_id, claim=claim)
    job_id = allocate_identifier("job")
    moment = _timestamp(settlement.settled_at_us)
    input_json = _document(claim.to_wire())
    connection.execute(
        "INSERT INTO omnivia_durable_jobs "
        "(job_id, job_type, state, payload_json, created_at, updated_at, "
        "fencing_generation, claimed_by_service_instance) "
        "VALUES (?, 'ingestion.import', 'claimed', ?, ?, ?, ?, ?)",
        (
            job_id,
            input_json,
            moment,
            moment,
            fencing_generation,
            claimed_by_service_instance,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_job_application_metadata "
        "(workspace_id, job_id, job_kind, originating_operation, audit_ref, "
        "created_at_us, terminal_result_kind, supports_checkpoint_resume, max_attempts) "
        "VALUES (?, ?, 'ingestion.import', 'import.start', ?, ?, "
        "'import_completion', 1, 3)",
        (workspace_id, job_id, settlement.audit_ref, settlement.settled_at_us),
    )
    source = claim.source
    connection.execute(
        "INSERT INTO omnivia_application_import_claims "
        "(workspace_id, job_id, audit_ref, staged_source_ref, source_kind, "
        "content_checksum, content_length_bytes, media_type, source_version, "
        "input_json, input_digest, input_byte_length, settled_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            job_id,
            settlement.audit_ref,
            source.staged_source_ref,
            source.source_kind,
            source.content_checksum,
            source.content_length_bytes,
            source.media_type,
            source.source_version,
            input_json,
            _digest(input_json),
            len(input_json.encode()),
            settlement.settled_at_us,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_job_attempts "
        "(workspace_id, job_id, attempt_number, started_at_us, state) "
        "VALUES (?, ?, 1, ?, 'running')",
        (workspace_id, job_id, settlement.settled_at_us),
    )
    connection.execute(
        "INSERT INTO omnivia_job_events "
        "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
        "VALUES (?, ?, 0, ?, 'running', 'import started')",
        (workspace_id, job_id, settlement.settled_at_us),
    )
    snapshot = read_application_job_snapshot(
        connection, workspace_id=workspace_id, job_id=job_id
    )
    assert snapshot is not None
    return snapshot


def _insert_control(
    connection: sqlite3.Connection,
    *,
    settlement: MutationSettlementContext,
    workspace_id: str,
    job_id: str,
    operation: str,
    disposition: str,
    source_state: str,
    resulting_state: str,
    fencing_generation: int,
    payload: Mapping[str, object],
    source_observation: int | None = None,
    allocate_identifier: IdentifierAllocator = random_identifier,
) -> None:
    document = _document(payload)
    connection.execute(
        "INSERT INTO omnivia_application_job_controls "
        "(workspace_id, control_id, job_id, control_kind, operation, disposition, "
        "source_state, resulting_state, source_terminal_observation_number, "
        "audit_ref, fencing_generation, control_json, control_digest, "
        "control_byte_length, settled_at_us) VALUES "
        "(?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            allocate_identifier("ctl"),
            job_id,
            operation,
            disposition,
            source_state,
            resulting_state,
            source_observation,
            settlement.audit_ref,
            fencing_generation,
            document,
            _digest(document),
            len(document.encode()),
            settlement.settled_at_us,
        ),
    )


def request_job_cancellation(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    job_id: str,
    reason: str | None,
    fencing_generation: int,
    allocate_identifier: IdentifierAllocator = random_identifier,
) -> dict[str, object]:
    before = read_application_job_snapshot(
        connection, workspace_id=workspace_id, job_id=job_id
    )
    if before is None:
        raise OperationError("not_found", _MESSAGE_JOB_NOT_FOUND)
    handle = before["job"]
    assert isinstance(handle, Mapping)
    source_state = str(handle["state"])
    control = handle["control"]
    assert isinstance(control, Mapping)
    available = str(control["cancellation"])
    if available == "cancellable":
        disposition = "cancellation_requested"
    elif source_state == "cancelled":
        disposition = "cancelled"
    else:
        disposition = "not_cancellable"
    _insert_control(
        connection,
        settlement=settlement,
        workspace_id=workspace_id,
        job_id=job_id,
        operation="job.cancel",
        disposition=disposition,
        source_state=source_state,
        resulting_state=source_state,
        fencing_generation=fencing_generation,
        payload={"job_id": job_id, **({} if reason is None else {"reason": reason})},
        allocate_identifier=allocate_identifier,
    )
    if disposition == "cancellation_requested":
        sequence = _next_number(
            connection,
            "omnivia_job_events",
            "sequence",
            workspace_id=workspace_id,
            job_id=job_id,
            base=-1,
        )
        connection.execute(
            "INSERT INTO omnivia_job_events "
            "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
            "VALUES (?, ?, ?, ?, ?, 'job cancellation requested')",
            (workspace_id, job_id, sequence, settlement.settled_at_us, source_state),
        )
    result_snapshot = read_application_job_snapshot(
        connection, workspace_id=workspace_id, job_id=job_id
    )
    assert result_snapshot is not None
    return {
        "job": result_snapshot["job"],
        "cancellation_disposition": disposition,
    }


def request_job_retry(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    job_id: str,
    fencing_generation: int,
    allocate_identifier: IdentifierAllocator = random_identifier,
) -> dict[str, object]:
    before = read_application_job_snapshot(
        connection, workspace_id=workspace_id, job_id=job_id
    )
    if before is None:
        raise OperationError("not_found", _MESSAGE_JOB_NOT_FOUND)
    handle = before["job"]
    assert isinstance(handle, Mapping)
    source_state = str(handle["state"])
    control = handle["control"]
    assert isinstance(control, Mapping)
    recovery = str(control["recovery"])
    if source_state == "failed" and recovery == "retryable":
        disposition = "retry_scheduled"
    elif source_state == "cancelled" and recovery == "resumable":
        disposition = "resume_scheduled"
    else:
        disposition = "not_retryable"
    observation: int | None = None
    resulting_state = source_state
    if disposition != "not_retryable":
        row = connection.execute(
            "SELECT MAX(terminal_observation_number) "
            "FROM omnivia_job_terminal_observations "
            "WHERE workspace_id = ? AND job_id = ?",
            (workspace_id, job_id),
        ).fetchone()
        assert row is not None and row[0] is not None
        observation = int(row[0])
        resulting_state = "queued"
        moment = _timestamp(settlement.settled_at_us)
        connection.execute(
            "UPDATE omnivia_durable_jobs SET state = 'queued', updated_at = ?, "
            "claimed_by_service_instance = NULL, fencing_generation = ? "
            "WHERE job_id = ?",
            (moment, fencing_generation, job_id),
        )
    _insert_control(
        connection,
        settlement=settlement,
        workspace_id=workspace_id,
        job_id=job_id,
        operation="job.retry",
        disposition=disposition,
        source_state=source_state,
        resulting_state=resulting_state,
        source_observation=observation,
        fencing_generation=fencing_generation,
        payload={"job_id": job_id},
        allocate_identifier=allocate_identifier,
    )
    if resulting_state == "queued":
        sequence = _next_number(
            connection,
            "omnivia_job_events",
            "sequence",
            workspace_id=workspace_id,
            job_id=job_id,
            base=-1,
        )
        connection.execute(
            "INSERT INTO omnivia_job_events "
            "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
            "VALUES (?, ?, ?, ?, 'queued', 'job recovery scheduled')",
            (workspace_id, job_id, sequence, settlement.settled_at_us),
        )
    result_snapshot = read_application_job_snapshot(
        connection, workspace_id=workspace_id, job_id=job_id
    )
    assert result_snapshot is not None
    return {"job": result_snapshot["job"], "recovery_disposition": disposition}


def read_application_job_events(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    job_id: str,
    start_sequence: int,
    snapshot_event_count: int,
    limit: int,
) -> tuple[dict[str, object], ...] | None:
    if read_application_job_snapshot(
        connection, workspace_id=workspace_id, job_id=job_id
    ) is None:
        return None
    rows = connection.execute(
        "SELECT sequence, occurred_at_us, state, message, details_json "
        "FROM omnivia_job_events WHERE workspace_id = ? AND job_id = ? "
        "AND sequence >= ? AND sequence < ? ORDER BY sequence LIMIT ?",
        (workspace_id, job_id, start_sequence, snapshot_event_count, limit),
    ).fetchall()
    events: list[dict[str, object]] = []
    for sequence, occurred, state, message, details_json in rows:
        event: dict[str, object] = {
            "sequence": int(sequence),
            "occurred_at": _timestamp(int(occurred)),
            "state": str(state),
        }
        if message is not None:
            event["message"] = str(message)
        if details_json is not None:
            event["details"] = _load_json(details_json)
        events.append(event)
    return tuple(events)


def application_job_event_count(
    connection: sqlite3.Connection, *, workspace_id: str, job_id: str
) -> int | None:
    if read_application_job_snapshot(
        connection, workspace_id=workspace_id, job_id=job_id
    ) is None:
        return None
    row = connection.execute(
        "SELECT COUNT(*) FROM omnivia_job_events WHERE workspace_id = ? AND job_id = ?",
        (workspace_id, job_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def recover_stranded_application_jobs(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    now_us: int,
    clock: Clock,
) -> list[dict[str, object]]:
    del clock
    recovered_ids: list[str] = []
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        rows = connection.execute(
            "SELECT j.job_id FROM omnivia_durable_jobs j "
            "JOIN omnivia_job_application_metadata m ON m.job_id = j.job_id "
            "WHERE m.workspace_id = ? AND j.state = 'claimed' "
            "AND COALESCE(j.fencing_generation, 0) < ? ORDER BY j.job_id",
            (workspace_id, fencing_generation),
        ).fetchall()
        for (raw_job_id,) in rows:
            job_id = str(raw_job_id)
            connection.execute(
                "UPDATE omnivia_durable_jobs SET state = 'failed', updated_at = ?, "
                "claimed_by_service_instance = NULL, fencing_generation = ? "
                "WHERE job_id = ?",
                (_timestamp(now_us), fencing_generation, job_id),
            )
            error_json = _document(_RECOVERY_ERROR)
            connection.execute(
                "UPDATE omnivia_job_attempts SET state = 'failed', finished_at_us = ?, "
                "error_json = ? WHERE workspace_id = ? AND job_id = ? "
                "AND attempt_number = (SELECT MAX(a.attempt_number) "
                "FROM omnivia_job_attempts a WHERE a.workspace_id = ? AND a.job_id = ?) "
                "AND state = 'running'",
                (now_us, error_json, workspace_id, job_id, workspace_id, job_id),
            )
            failed_sequence = _next_number(
                connection,
                "omnivia_job_events",
                "sequence",
                workspace_id=workspace_id,
                job_id=job_id,
                base=-1,
            )
            connection.execute(
                "INSERT INTO omnivia_job_events "
                "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
                "VALUES (?, ?, ?, ?, 'failed', 'interrupted attempt failed')",
                (workspace_id, job_id, failed_sequence, now_us),
            )
            attempt_row = connection.execute(
                "SELECT MAX(attempt_number) FROM omnivia_job_attempts "
                "WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
            assert attempt_row is not None and attempt_row[0] is not None
            observation = _next_number(
                connection,
                "omnivia_job_terminal_observations",
                "terminal_observation_number",
                workspace_id=workspace_id,
                job_id=job_id,
                base=0,
            )
            connection.execute(
                "INSERT INTO omnivia_job_terminal_observations "
                "(workspace_id, job_id, terminal_observation_number, attempt_number, "
                "terminal_state, finished_at_us, error_json, provenance_kind, "
                "fencing_generation) VALUES "
                "(?, ?, ?, ?, 'failed', ?, ?, 'service_committed', ?)",
                (
                    workspace_id,
                    job_id,
                    observation,
                    int(attempt_row[0]),
                    now_us,
                    error_json,
                    fencing_generation,
                ),
            )
            connection.execute(
                "UPDATE omnivia_durable_jobs SET state = 'queued' WHERE job_id = ?",
                (job_id,),
            )
            control_json = _document({"reason": "fencing_generation_advanced"})
            connection.execute(
                "INSERT INTO omnivia_application_job_controls "
                "(workspace_id, control_id, job_id, control_kind, operation, "
                "disposition, source_state, resulting_state, audit_ref, "
                "fencing_generation, control_json, control_digest, "
                "control_byte_length, settled_at_us) VALUES "
                "(?, ?, ?, 'system', 'system.recovery', 'recovery_requeued', "
                "'running', 'queued', NULL, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    f"ctl-recovery-{job_id}-{fencing_generation}",
                    job_id,
                    fencing_generation,
                    control_json,
                    _digest(control_json),
                    len(control_json.encode()),
                    now_us,
                ),
            )
            sequence = _next_number(
                connection,
                "omnivia_job_events",
                "sequence",
                workspace_id=workspace_id,
                job_id=job_id,
                base=-1,
            )
            connection.execute(
                "INSERT INTO omnivia_job_events "
                "(workspace_id, job_id, sequence, occurred_at_us, state, message) "
                "VALUES (?, ?, ?, ?, 'queued', 'interrupted job recovered')",
                (workspace_id, job_id, sequence, now_us),
            )
            recovered_ids.append(job_id)
    return [
        snapshot
        for job_id in recovered_ids
        if (
            snapshot := read_application_job_snapshot(
                connection, workspace_id=workspace_id, job_id=job_id
            )
        )
        is not None
    ]


__all__ = [
    "application_job_event_count",
    "read_accepted_import_source",
    "read_application_job_events",
    "read_application_job_snapshot",
    "read_job_id_by_origin_audit",
    "recover_stranded_application_jobs",
    "request_job_cancellation",
    "request_job_retry",
    "require_staged_import_source",
    "start_import_job",
]
