"""Authoritative persistence for the canonical Agent Runtime records (RT-102).

Storage primitives for `Run`, `RunStep`, `Attempt`, `Wait` and `RuntimeEvent` on
migration 0018, and nothing above them. There is no command envelope, no
`ResolveWait` handling, no admission decision and no status machine here: RT-104
owns the command/event-append transaction, and this module gives it the writes and
reads to build one out of.

Every public write function opens its own `fenced_transaction` rather than assuming
the caller did, so a repository call is durable authority-checked on entry and again
immediately before commit. That differs deliberately from `storage/jobs.py`, whose
functions run inside a transaction the mutation seam already opened; the Runtime
records have no such seam yet, and a function that quietly required one would be a
rule enforced nowhere.

:func:`runtime_writer` is that seam, made explicit. RT-104 has to settle an
idempotency claim and append run history in one transaction -- a command that
committed its answer and lost its events, or the reverse, is exactly the divergence
the claim relations exist to prevent -- and it cannot get there by calling the
standalone functions, because `BEGIN IMMEDIATE` does not nest. So the writes live on
:class:`RuntimeWriter`, which issues them into a transaction somebody else opened, and
the standalone functions are thin wrappers that open one first. There is one copy of
every statement, one fence per composition, and no way to reach the writer without a
fence: the context manager is what hands it out.

Two boundary decisions, stated rather than papered over:

* Reads return the generated contract records -- `RunStep`, `Attempt`, `Wait`,
  `RuntimeEvent` -- because each can be materialised honestly from what 0018 stores.
* A whole `Run` cannot be. The accepted contract requires a `PolicySnapshot`, a
  `BudgetSnapshot`, capability grants, artifacts, evidence and cleanup receipts,
  whose stores belong to RT-103 and RT-202+. `read_run` therefore returns
  :class:`RunSnapshot`, which states exactly what this migration holds, rather than
  a `Run` with six invented fields.

Every read takes the caller's workspace and filters on it in SQL. A workspace that
arrives inside a record is never the one a query runs against: an identifier without
the workspace it was issued in cannot be resolved, and could be resolved against the
wrong one.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ATTEMPT_STATUS_RUNNING,
    RUN_TERMINAL_STATUSES,
    WAIT_STATUS_PENDING,
    ApiError,
    Attempt,
    ContractDecodeError,
    ExternalReference,
    RunDefinitionRef,
    RunStep,
    RuntimeEvent,
    Wait,
    to_canonical_json,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError

_STEP_STATUS_WAITING: Final = "waiting"
_ATTEMPT_STATUS_FAILED: Final = "failed"

#: The correlation kind a canonical run's durable job is recorded under. Correlation
#: only: the job substrate is where a run is admitted and claimed, never authority
#: over the run's own record.
_JOB_SOURCE_KIND: Final = "application_job"


def _timestamp(value: int) -> str:
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def _digest(document: str) -> str:
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


def _stored_document(value: object) -> tuple[str, str, int]:
    """Canonical bytes, their digest and their length, for one nested document."""
    if not isinstance(value, dict):
        raise StorageError("a runtime document must be a JSON object")
    document = to_canonical_json(dict(value))
    return document, _digest(document), len(document.encode("utf-8"))


def _verified_document(
    text: object, digest: object, byte_length: object, label: str
) -> dict[str, Any]:
    """One stored nested document, recomputed before it is believed.

    The digest and the byte length are stored beside the bytes so that what was hashed
    is recorded next to the hash; a reader that returned the bytes without recomputing
    both would be treating the columns as decoration. The `CHECK` constraint holds the
    length only for writes that go through this database's own triggers -- a file
    edited offline reaches this function with whatever it was left holding, which is
    the case the recomputation exists for.

    Ordered digest, length, parse: a document whose digest is wrong is not this
    document, whatever it happens to decode to, so there is nothing to be gained by
    parsing it first.
    """
    document = str(text)
    encoded = document.encode("utf-8")
    if not isinstance(digest, str) or _digest(document) != digest:
        raise StorageError(f"a stored {label} does not match its recorded digest")
    if not isinstance(byte_length, int) or byte_length != len(encoded):
        raise StorageError(f"a stored {label} does not match its recorded byte length")
    try:
        decoded = json.loads(document)
    except json.JSONDecodeError as error:
        raise StorageError(f"a stored {label} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise StorageError(f"a stored {label} is not a JSON object")
    return decoded


def _stored_failure(text: object, digest: object, byte_length: object) -> ApiError:
    """One stored attempt failure, verified and decoded as the contract's `ApiError`.

    A payload that survives the digest and still fails to decode is a corrupt record,
    not a caller error, so it leaves here as a `StorageError` like every other unusable
    stored value rather than as a contract decoding failure from three layers down.
    """
    payload = _verified_document(text, digest, byte_length, "attempt failure")
    try:
        return ApiError.from_wire(payload)
    except ContractDecodeError as error:
        raise StorageError("a stored attempt failure is not a valid ApiError") from error


@dataclass(frozen=True, slots=True)
class RunAdmission:
    """Everything a canonical run states at admission, and nothing it learns later.

    The run row is immutable, so this is the complete set of facts recorded once.
    `job_id` names the existing `omnivia_durable_jobs` row that carries the run:
    admission and claiming stay with the scheduler, and this is the edge that says
    which job is which run.

    `claim_id` names the existing `omnivia_idempotency_claims` row the command was
    admitted under. It is the run's binding to the application's own replay authority
    rather than a second one: the claim's operation, idempotency key and audit
    reference must be this run's, and the migration refuses the admission otherwise.
    The claim need not have settled -- settling it is what RT-104 does *after* the run
    it admitted has answered.
    """

    run_id: str
    job_id: str
    claim_id: str
    definition: RunDefinitionRef
    logical_key: str
    originating_operation: str
    audit_ref: str
    admitted_at_us: int
    runtime_event_id: str
    event_kind: str = "run_admitted"
    message: str | None = None


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """What RT-102 can honestly report about one canonical run.

    Deliberately not a `Run`. The contract's aggregate requires a policy snapshot, a
    budget snapshot, capability grants, artifacts, evidence and cleanup receipts;
    none of those has a store yet, and filling them in to satisfy a type would
    report data nobody recorded. `status`, `updated_at` and `finished_at` are read
    from the event stream, which states the run status in force at every entry, so
    they are derived from stored facts rather than maintained beside them.
    """

    workspace_id: str
    run_id: str
    job_id: str
    claim_id: str
    definition: RunDefinitionRef
    status: str
    logical_key: str
    originating_operation: str
    audit_reference: str
    created_at: str
    updated_at: str
    finished_at: str | None
    steps: tuple[RunStep, ...]
    waits: tuple[Wait, ...]
    events: tuple[RuntimeEvent, ...]
    correlations: tuple[ExternalReference, ...]


# --- writes -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeWriter:
    """Every runtime write, issued into a transaction that is already open.

    Not constructible usefully on its own: :func:`runtime_writer` is what hands one
    out, and it only does so from inside a `fenced_transaction`. That is the whole
    point of the type -- the statements live somewhere a composition can reach them
    without any of them opening a second `BEGIN IMMEDIATE`, and the only route to that
    somewhere runs through a fence.

    The workspace is bound at construction rather than passed per call, because a
    composition is one workspace's work: the two writes RT-104 has to commit together
    belong to the same run in the same workspace, and threading the identifier through
    each call would let a caller mix two.
    """

    connection: sqlite3.Connection
    workspace_id: str

    def admit_run(self, admission: RunAdmission) -> None:
        """Record one canonical run and open its event stream at sequence zero.

        Both statements land together. A run whose stream had no `admitted` entry would
        have no status at all, because the stream is where a run's status lives.
        """
        self.connection.execute(
            "INSERT INTO omnivia_runtime_runs "
            "(workspace_id, run_id, job_id, claim_id, definition_kind, definition_id, "
            "definition_version, logical_key, originating_operation, audit_ref, "
            "created_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                admission.run_id,
                admission.job_id,
                admission.claim_id,
                admission.definition.definition_kind,
                admission.definition.definition_id,
                admission.definition.definition_version,
                admission.logical_key,
                admission.originating_operation,
                admission.audit_ref,
                admission.admitted_at_us,
            ),
        )
        self.connection.execute(
            "INSERT INTO omnivia_runtime_events "
            "(workspace_id, run_id, sequence, runtime_event_id, occurred_at_us, "
            "event_kind, run_status, message) VALUES (?, ?, 0, ?, ?, ?, 'admitted', ?)",
            (
                self.workspace_id,
                admission.run_id,
                admission.runtime_event_id,
                admission.admitted_at_us,
                admission.event_kind,
                admission.message,
            ),
        )

    def append_run_step(
        self,
        *,
        run_id: str,
        run_step_id: str,
        ordinal: int,
        step_kind: str,
        created_at_us: int,
        status: str = "pending",
    ) -> None:
        """Record one step of a run, with the status it starts in."""
        self.connection.execute(
            "INSERT INTO omnivia_runtime_run_steps "
            "(workspace_id, run_step_id, run_id, ordinal, step_kind, created_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.workspace_id, run_step_id, run_id, ordinal, step_kind, created_at_us),
        )
        self.connection.execute(
            "INSERT INTO omnivia_runtime_run_step_states "
            "(workspace_id, run_step_id, state_sequence, status, observed_at_us) "
            "VALUES (?, ?, 0, ?, ?)",
            (self.workspace_id, run_step_id, status, created_at_us),
        )

    def record_step_status(
        self, *, run_step_id: str, status: str, observed_at_us: int
    ) -> int:
        """Append one entry to a step's status history, and return its sequence."""
        sequence = self._next_sequence(
            "SELECT COALESCE(MAX(state_sequence), -1) + 1 "
            "FROM omnivia_runtime_run_step_states "
            "WHERE workspace_id = ? AND run_step_id = ?",
            (self.workspace_id, run_step_id),
        )
        self.connection.execute(
            "INSERT INTO omnivia_runtime_run_step_states "
            "(workspace_id, run_step_id, state_sequence, status, observed_at_us) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.workspace_id, run_step_id, sequence, status, observed_at_us),
        )
        return sequence

    def start_attempt(
        self,
        *,
        attempt_id: str,
        run_id: str,
        run_step_id: str,
        attempt_number: int,
        started_at_us: int,
    ) -> None:
        """Record that one execution of one step started."""
        self.connection.execute(
            "INSERT INTO omnivia_runtime_attempts "
            "(workspace_id, attempt_id, run_id, run_step_id, attempt_number, "
            "started_at_us) VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                attempt_id,
                run_id,
                run_step_id,
                attempt_number,
                started_at_us,
            ),
        )

    def finish_attempt(
        self,
        *,
        attempt_id: str,
        status: str,
        finished_at_us: int,
        failure: ApiError | None = None,
    ) -> None:
        """Terminalize one attempt, exactly once.

        A failure belongs to a `failed` attempt and to no other. An `uncertain` attempt
        carries none: uncertainty is not failure, and attaching an error to it would
        licence exactly the blind retry the uncertainty forbids. The schema refuses the
        mismatch either way; this raises before the statement so the caller sees which
        rule it broke.
        """
        if (failure is not None) != (status == _ATTEMPT_STATUS_FAILED):
            raise StorageError(
                f"a {status!r} attempt outcome does not carry a failure; "
                "only a failed one does"
            )
        document: str | None = None
        digest: str | None = None
        byte_length: int | None = None
        if failure is not None:
            document, digest, byte_length = _stored_document(failure.to_wire())
        self.connection.execute(
            "INSERT INTO omnivia_runtime_attempt_outcomes "
            "(workspace_id, attempt_id, status, finished_at_us, failure_json, "
            "failure_digest, failure_byte_length) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                attempt_id,
                status,
                finished_at_us,
                document,
                digest,
                byte_length,
            ),
        )

    def open_wait(
        self,
        *,
        wait_id: str,
        run_id: str,
        run_step_id: str,
        kind: str,
        created_at_us: int,
        resume_digest: str,
        expires_at_us: int | None = None,
    ) -> None:
        """Record one durable suspension of a run, bound to the state it resumes from."""
        self.connection.execute(
            "INSERT INTO omnivia_runtime_waits "
            "(workspace_id, wait_id, run_id, run_step_id, kind, created_at_us, "
            "expires_at_us, resume_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                wait_id,
                run_id,
                run_step_id,
                kind,
                created_at_us,
                expires_at_us,
                resume_digest,
            ),
        )

    def close_wait(
        self,
        *,
        wait_id: str,
        status: str,
        resolved_at_us: int,
        resolution_reason: str,
        approval_id: str | None = None,
    ) -> None:
        """Record that one wait stopped being pending, exactly once.

        Storage only. Whether the resolution matches the wait's kind, and whether the
        quoted resume digest is the one the wait published, are decisions the
        `ResolveWait` command makes -- RT-104's, not this module's. What is enforced
        here is that a wait stops being pending once, never before it was created, and
        that only a resolved approval wait names an approval.
        """
        self.connection.execute(
            "INSERT INTO omnivia_runtime_wait_resolutions "
            "(workspace_id, wait_id, status, resolved_at_us, resolution_reason, "
            "approval_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                wait_id,
                status,
                resolved_at_us,
                resolution_reason,
                approval_id,
            ),
        )

    def append_run_event(
        self,
        *,
        run_id: str,
        runtime_event_id: str,
        occurred_at_us: int,
        event_kind: str,
        run_status: str,
        run_step_id: str | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Append one entry to a run's event stream, and return its sequence.

        The sequence is allocated from the stream inside the transaction rather than
        taken from the caller, so two concurrent appends cannot agree on a number the
        trigger would then have to reject.
        """
        document: str | None = None
        digest: str | None = None
        byte_length: int | None = None
        if details is not None:
            document, digest, byte_length = _stored_document(details)
        sequence = self._next_sequence(
            "SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_runtime_events "
            "WHERE workspace_id = ? AND run_id = ?",
            (self.workspace_id, run_id),
        )
        self.connection.execute(
            "INSERT INTO omnivia_runtime_events "
            "(workspace_id, run_id, sequence, runtime_event_id, occurred_at_us, "
            "event_kind, run_status, run_step_id, message, details_json, "
            "details_digest, details_byte_length) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                run_id,
                sequence,
                runtime_event_id,
                occurred_at_us,
                event_kind,
                run_status,
                run_step_id,
                message,
                document,
                digest,
                byte_length,
            ),
        )
        return sequence

    def _next_sequence(self, query: str, parameters: tuple[object, ...]) -> int:
        row = self.connection.execute(query, parameters).fetchone()
        if row is None:  # pragma: no cover - an aggregate always returns one row
            raise StorageError("a sequence allocation returned no row")
        return int(row[0])


@contextmanager
def runtime_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[RuntimeWriter]:
    """One fenced transaction, and the runtime writes that may be issued into it.

    The composition seam RT-104 needs and RT-102 does not use for anything more than
    single writes. A caller holding the writer may also issue its own statements on the
    same connection -- the claim settlement, the command's audit event -- and all of
    them commit or roll back as one, under authority validated on entry and again
    immediately before commit.

    Storage only. This opens a transaction and lends out the writes; it decides no
    command, settles no claim and knows no `ResolveWait`.
    """
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield RuntimeWriter(connection, workspace_id)


def admit_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    admission: RunAdmission,
) -> None:
    """Record one canonical run and open its event stream, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.admit_run(admission)


def append_run_step(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    run_id: str,
    run_step_id: str,
    ordinal: int,
    step_kind: str,
    created_at_us: int,
    status: str = "pending",
) -> None:
    """Record one step of a run and its first status, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.append_run_step(
            run_id=run_id,
            run_step_id=run_step_id,
            ordinal=ordinal,
            step_kind=step_kind,
            created_at_us=created_at_us,
            status=status,
        )


def record_step_status(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    run_step_id: str,
    status: str,
    observed_at_us: int,
) -> int:
    """Append one entry to a step's status history, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.record_step_status(
            run_step_id=run_step_id, status=status, observed_at_us=observed_at_us
        )


def start_attempt(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    attempt_id: str,
    run_id: str,
    run_step_id: str,
    attempt_number: int,
    started_at_us: int,
) -> None:
    """Record that one execution of one step started, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.start_attempt(
            attempt_id=attempt_id,
            run_id=run_id,
            run_step_id=run_step_id,
            attempt_number=attempt_number,
            started_at_us=started_at_us,
        )


def finish_attempt(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    attempt_id: str,
    status: str,
    finished_at_us: int,
    failure: ApiError | None = None,
) -> None:
    """Terminalize one attempt, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.finish_attempt(
            attempt_id=attempt_id,
            status=status,
            finished_at_us=finished_at_us,
            failure=failure,
        )


def open_wait(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    wait_id: str,
    run_id: str,
    run_step_id: str,
    kind: str,
    created_at_us: int,
    resume_digest: str,
    expires_at_us: int | None = None,
) -> None:
    """Record one durable suspension of a run, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.open_wait(
            wait_id=wait_id,
            run_id=run_id,
            run_step_id=run_step_id,
            kind=kind,
            created_at_us=created_at_us,
            resume_digest=resume_digest,
            expires_at_us=expires_at_us,
        )


def close_wait(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    wait_id: str,
    status: str,
    resolved_at_us: int,
    resolution_reason: str,
    approval_id: str | None = None,
) -> None:
    """Record that one wait stopped being pending, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.close_wait(
            wait_id=wait_id,
            status=status,
            resolved_at_us=resolved_at_us,
            resolution_reason=resolution_reason,
            approval_id=approval_id,
        )


def append_run_event(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    run_id: str,
    runtime_event_id: str,
    occurred_at_us: int,
    event_kind: str,
    run_status: str,
    run_step_id: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Append one entry to a run's event stream, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.append_run_event(
            run_id=run_id,
            runtime_event_id=runtime_event_id,
            occurred_at_us=occurred_at_us,
            event_kind=event_kind,
            run_status=run_status,
            run_step_id=run_step_id,
            message=message,
            details=details,
        )


# --- reads --------------------------------------------------------------------


def read_run_id_by_logical_key(
    connection: sqlite3.Connection, *, workspace_id: str, logical_key: str
) -> str | None:
    """The run this logical key already names, when one exists.

    This is what makes a replay provable rather than assumed: equal keys are one
    run replayed, not two runs. It answers "which run" and nothing else -- whether
    a command is a replay, a conflict or a distinct request is decided by the
    existing scoped claim relations, which this module neither duplicates nor
    second-guesses.
    """
    row = connection.execute(
        "SELECT run_id FROM omnivia_runtime_runs "
        "WHERE workspace_id = ? AND logical_key = ?",
        (workspace_id, logical_key),
    ).fetchone()
    return None if row is None else str(row[0])


def read_run_id_by_job(
    connection: sqlite3.Connection, *, workspace_id: str, job_id: str
) -> str | None:
    """The canonical run this durable job carries, when it carries one."""
    row = connection.execute(
        "SELECT run_id FROM omnivia_runtime_runs WHERE workspace_id = ? AND job_id = ?",
        (workspace_id, job_id),
    ).fetchone()
    return None if row is None else str(row[0])


def read_run_events(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    start_sequence: int = 0,
    limit: int | None = None,
) -> tuple[RuntimeEvent, ...]:
    """One run's event stream, in ascending sequence.

    Sequence order is the replay order. It is a total order over the stream that no
    two entries can share, which a timestamp is not: two events recorded in the same
    microsecond would otherwise be returned in whatever order the page came back in.
    """
    rows = connection.execute(
        "SELECT sequence, runtime_event_id, occurred_at_us, event_kind, run_status, "
        "run_step_id, message, details_json, details_digest, details_byte_length "
        "FROM omnivia_runtime_events "
        "WHERE workspace_id = ? AND run_id = ? AND sequence >= ? "
        "ORDER BY sequence LIMIT ?",
        (workspace_id, run_id, start_sequence, -1 if limit is None else limit),
    ).fetchall()
    return tuple(
        RuntimeEvent(
            workspace_id=workspace_id,
            runtime_event_id=str(row[1]),
            run_id=run_id,
            sequence=int(row[0]),
            occurred_at=_timestamp(int(row[2])),
            event_kind=str(row[3]),
            run_status=str(row[4]),
            run_step_id=None if row[5] is None else str(row[5]),
            message=None if row[6] is None else str(row[6]),
            details=(
                None
                if row[7] is None
                else _verified_document(row[7], row[8], row[9], "runtime event detail")
            ),
        )
        for row in rows
    )


def read_run_waits(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[Wait, ...]:
    """Every wait this run has entered, in creation order then identifier order."""
    rows = connection.execute(
        "SELECT w.wait_id, w.run_step_id, w.kind, w.created_at_us, w.expires_at_us, "
        "w.resume_digest, r.status, r.resolved_at_us, r.resolution_reason, "
        "r.approval_id FROM omnivia_runtime_waits w "
        "LEFT JOIN omnivia_runtime_wait_resolutions r "
        "ON r.workspace_id = w.workspace_id AND r.wait_id = w.wait_id "
        "WHERE w.workspace_id = ? AND w.run_id = ? "
        "ORDER BY w.created_at_us, w.wait_id",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(
        Wait(
            workspace_id=workspace_id,
            wait_id=str(row[0]),
            run_id=run_id,
            run_step_id=str(row[1]),
            kind=str(row[2]),
            status=WAIT_STATUS_PENDING if row[6] is None else str(row[6]),
            created_at=_timestamp(int(row[3])),
            expires_at=None if row[4] is None else _timestamp(int(row[4])),
            resolved_at=None if row[7] is None else _timestamp(int(row[7])),
            resolution_reason=None if row[8] is None else str(row[8]),
            approval_id=None if row[9] is None else str(row[9]),
            resume_digest=str(row[5]),
        )
        for row in rows
    )


def _attempts_by_step(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> dict[str, list[Attempt]]:
    rows = connection.execute(
        "SELECT a.run_step_id, a.attempt_id, a.attempt_number, a.started_at_us, "
        "o.status, o.finished_at_us, o.failure_json, o.failure_digest, "
        "o.failure_byte_length FROM omnivia_runtime_attempts a "
        "LEFT JOIN omnivia_runtime_attempt_outcomes o "
        "ON o.workspace_id = a.workspace_id AND o.attempt_id = a.attempt_id "
        "WHERE a.workspace_id = ? AND a.run_id = ? "
        "ORDER BY a.run_step_id, a.attempt_number",
        (workspace_id, run_id),
    ).fetchall()
    grouped: dict[str, list[Attempt]] = {}
    for row in rows:
        grouped.setdefault(str(row[0]), []).append(
            Attempt(
                workspace_id=workspace_id,
                attempt_id=str(row[1]),
                run_id=run_id,
                run_step_id=str(row[0]),
                attempt_number=int(row[2]),
                status=ATTEMPT_STATUS_RUNNING if row[4] is None else str(row[4]),
                started_at=_timestamp(int(row[3])),
                finished_at=None if row[5] is None else _timestamp(int(row[5])),
                failure=(
                    None if row[6] is None else _stored_failure(row[6], row[7], row[8])
                ),
            )
        )
    return grouped


def _unresolved_wait_by_step(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> dict[str, str]:
    rows = connection.execute(
        "SELECT w.run_step_id, w.wait_id FROM omnivia_runtime_waits w "
        "LEFT JOIN omnivia_runtime_wait_resolutions r "
        "ON r.workspace_id = w.workspace_id AND r.wait_id = w.wait_id "
        "WHERE w.workspace_id = ? AND w.run_id = ? AND r.wait_id IS NULL",
        (workspace_id, run_id),
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def read_run_steps(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[RunStep, ...]:
    """Every step of this run, in ascending ordinal, each with its attempt history.

    A step's status and `updated_at` come from the latest entry of its status
    history, and a `waiting` step names the wait still holding it. A step with no
    status history at all is a corrupt row rather than a pending step, and says so.
    """
    rows = connection.execute(
        "SELECT s.run_step_id, s.ordinal, s.step_kind, s.created_at_us, "
        "(SELECT status FROM omnivia_runtime_run_step_states "
        " WHERE workspace_id = s.workspace_id AND run_step_id = s.run_step_id "
        " ORDER BY state_sequence DESC LIMIT 1), "
        "(SELECT observed_at_us FROM omnivia_runtime_run_step_states "
        " WHERE workspace_id = s.workspace_id AND run_step_id = s.run_step_id "
        " ORDER BY state_sequence DESC LIMIT 1) "
        "FROM omnivia_runtime_run_steps s "
        "WHERE s.workspace_id = ? AND s.run_id = ? ORDER BY s.ordinal",
        (workspace_id, run_id),
    ).fetchall()
    attempts = _attempts_by_step(connection, workspace_id=workspace_id, run_id=run_id)
    holding = _unresolved_wait_by_step(
        connection, workspace_id=workspace_id, run_id=run_id
    )
    steps: list[RunStep] = []
    for row in rows:
        run_step_id = str(row[0])
        if row[4] is None:
            raise StorageError(f"run step {run_step_id!r} has no recorded status")
        status = str(row[4])
        steps.append(
            RunStep(
                workspace_id=workspace_id,
                run_step_id=run_step_id,
                run_id=run_id,
                ordinal=int(row[1]),
                step_kind=str(row[2]),
                status=status,
                created_at=_timestamp(int(row[3])),
                updated_at=_timestamp(int(row[5])),
                attempts=tuple(attempts.get(run_step_id, ())),
                wait_id=(
                    holding.get(run_step_id)
                    if status == _STEP_STATUS_WAITING
                    else None
                ),
            )
        )
    return tuple(steps)


def read_run(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> RunSnapshot | None:
    """One run and the history RT-102 holds for it, or `None` when there is no such run."""
    row = connection.execute(
        "SELECT job_id, definition_kind, definition_id, definition_version, "
        "logical_key, originating_operation, audit_ref, created_at_us, claim_id "
        "FROM omnivia_runtime_runs WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        return None
    events = read_run_events(connection, workspace_id=workspace_id, run_id=run_id)
    if not events:
        raise StorageError(f"run {run_id!r} has no event stream to read its status from")
    latest = events[-1]
    job_id = str(row[0])
    return RunSnapshot(
        workspace_id=workspace_id,
        run_id=run_id,
        job_id=job_id,
        claim_id=str(row[8]),
        definition=RunDefinitionRef(
            definition_kind=str(row[1]),
            definition_id=str(row[2]),
            definition_version=str(row[3]),
        ),
        status=latest.run_status,
        logical_key=str(row[4]),
        originating_operation=str(row[5]),
        audit_reference=str(row[6]),
        created_at=_timestamp(int(row[7])),
        updated_at=latest.occurred_at,
        finished_at=(
            latest.occurred_at if latest.run_status in RUN_TERMINAL_STATUSES else None
        ),
        steps=read_run_steps(connection, workspace_id=workspace_id, run_id=run_id),
        waits=read_run_waits(connection, workspace_id=workspace_id, run_id=run_id),
        events=events,
        correlations=(
            ExternalReference(
                source_kind=_JOB_SOURCE_KIND,
                source_id=job_id,
                workspace_id=workspace_id,
            ),
        ),
    )


def read_workspace_run_ids(
    connection: sqlite3.Connection, *, workspace_id: str
) -> tuple[str, ...]:
    """Every run in this workspace, oldest admission first then identifier order."""
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT run_id FROM omnivia_runtime_runs WHERE workspace_id = ? "
            "ORDER BY created_at_us, run_id",
            (workspace_id,),
        ).fetchall()
    )


__all__ = [
    "RunAdmission",
    "RunSnapshot",
    "RuntimeWriter",
    "admit_run",
    "append_run_event",
    "append_run_step",
    "close_wait",
    "finish_attempt",
    "open_wait",
    "read_run",
    "read_run_events",
    "read_run_id_by_job",
    "read_run_id_by_logical_key",
    "read_run_steps",
    "read_run_waits",
    "read_workspace_run_ids",
    "record_step_status",
    "runtime_writer",
    "start_attempt",
]
