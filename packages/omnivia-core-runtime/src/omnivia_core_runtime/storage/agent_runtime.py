"""Authoritative persistence for the canonical Agent Runtime records (RT-102, RT-103, RT-202, RT-203).

Storage primitives for `Run`, `RunStep`, `Attempt`, `Wait`, `RuntimeEvent` (migration
0018), `Artifact`, `EvidenceItem` and `CleanupReceipt` (migration 0019),
`PolicySnapshot` and `BudgetSnapshot` (migration 0021), `Approval` and
`CapabilityGrant` (migration 0022), and nothing above them. There is
no command envelope, no `ResolveWait` handling, no admission decision and no status
machine here: RT-104 owns the command/event-append transaction, and this module gives it
the writes and reads to build one out of.

Every public write function opens its own `fenced_transaction` rather than assuming
the caller did, so a repository call is durable authority-checked on entry and again
immediately before commit. That differs deliberately from `storage/jobs.py`, whose
functions run inside a transaction the mutation seam already opened; the Runtime
records have no such seam yet, and a function that quietly required one would be a
rule enforced nowhere.

:class:`RuntimeWriter` is that seam, made explicit. RT-104 has to settle an
idempotency claim and append run history in one transaction -- a command that
committed its answer and lost its events, or the reverse, is exactly the divergence
the claim relations exist to prevent -- and it cannot get there by calling the
standalone functions, because `BEGIN IMMEDIATE` does not nest. So the writes live on
the writer, which issues them into a transaction somebody else opened, and the
standalone functions are thin wrappers that open one first. Two functions hand a
writer out and there is no third: :func:`runtime_writer` for a caller with no
transaction, which opens the fence itself, and :func:`transaction_local_writer` for
one already inside a fence somebody else opened -- which is where the mutation seam
puts RT-104's command. There is one copy of every statement and one fence per
composition either way.

Two boundary decisions, stated rather than papered over:

* Reads return the generated contract records -- `RunStep`, `Attempt`, `Wait`,
  `RuntimeEvent`, `Artifact`, `EvidenceItem`, `CleanupReceipt`, `PolicySnapshot`,
  `BudgetSnapshot`, `Approval`, `CapabilityGrant` -- because each can be materialised
  honestly from what 0018, 0019, 0021 and 0022 store.
* A whole `Run` still cannot be. 0022 gives `read_run` the run's approvals and
  capability grants on top of 0021's latest policy and budget, but the accepted
  aggregate also requires the effect family, whose store belongs to RT-203's successor.
  `read_run` therefore keeps returning :class:`RunSnapshot`, whose `policy` and `budget`
  are optional because a run admitted before 0021 -- or one whose decisions were never
  recorded -- has neither, rather than a `Run` with the remaining fields invented.

A policy or budget snapshot, and a capability grant, is stored as the complete canonical
v1 wire document plus the digest and byte length of exactly those bytes. The digest
addresses the document, the contract's own identifier included; it is not that
identifier and does not derive it. Every read recomputes both, requires the bytes to be
canonical, decodes through the generated contract, validates the semantics and checks
the columns the row is indexed by against the document itself, so a tampered row raises
`StorageError` rather than returning something that merely parses. A grant is validated
against the exact `PolicySnapshot` it names -- the historical one, not whichever
revision is latest now -- because a grant issued under a policy that has since narrowed
is still the grant that was issued, and re-checking it against a decision made
afterwards would make history unreadable.

An `Approval` is not stored as a document. It is one request and, later, one decision,
written as separate append-only facts and materialised by joining them: all four
decision fields absent is pending, all four present is decided, and 0022's own primary
key is what makes a second decision structurally impossible rather than merely refused.
`record_approval_decision` compares every immutable request fact with the one already
stored before it appends anything, so a decision that disagrees with its own request
inserts nothing at all.

Two limits of accepted v1 are worth stating rather than papering over. It records no
requester identity, so this module stores none. And it gives an `Approval` no field
naming a grant it authorised, so no such edge is stored either -- inventing either
would be this module publishing a record the contract does not have. Who `decided_by`
may be remains the `WaitResolutionPolicy` seam's decision; what is checked here is the
shape of the identifier, the immutable correlation to the request and its wait, and the
deadlines a decision must fall inside.

A missing `omnivia_blob_objects` row for an artifact's content address is an
availability fact, not a reason to refuse or hide the artifact's own metadata:
:func:`read_blob_availability` reports it as unavailable, and every artifact read
still returns the row this module stores regardless of what the blob catalogue
currently holds.

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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ATTEMPT_STATUS_RUNNING,
    RUN_TERMINAL_STATUSES,
    WAIT_STATUS_PENDING,
    ApiError,
    Approval,
    Artifact,
    Attempt,
    BudgetSnapshot,
    CapabilityGrant,
    CleanupReceipt,
    ContractDecodeError,
    ContractSemanticError,
    EvidenceItem,
    ExternalReference,
    PolicySnapshot,
    RunDefinitionRef,
    RunStep,
    RuntimeEvent,
    Wait,
    to_canonical_json,
    validate_approval,
    validate_budget_snapshot,
    validate_budget_snapshot_progression,
    validate_capability_grant,
    validate_policy_snapshot,
    validate_policy_snapshot_progression,
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

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

#: The columns of one stored snapshot row, in the order the row helpers read them. Both
#: 0021 tables have the same shape but not the same identifier name, so the identifier
#: column is the only part that varies.
_POLICY_SNAPSHOT_COLUMNS: Final = (
    "policy_snapshot_id, run_id, revision, pinned_at_us, snapshot_json, "
    "snapshot_digest, snapshot_byte_length"
)
_BUDGET_SNAPSHOT_COLUMNS: Final = (
    "budget_snapshot_id, run_id, revision, pinned_at_us, snapshot_json, "
    "snapshot_digest, snapshot_byte_length"
)

_CAPABILITY_GRANT_COLUMNS: Final = (
    "capability_grant_id, run_id, policy_snapshot_id, granted_at_us, grant_json, "
    "grant_digest, grant_byte_length"
)

#: One `Approval`, joined from the request 0022 stores and the decision and comment it
#: may later receive. `LEFT JOIN` twice rather than three reads, because pending and
#: decided are the same record read at two instants and one query says so.
_APPROVAL_COLUMNS: Final = (
    "a.approval_id, a.run_id, a.wait_id, a.requested_at_us, a.approver_role, "
    "a.assigned_to, a.escalated_to, a.expires_at_us, d.decision, d.decided_at_us, "
    "d.decided_by, d.audit_ref, c.comment"
)
_APPROVAL_SOURCE: Final = (
    "omnivia_runtime_approvals a "
    "LEFT JOIN omnivia_runtime_approval_decisions d "
    "ON d.workspace_id = a.workspace_id AND d.approval_id = a.approval_id "
    "LEFT JOIN omnivia_runtime_approval_comments c "
    "ON c.workspace_id = a.workspace_id AND c.approval_id = a.approval_id"
)


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


def _verified_canonical_document(
    text: object, digest: object, byte_length: object, label: str
) -> dict[str, Any]:
    """One stored document that must be canonical bytes, not merely equivalent JSON.

    A snapshot is content-addressed, so the bytes are the record: a row that decodes to
    the right value out of a re-spaced or re-ordered spelling has a digest nobody else
    can reproduce, which is the same defect as a wrong digest arriving one step later.
    """
    decoded = _verified_document(text, digest, byte_length, label)
    if to_canonical_json(decoded) != str(text):
        raise StorageError(f"a stored {label} is not canonical JSON")
    return decoded


def _instant_us(value: str) -> int:
    """One validated RFC 3339 UTC timestamp as the microsecond column it is indexed by.

    Only ever called on a timestamp the contract validators have already parsed, on both
    the write and the read path, so the spelling is known good by the time it arrives.
    """
    return (datetime.fromisoformat(value) - _EPOCH) // timedelta(microseconds=1)


def _canonical_instant(value: str | None) -> str | None:
    """One timestamp respelled the way a stored microsecond column reads back.

    A record materialised from columns stores the instant, not the spelling, so
    `...:40.000Z` and `...:40Z` are one fact written two ways. Comparing the spellings
    would refuse a decision that restates its own request exactly, in a spelling this
    module itself never emits.
    """
    return None if value is None else _timestamp(_instant_us(value))


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
class StoredPolicySnapshot:
    """One accepted `PolicySnapshot` and the address of the bytes it was stored as.

    `content_address` is the `sha256:` digest of the complete canonical wire document,
    and `content_length_bytes` is that document's exact length. Both are properties of
    the storage, not of the accepted contract, which is why they live here rather than
    widening the public wire schema with two fields nobody publishes.
    """

    snapshot: PolicySnapshot
    content_address: str
    content_length_bytes: int


@dataclass(frozen=True, slots=True)
class StoredBudgetSnapshot:
    """One accepted `BudgetSnapshot` and the address of the bytes it was stored as."""

    snapshot: BudgetSnapshot
    content_address: str
    content_length_bytes: int


@dataclass(frozen=True, slots=True)
class StoredCapabilityGrant:
    """One issued `CapabilityGrant` and the address of the bytes it was stored as.

    The same shape, and for the same reason, as :class:`StoredPolicySnapshot`: the
    address and the length are properties of the storage rather than of the accepted
    contract, so they live here instead of widening the public wire schema.
    """

    grant: CapabilityGrant
    content_address: str
    content_length_bytes: int


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """What RT-102, RT-103, RT-202 and RT-203 can honestly report about one canonical run.

    Deliberately not a `Run`. The contract's aggregate also requires the effect family,
    which has no store yet, and filling it in to satisfy a type would report data nobody
    recorded. `approvals` and `capability_grants` are what 0022 records and are empty
    tuples for a run that has neither, which is an answer rather than a gap.
    `policy` and `budget` are the run's latest
    stored revisions and are optional for the same reason: a run admitted before
    migration 0021, or one whose decisions were never recorded, has neither, and `None`
    says so rather than inventing an unbounded default. `status`, `updated_at` and
    `finished_at` are read from the event stream, which states the run status in force
    at every entry, so they are derived from stored facts rather than maintained beside
    them.
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
    artifacts: tuple[Artifact, ...]
    evidence: tuple[EvidenceItem, ...]
    cleanup_receipts: tuple[CleanupReceipt, ...]
    approvals: tuple[Approval, ...]
    capability_grants: tuple[CapabilityGrant, ...]
    correlations: tuple[ExternalReference, ...]
    policy: PolicySnapshot | None = None
    budget: BudgetSnapshot | None = None


# --- writes -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeWriter:
    """Every runtime write, issued into a transaction that is already open.

    Not constructible usefully on its own: :func:`runtime_writer` and
    :func:`transaction_local_writer` are what hand one out, and neither issues a
    statement outside a fenced transaction -- the first opens one, and the second is
    for a caller that already holds one. That is the whole point of the type: the
    statements live somewhere a composition can reach them without any of them
    opening a second `BEGIN IMMEDIATE`.

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
        # RT-105's derived active-Run summary is part of this same transaction.
        # Import locally to keep the projection module independent of this repository
        # module while still giving RuntimeWriter the only incremental write seam.
        from omnivia_core_runtime.storage.projections.runtime_run_summary import (
            apply_runtime_run_summary_event,
        )

        apply_runtime_run_summary_event(
            self.connection,
            workspace_id=self.workspace_id,
            run_id=admission.run_id,
            runtime_event_id=admission.runtime_event_id,
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
        sequence = (
            read_run_sequence(
                self.connection, workspace_id=self.workspace_id, run_id=run_id
            )
            + 1
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
        from omnivia_core_runtime.storage.projections.runtime_run_summary import (
            apply_runtime_run_summary_event,
        )

        apply_runtime_run_summary_event(
            self.connection,
            workspace_id=self.workspace_id,
            run_id=run_id,
            runtime_event_id=runtime_event_id,
        )
        return sequence

    def append_artifact(
        self,
        *,
        artifact_id: str,
        run_id: str,
        artifact_kind: str,
        media_type: str,
        content_checksum: str,
        content_length_bytes: int,
        produced_at_us: int,
        run_step_id: str | None = None,
    ) -> None:
        """Record one content-addressed output a run produced.

        Storage only, and independent of any physical blob: the content address and
        length are recorded whether or not `omnivia_blob_objects` currently holds a
        matching row. The migration's guard refuses only an outright contradiction --
        a verified blob of this same address whose length disagrees.
        """
        self.connection.execute(
            "INSERT INTO omnivia_runtime_artifacts "
            "(workspace_id, artifact_id, run_id, run_step_id, artifact_kind, "
            "media_type, content_checksum, content_length_bytes, produced_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                artifact_id,
                run_id,
                run_step_id,
                artifact_kind,
                media_type,
                content_checksum,
                content_length_bytes,
                produced_at_us,
            ),
        )

    def append_evidence_item(
        self,
        *,
        evidence_item_id: str,
        run_id: str,
        evidence_kind: str,
        source: ExternalReference,
        content_checksum: str,
        captured_at_us: int,
        authoritative: bool,
        retained: bool,
        run_step_id: str | None = None,
        artifact_id: str | None = None,
    ) -> None:
        """Record one piece of evidence a run captured, exactly once.

        `source` is stored as its three fields rather than a nested document, so it
        round-trips through the same columns a correlation's own `workspace_id`,
        `source_kind` and `source_id` are: nothing here re-encodes it as JSON. The
        migration refuses an authoritative claim from anything but the runtime's own
        record, and refuses an `artifact_id` that names an artifact of another run or
        a different checksum.
        """
        self.connection.execute(
            "INSERT INTO omnivia_runtime_evidence "
            "(workspace_id, evidence_item_id, run_id, run_step_id, evidence_kind, "
            "source_kind, source_id, source_workspace_id, content_checksum, "
            "artifact_id, captured_at_us, authoritative, retained) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                evidence_item_id,
                run_id,
                run_step_id,
                evidence_kind,
                source.source_kind,
                source.source_id,
                source.workspace_id,
                content_checksum,
                artifact_id,
                captured_at_us,
                1 if authoritative else 0,
                1 if retained else 0,
            ),
        )

    def append_cleanup_receipt(
        self,
        *,
        cleanup_receipt_id: str,
        run_id: str,
        resource_kind: str,
        outcome: str,
        reason: str,
        performed_at_us: int,
        audit_reference: str,
    ) -> None:
        """Record that cleanup was attempted for one run, and what it achieved.

        Written for the attempt, not for the success: a `failed` outcome is as
        storable as `released` or `not_required`, so a failed release is a row
        rather than silence indistinguishable from cleanup that never ran.
        """
        self.connection.execute(
            "INSERT INTO omnivia_runtime_cleanup_receipts "
            "(workspace_id, cleanup_receipt_id, run_id, resource_kind, outcome, "
            "reason, performed_at_us, audit_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                cleanup_receipt_id,
                run_id,
                resource_kind,
                outcome,
                reason,
                performed_at_us,
                audit_reference,
            ),
        )

    def append_policy_snapshot(self, snapshot: PolicySnapshot) -> None:
        """Record one accepted policy decision as the immutable successor of the last.

        Validated before a statement is issued, and validated against the run's latest
        stored revision when it already has one, so a widening leaves the previous
        decision exactly as it was and inserts nothing at all. The refusal is the
        contract's own :class:`ContractSemanticError`: the caller handed over a record
        the accepted rules refuse, which is not a database this module failed to read.

        The workspace checked against is the writer's, never the one inside the record.
        The run is the snapshot's own claim -- there is no second run to check it
        against here -- and the composite foreign key is what refuses a run this
        workspace does not hold.
        """
        validate_policy_snapshot(
            snapshot, run_id=snapshot.run_id, workspace_id=self.workspace_id
        )
        previous = _latest_policy_snapshot(
            self.connection, workspace_id=self.workspace_id, run_id=snapshot.run_id
        )
        if previous is not None:
            validate_policy_snapshot_progression(previous.snapshot, snapshot)
        document, digest, byte_length = _stored_document(snapshot.to_wire())
        self.connection.execute(
            "INSERT INTO omnivia_runtime_policy_snapshots "
            "(workspace_id, policy_snapshot_id, run_id, revision, pinned_at_us, "
            "snapshot_json, snapshot_digest, snapshot_byte_length) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                snapshot.policy_snapshot_id,
                snapshot.run_id,
                snapshot.revision,
                _instant_us(snapshot.pinned_at),
                document,
                digest,
                byte_length,
            ),
        )

    def append_budget_snapshot(self, snapshot: BudgetSnapshot) -> None:
        """Record one accepted budget decision as the immutable successor of the last.

        The same rule policy follows, in the direction budget monotonicity runs:
        ceilings may narrow and never widen, consumption never decreases, and a revision
        that breaks either leaves the previous decision intact and stores nothing.
        """
        validate_budget_snapshot(
            snapshot, run_id=snapshot.run_id, workspace_id=self.workspace_id
        )
        previous = _latest_budget_snapshot(
            self.connection, workspace_id=self.workspace_id, run_id=snapshot.run_id
        )
        if previous is not None:
            validate_budget_snapshot_progression(previous.snapshot, snapshot)
        document, digest, byte_length = _stored_document(snapshot.to_wire())
        self.connection.execute(
            "INSERT INTO omnivia_runtime_budget_snapshots "
            "(workspace_id, budget_snapshot_id, run_id, revision, pinned_at_us, "
            "snapshot_json, snapshot_digest, snapshot_byte_length) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                snapshot.budget_snapshot_id,
                snapshot.run_id,
                snapshot.revision,
                _instant_us(snapshot.pinned_at),
                document,
                digest,
                byte_length,
            ),
        )

    def request_approval(self, approval: Approval) -> None:
        """Record that one approval was asked for, and the comment it already carries.

        The request half only. A decided `Approval` is not a request that happens to
        know its own answer: :meth:`record_approval_decision` is what records one, and
        it is a separate append precisely so a second decision has nowhere to live.

        The workspace checked against is the writer's, never the one inside the record.
        Whether the wait is an approval wait of this same run, and whether it is still
        pending, are 0022's guards -- this module does not restate them in Python where
        a second copy could disagree.
        """
        validate_approval(
            approval, run_id=approval.run_id, workspace_id=self.workspace_id
        )
        if approval.decision is not None:
            raise StorageError(
                "a requested approval carries no decision; record_approval_decision is "
                "what records one"
            )
        self.connection.execute(
            "INSERT INTO omnivia_runtime_approvals "
            "(workspace_id, approval_id, run_id, wait_id, requested_at_us, "
            "approver_role, assigned_to, escalated_to, expires_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                approval.approval_id,
                approval.run_id,
                approval.wait_id,
                _instant_us(approval.requested_at),
                approval.approver_role,
                approval.assigned_to,
                approval.escalated_to,
                None if approval.expires_at is None else _instant_us(approval.expires_at),
            ),
        )
        self._append_approval_comment(approval)

    def record_approval_decision(self, approval: Approval) -> None:
        """Record the one decision an already-requested approval ever receives.

        Takes the complete decided `Approval`, not a decision in isolation, because the
        accepted record is the request/decision pair and a caller holding half of it can
        state which half it thinks it has. Every immutable request fact is compared with
        the one already stored before a statement is issued, so a decision that
        disagrees with its own request -- a different wait, role, assignee, deadline or
        instant -- inserts nothing at all rather than appending a decision onto a
        request nobody made in those terms.

        Only the facts that are genuinely new are appended: the decision, and a comment
        the request did not already carry. A comment already recorded is never replaced,
        and a second decision is refused here and then refused again by 0022's primary
        key, which is what makes it structurally impossible rather than merely policed.
        """
        validate_approval(
            approval, run_id=approval.run_id, workspace_id=self.workspace_id
        )
        decision = approval.decision
        decided_at = approval.decided_at
        decided_by = approval.decided_by
        audit_reference = approval.audit_reference
        if (
            decision is None
            or decided_at is None
            or decided_by is None
            or audit_reference is None
        ):
            raise StorageError(
                "a recorded approval decision states all of decision, decided_at, "
                "decided_by and audit_reference; a partial one is not a decision"
            )
        stored = read_approval(
            self.connection,
            workspace_id=self.workspace_id,
            approval_id=approval.approval_id,
        )
        if stored is None:
            raise StorageError(
                f"approval {approval.approval_id!r} was never requested in this workspace"
            )
        if stored.decision is not None:
            raise StorageError(
                f"approval {approval.approval_id!r} is already decided; a decision is "
                "recorded once and never re-decided"
            )
        # Comparing the whole record with its decision stripped and the stored comment
        # substituted checks every immutable request fact at once -- including any the
        # contract gains later -- rather than a hand-written field list that could fall
        # behind the record it claims to compare.
        requested = replace(
            approval,
            requested_at=_timestamp(_instant_us(approval.requested_at)),
            expires_at=_canonical_instant(approval.expires_at),
            decision=None,
            decided_at=None,
            decided_by=None,
            audit_reference=None,
            comment=stored.comment,
        )
        if requested != stored:
            raise StorageError(
                f"the decision offered for approval {approval.approval_id!r} disagrees "
                "with the request already recorded for it"
            )
        if stored.comment is not None and approval.comment != stored.comment:
            raise StorageError(
                f"approval {approval.approval_id!r} already carries a comment; a comment "
                "is recorded once and never replaced"
            )
        if stored.comment is None:
            self._append_approval_comment(approval)
        self.connection.execute(
            "INSERT INTO omnivia_runtime_approval_decisions "
            "(workspace_id, approval_id, decision, decided_at_us, decided_by, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                approval.approval_id,
                decision,
                _instant_us(decided_at),
                decided_by,
                audit_reference,
            ),
        )

    def issue_capability_grant(self, grant: CapabilityGrant) -> None:
        """Issue one capability to one run, backed by the policy in force right now.

        *Discovery is not authority*, and neither is a policy the run has already moved
        past. The grant must name the run's latest stored `PolicySnapshot` -- pinning a
        grant to a superseded revision would let a narrowing be walked back by quoting
        the decision it replaced -- and the accepted contract's own
        `validate_capability_grant` is what proves the capability is in that policy's
        `granted_capabilities` rather than only in its `discovered_capabilities`. Both
        answers are settled before a statement is issued, so a refused grant leaves the
        database exactly as it found it.
        """
        policy = _latest_policy_snapshot(
            self.connection, workspace_id=self.workspace_id, run_id=grant.run_id
        )
        if policy is None:
            raise StorageError(
                f"run {grant.run_id!r} has no pinned policy for a capability grant to "
                "be backed by"
            )
        if policy.snapshot.policy_snapshot_id != grant.policy_snapshot_id:
            raise StorageError(
                "a grant names the policy in force when it is issued; run "
                f"{grant.run_id!r} is pinned to "
                f"{policy.snapshot.policy_snapshot_id!r}, not "
                f"{grant.policy_snapshot_id!r}"
            )
        validate_capability_grant(
            grant,
            run_id=grant.run_id,
            workspace_id=self.workspace_id,
            policy=policy.snapshot,
        )
        document, digest, byte_length = _stored_document(grant.to_wire())
        self.connection.execute(
            "INSERT INTO omnivia_runtime_capability_grants "
            "(workspace_id, capability_grant_id, run_id, policy_snapshot_id, "
            "granted_at_us, grant_json, grant_digest, grant_byte_length) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                grant.capability_grant_id,
                grant.run_id,
                grant.policy_snapshot_id,
                _instant_us(grant.granted_at),
                document,
                digest,
                byte_length,
            ),
        )

    def _append_approval_comment(self, approval: Approval) -> None:
        """The one comment fact an approval carries, when it carries one.

        A row of its own rather than a column on either half, because the accepted
        contract lets a *pending* approval carry a comment and lets a decision add one
        later, and a column on either half would need an UPDATE to represent the second
        of those. There is one authoritative copy and 0022's primary key refuses a
        replacement.
        """
        if approval.comment is None:
            return
        self.connection.execute(
            "INSERT INTO omnivia_runtime_approval_comments "
            "(workspace_id, approval_id, comment) VALUES (?, ?, ?)",
            (self.workspace_id, approval.approval_id, approval.comment),
        )

    def _next_sequence(self, query: str, parameters: tuple[object, ...]) -> int:
        row = self.connection.execute(query, parameters).fetchone()
        if row is None:  # pragma: no cover - an aggregate always returns one row
            raise StorageError("a sequence allocation returned no row")
        return int(row[0])


def transaction_local_writer(
    connection: sqlite3.Connection, *, workspace_id: str
) -> RuntimeWriter:
    """The runtime writes, for a caller that already holds a fenced transaction.

    The narrow companion to :func:`runtime_writer`, and the only other way to obtain
    a writer. `runtime_writer` opens the fence itself, which is right for a caller
    that has no transaction; a caller that already runs inside one -- RT-104's
    command seam, which settles a mutation's audit, claim and outcome around this --
    cannot use it, because `BEGIN IMMEDIATE` does not nest.

    This weakens no fencing. It opens no transaction and validates no authority, so
    everything it issues is issued into whatever transaction the caller opened and is
    covered by that transaction's entry and pre-commit validation. Calling it outside
    one is not a way past the guard: the persisted triggers refuse an unguarded
    insert on every runtime table regardless of which Python object issued it.
    """
    return RuntimeWriter(connection, workspace_id)


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
        yield transaction_local_writer(connection, workspace_id=workspace_id)


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


def append_artifact(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    artifact_id: str,
    run_id: str,
    artifact_kind: str,
    media_type: str,
    content_checksum: str,
    content_length_bytes: int,
    produced_at_us: int,
    run_step_id: str | None = None,
) -> None:
    """Record one content-addressed output a run produced, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.append_artifact(
            artifact_id=artifact_id,
            run_id=run_id,
            run_step_id=run_step_id,
            artifact_kind=artifact_kind,
            media_type=media_type,
            content_checksum=content_checksum,
            content_length_bytes=content_length_bytes,
            produced_at_us=produced_at_us,
        )


def append_evidence_item(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    evidence_item_id: str,
    run_id: str,
    evidence_kind: str,
    source: ExternalReference,
    content_checksum: str,
    captured_at_us: int,
    authoritative: bool,
    retained: bool,
    run_step_id: str | None = None,
    artifact_id: str | None = None,
) -> None:
    """Record one piece of evidence a run captured, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.append_evidence_item(
            evidence_item_id=evidence_item_id,
            run_id=run_id,
            run_step_id=run_step_id,
            evidence_kind=evidence_kind,
            source=source,
            content_checksum=content_checksum,
            artifact_id=artifact_id,
            captured_at_us=captured_at_us,
            authoritative=authoritative,
            retained=retained,
        )


def append_cleanup_receipt(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    cleanup_receipt_id: str,
    run_id: str,
    resource_kind: str,
    outcome: str,
    reason: str,
    performed_at_us: int,
    audit_reference: str,
) -> None:
    """Record that cleanup was attempted for one run, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.append_cleanup_receipt(
            cleanup_receipt_id=cleanup_receipt_id,
            run_id=run_id,
            resource_kind=resource_kind,
            outcome=outcome,
            reason=reason,
            performed_at_us=performed_at_us,
            audit_reference=audit_reference,
        )


def append_policy_snapshot(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    snapshot: PolicySnapshot,
) -> None:
    """Record one accepted policy decision, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.append_policy_snapshot(snapshot)


def append_budget_snapshot(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    snapshot: BudgetSnapshot,
) -> None:
    """Record one accepted budget decision, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.append_budget_snapshot(snapshot)


def request_approval(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    approval: Approval,
) -> None:
    """Record one approval request, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.request_approval(approval)


def record_approval_decision(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    approval: Approval,
) -> None:
    """Record the one decision an approval receives, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.record_approval_decision(approval)


def issue_capability_grant(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    grant: CapabilityGrant,
) -> None:
    """Issue one policy-backed capability grant, in its own fenced transaction."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        writer.issue_capability_grant(grant)


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


def read_run_sequence(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> int:
    """The sequence this run's stream is at, or `-1` when it holds no event yet.

    The aggregate version an optimistic command states its expectation against, and
    the number :meth:`RuntimeWriter.append_run_event` allocates its successor from --
    one statement serving both, because a check that read the stream differently from
    the allocation could pass on a number the trigger would then reject.

    `-1` is the empty stream, which is the same value the migration's own contiguity
    trigger starts from. A run always opens its stream at sequence zero in the same
    statement pair that records the run, so `-1` here means this workspace holds no
    such run rather than a run whose history is missing.
    """
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), -1) FROM omnivia_runtime_events "
        "WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:  # pragma: no cover - an aggregate always returns one row
        raise StorageError("a run sequence read returned no row")
    return int(row[0])


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


def read_run_artifacts(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[Artifact, ...]:
    """Every artifact this run produced, in production order then identifier order."""
    rows = connection.execute(
        "SELECT artifact_id, run_step_id, artifact_kind, media_type, "
        "content_checksum, content_length_bytes, produced_at_us "
        "FROM omnivia_runtime_artifacts "
        "WHERE workspace_id = ? AND run_id = ? "
        "ORDER BY produced_at_us, artifact_id",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(
        Artifact(
            workspace_id=workspace_id,
            artifact_id=str(row[0]),
            run_id=run_id,
            run_step_id=None if row[1] is None else str(row[1]),
            artifact_kind=str(row[2]),
            media_type=str(row[3]),
            content_checksum=str(row[4]),
            content_length_bytes=int(row[5]),
            produced_at=_timestamp(int(row[6])),
        )
        for row in rows
    )


def read_artifact(
    connection: sqlite3.Connection, *, workspace_id: str, artifact_id: str
) -> Artifact | None:
    """One artifact by identifier, or `None` when this workspace holds no such artifact."""
    row = connection.execute(
        "SELECT run_id, run_step_id, artifact_kind, media_type, content_checksum, "
        "content_length_bytes, produced_at_us FROM omnivia_runtime_artifacts "
        "WHERE workspace_id = ? AND artifact_id = ?",
        (workspace_id, artifact_id),
    ).fetchone()
    if row is None:
        return None
    return Artifact(
        workspace_id=workspace_id,
        artifact_id=artifact_id,
        run_id=str(row[0]),
        run_step_id=None if row[1] is None else str(row[1]),
        artifact_kind=str(row[2]),
        media_type=str(row[3]),
        content_checksum=str(row[4]),
        content_length_bytes=int(row[5]),
        produced_at=_timestamp(int(row[6])),
    )


def read_run_evidence(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[EvidenceItem, ...]:
    """Every evidence item this run captured, in capture order then identifier order."""
    rows = connection.execute(
        "SELECT evidence_item_id, run_step_id, evidence_kind, source_kind, "
        "source_id, source_workspace_id, content_checksum, artifact_id, "
        "captured_at_us, authoritative, retained FROM omnivia_runtime_evidence "
        "WHERE workspace_id = ? AND run_id = ? "
        "ORDER BY captured_at_us, evidence_item_id",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(_evidence_item_from_row(run_id, row) for row in rows)


def read_evidence_item(
    connection: sqlite3.Connection, *, workspace_id: str, evidence_item_id: str
) -> EvidenceItem | None:
    """One evidence item by identifier, or `None` when this workspace holds no such item."""
    row = connection.execute(
        "SELECT run_id, run_step_id, evidence_kind, source_kind, source_id, "
        "source_workspace_id, content_checksum, artifact_id, captured_at_us, "
        "authoritative, retained FROM omnivia_runtime_evidence "
        "WHERE workspace_id = ? AND evidence_item_id = ?",
        (workspace_id, evidence_item_id),
    ).fetchone()
    if row is None:
        return None
    return _evidence_item_from_row(str(row[0]), (evidence_item_id, *row[1:]))


def _evidence_item_from_row(run_id: str, row: tuple[object, ...]) -> EvidenceItem:
    captured_at_us = row[8]
    if not isinstance(captured_at_us, int):
        raise StorageError("a stored evidence capture time is not an integer")
    return EvidenceItem(
        workspace_id=str(row[5]),
        evidence_item_id=str(row[0]),
        run_id=run_id,
        run_step_id=None if row[1] is None else str(row[1]),
        evidence_kind=str(row[2]),
        source=ExternalReference(
            source_kind=str(row[3]),
            source_id=str(row[4]),
            workspace_id=str(row[5]),
        ),
        content_checksum=str(row[6]),
        artifact_id=None if row[7] is None else str(row[7]),
        captured_at=_timestamp(captured_at_us),
        authoritative=bool(row[9]),
        retained=bool(row[10]),
    )


def read_run_cleanup_receipts(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[CleanupReceipt, ...]:
    """Every cleanup receipt of this run, in performance order then identifier order."""
    rows = connection.execute(
        "SELECT cleanup_receipt_id, resource_kind, outcome, reason, "
        "performed_at_us, audit_ref FROM omnivia_runtime_cleanup_receipts "
        "WHERE workspace_id = ? AND run_id = ? "
        "ORDER BY performed_at_us, cleanup_receipt_id",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(
        CleanupReceipt(
            workspace_id=workspace_id,
            cleanup_receipt_id=str(row[0]),
            run_id=run_id,
            resource_kind=str(row[1]),
            outcome=str(row[2]),
            reason=str(row[3]),
            performed_at=_timestamp(int(row[4])),
            audit_reference=str(row[5]),
        )
        for row in rows
    )


def read_cleanup_receipt(
    connection: sqlite3.Connection, *, workspace_id: str, cleanup_receipt_id: str
) -> CleanupReceipt | None:
    """One cleanup receipt by identifier, or `None` when this workspace holds no such receipt."""
    row = connection.execute(
        "SELECT run_id, resource_kind, outcome, reason, performed_at_us, audit_ref "
        "FROM omnivia_runtime_cleanup_receipts "
        "WHERE workspace_id = ? AND cleanup_receipt_id = ?",
        (workspace_id, cleanup_receipt_id),
    ).fetchone()
    if row is None:
        return None
    return CleanupReceipt(
        workspace_id=workspace_id,
        cleanup_receipt_id=cleanup_receipt_id,
        run_id=str(row[0]),
        resource_kind=str(row[1]),
        outcome=str(row[2]),
        reason=str(row[3]),
        performed_at=_timestamp(int(row[4])),
        audit_reference=str(row[5]),
    )


def _stored_policy_snapshot(
    row: tuple[Any, ...], *, workspace_id: str
) -> StoredPolicySnapshot:
    """One policy snapshot row, proven before any of it is believed.

    Ordered digest and length, canonical form, decode, semantics, then the columns the
    row is indexed by against the document itself. A row whose selectors disagree with
    its own bytes is not this snapshot however well it parses, so it leaves here as a
    `StorageError` rather than as data a caller could act on.
    """
    document = _verified_canonical_document(row[4], row[5], row[6], "policy snapshot")
    try:
        snapshot = PolicySnapshot.from_wire(document)
    except ContractDecodeError as error:
        raise StorageError(
            "a stored policy snapshot is not a valid PolicySnapshot"
        ) from error
    try:
        validate_policy_snapshot(
            snapshot, run_id=snapshot.run_id, workspace_id=workspace_id
        )
    except ContractSemanticError as error:
        raise StorageError(
            "a stored policy snapshot is not a valid PolicySnapshot"
        ) from error
    if (
        snapshot.policy_snapshot_id != str(row[0])
        or snapshot.run_id != str(row[1])
        or snapshot.revision != int(row[2])
        or _instant_us(snapshot.pinned_at) != int(row[3])
    ):
        raise StorageError(
            "a stored policy snapshot disagrees with the columns it is indexed by"
        )
    return StoredPolicySnapshot(
        snapshot=snapshot,
        content_address=str(row[5]),
        content_length_bytes=int(row[6]),
    )


def _stored_budget_snapshot(
    row: tuple[Any, ...], *, workspace_id: str
) -> StoredBudgetSnapshot:
    """One budget snapshot row, proven the same way a policy row is."""
    document = _verified_canonical_document(row[4], row[5], row[6], "budget snapshot")
    try:
        snapshot = BudgetSnapshot.from_wire(document)
    except ContractDecodeError as error:
        raise StorageError(
            "a stored budget snapshot is not a valid BudgetSnapshot"
        ) from error
    try:
        validate_budget_snapshot(
            snapshot, run_id=snapshot.run_id, workspace_id=workspace_id
        )
    except ContractSemanticError as error:
        raise StorageError(
            "a stored budget snapshot is not a valid BudgetSnapshot"
        ) from error
    if (
        snapshot.budget_snapshot_id != str(row[0])
        or snapshot.run_id != str(row[1])
        or snapshot.revision != int(row[2])
        or _instant_us(snapshot.pinned_at) != int(row[3])
    ):
        raise StorageError(
            "a stored budget snapshot disagrees with the columns it is indexed by"
        )
    return StoredBudgetSnapshot(
        snapshot=snapshot,
        content_address=str(row[5]),
        content_length_bytes=int(row[6]),
    )


def read_policy_snapshot(
    connection: sqlite3.Connection, *, workspace_id: str, policy_snapshot_id: str
) -> StoredPolicySnapshot | None:
    """One policy snapshot by identifier, or `None` when this workspace holds no such one."""
    row = connection.execute(
        f"SELECT {_POLICY_SNAPSHOT_COLUMNS} FROM omnivia_runtime_policy_snapshots "
        "WHERE workspace_id = ? AND policy_snapshot_id = ?",
        (workspace_id, policy_snapshot_id),
    ).fetchone()
    if row is None:
        return None
    return _stored_policy_snapshot(row, workspace_id=workspace_id)


def read_budget_snapshot(
    connection: sqlite3.Connection, *, workspace_id: str, budget_snapshot_id: str
) -> StoredBudgetSnapshot | None:
    """One budget snapshot by identifier, or `None` when this workspace holds no such one."""
    row = connection.execute(
        f"SELECT {_BUDGET_SNAPSHOT_COLUMNS} FROM omnivia_runtime_budget_snapshots "
        "WHERE workspace_id = ? AND budget_snapshot_id = ?",
        (workspace_id, budget_snapshot_id),
    ).fetchone()
    if row is None:
        return None
    return _stored_budget_snapshot(row, workspace_id=workspace_id)


def read_run_policy_snapshots(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[StoredPolicySnapshot, ...]:
    """Every policy decision this run was pinned to, oldest revision first.

    Revision order is the decision order, and it is a total order no two entries of one
    run can share, which the pinned instant is not: two revisions pinned in the same
    millisecond would otherwise come back in whatever order the page arrived in.
    """
    rows = connection.execute(
        f"SELECT {_POLICY_SNAPSHOT_COLUMNS} FROM omnivia_runtime_policy_snapshots "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY revision",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(_stored_policy_snapshot(row, workspace_id=workspace_id) for row in rows)


def read_run_budget_snapshots(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[StoredBudgetSnapshot, ...]:
    """Every budget decision this run was pinned to, oldest revision first."""
    rows = connection.execute(
        f"SELECT {_BUDGET_SNAPSHOT_COLUMNS} FROM omnivia_runtime_budget_snapshots "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY revision",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(_stored_budget_snapshot(row, workspace_id=workspace_id) for row in rows)


def _latest_policy_snapshot(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> StoredPolicySnapshot | None:
    row = connection.execute(
        f"SELECT {_POLICY_SNAPSHOT_COLUMNS} FROM omnivia_runtime_policy_snapshots "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY revision DESC LIMIT 1",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        return None
    return _stored_policy_snapshot(row, workspace_id=workspace_id)


def _latest_budget_snapshot(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> StoredBudgetSnapshot | None:
    row = connection.execute(
        f"SELECT {_BUDGET_SNAPSHOT_COLUMNS} FROM omnivia_runtime_budget_snapshots "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY revision DESC LIMIT 1",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        return None
    return _stored_budget_snapshot(row, workspace_id=workspace_id)


def _approval_from_row(workspace_id: str, row: tuple[Any, ...]) -> Approval:
    """One `Approval`, joined from the request and whatever has been recorded since.

    Pending and decided are the same record read at two instants: all four decision
    fields absent is pending, all four present is decided, and 0022 makes the partial
    state between them unrepresentable by keying the decision on the approval alone.
    """
    return Approval(
        workspace_id=workspace_id,
        approval_id=str(row[0]),
        run_id=str(row[1]),
        wait_id=str(row[2]),
        requested_at=_timestamp(int(row[3])),
        approver_role=str(row[4]),
        assigned_to=None if row[5] is None else str(row[5]),
        escalated_to=None if row[6] is None else str(row[6]),
        expires_at=None if row[7] is None else _timestamp(int(row[7])),
        decision=None if row[8] is None else str(row[8]),
        decided_at=None if row[9] is None else _timestamp(int(row[9])),
        decided_by=None if row[10] is None else str(row[10]),
        audit_reference=None if row[11] is None else str(row[11]),
        comment=None if row[12] is None else str(row[12]),
    )


def read_approval(
    connection: sqlite3.Connection, *, workspace_id: str, approval_id: str
) -> Approval | None:
    """One approval by identifier, or `None` when this workspace holds no such request."""
    row = connection.execute(
        f"SELECT {_APPROVAL_COLUMNS} FROM {_APPROVAL_SOURCE} "
        "WHERE a.workspace_id = ? AND a.approval_id = ?",
        (workspace_id, approval_id),
    ).fetchone()
    if row is None:
        return None
    return _approval_from_row(workspace_id, row)


def read_run_approvals(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[Approval, ...]:
    """Every approval this run asked for, in request order then identifier order."""
    rows = connection.execute(
        f"SELECT {_APPROVAL_COLUMNS} FROM {_APPROVAL_SOURCE} "
        "WHERE a.workspace_id = ? AND a.run_id = ? "
        "ORDER BY a.requested_at_us, a.approval_id",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(_approval_from_row(workspace_id, row) for row in rows)


def _stored_capability_grant(
    connection: sqlite3.Connection, row: tuple[Any, ...], *, workspace_id: str
) -> StoredCapabilityGrant:
    """One capability grant row, proven before any of it is believed.

    The same order a snapshot row is proven in -- digest and length, canonical form,
    decode, then the columns the row is indexed by against the document itself -- with
    the semantics checked against the `PolicySnapshot` the grant *names* rather than
    whichever revision is latest now. A grant issued under a policy that has since
    narrowed is still the grant that was issued; re-checking it against a decision made
    afterwards would make a legal history unreadable, which is the opposite of what
    reading it is for.
    """
    document = _verified_canonical_document(row[4], row[5], row[6], "capability grant")
    try:
        grant = CapabilityGrant.from_wire(document)
    except ContractDecodeError as error:
        raise StorageError(
            "a stored capability grant is not a valid CapabilityGrant"
        ) from error
    if (
        grant.capability_grant_id != str(row[0])
        or grant.run_id != str(row[1])
        or grant.policy_snapshot_id != str(row[2])
        or _instant_us(grant.granted_at) != int(row[3])
    ):
        raise StorageError(
            "a stored capability grant disagrees with the columns it is indexed by"
        )
    policy = read_policy_snapshot(
        connection, workspace_id=workspace_id, policy_snapshot_id=grant.policy_snapshot_id
    )
    if policy is None:
        raise StorageError(
            "a stored capability grant names a policy snapshot this workspace does not hold"
        )
    try:
        validate_capability_grant(
            grant,
            run_id=grant.run_id,
            workspace_id=workspace_id,
            policy=policy.snapshot,
        )
    except ContractSemanticError as error:
        raise StorageError(
            "a stored capability grant is not a valid CapabilityGrant"
        ) from error
    return StoredCapabilityGrant(
        grant=grant,
        content_address=str(row[5]),
        content_length_bytes=int(row[6]),
    )


def read_capability_grant(
    connection: sqlite3.Connection, *, workspace_id: str, capability_grant_id: str
) -> StoredCapabilityGrant | None:
    """One capability grant by identifier, or `None` when this workspace holds no such one."""
    row = connection.execute(
        f"SELECT {_CAPABILITY_GRANT_COLUMNS} FROM omnivia_runtime_capability_grants "
        "WHERE workspace_id = ? AND capability_grant_id = ?",
        (workspace_id, capability_grant_id),
    ).fetchone()
    if row is None:
        return None
    return _stored_capability_grant(connection, row, workspace_id=workspace_id)


def read_run_capability_grants(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[StoredCapabilityGrant, ...]:
    """Every capability issued to this run, in issue order then identifier order."""
    rows = connection.execute(
        f"SELECT {_CAPABILITY_GRANT_COLUMNS} FROM omnivia_runtime_capability_grants "
        "WHERE workspace_id = ? AND run_id = ? "
        "ORDER BY granted_at_us, capability_grant_id",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(
        _stored_capability_grant(connection, row, workspace_id=workspace_id)
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class BlobAvailability:
    """Whether `omnivia_blob_objects` currently holds the bytes an artifact addresses.

    An availability fact, never a permission or a corruption trigger: a `False` here
    says only that this workspace's blob catalogue has no verified row for this exact
    `(workspace_id, content_checksum)` pair right now. It fabricates no bytes and
    raises for nothing this repository can already tell apart from a database error --
    a missing blob is an ordinary, expected state for a Runtime record to outlive.
    """

    available: bool
    content_length_bytes: int | None = None


def read_blob_availability(
    connection: sqlite3.Connection, *, workspace_id: str, content_checksum: str
) -> BlobAvailability:
    """Report whether the blob catalogue currently holds this content address.

    Deliberately narrow: this is a read of the existing `omnivia_blob_objects`
    catalogue (migration 0008), which records verified identity rather than physical
    storage. It is not a second blob store, carries no filesystem path or URL, and an
    absent row is reported as unavailable rather than raised as an error -- reading an
    artifact's own metadata never depends on this answer.
    """
    row = connection.execute(
        "SELECT content_length_bytes FROM omnivia_blob_objects "
        "WHERE workspace_id = ? AND content_digest = ?",
        (workspace_id, content_checksum),
    ).fetchone()
    if row is None:
        return BlobAvailability(available=False)
    return BlobAvailability(available=True, content_length_bytes=int(row[0]))


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
    policy = _latest_policy_snapshot(
        connection, workspace_id=workspace_id, run_id=run_id
    )
    budget = _latest_budget_snapshot(
        connection, workspace_id=workspace_id, run_id=run_id
    )
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
        artifacts=read_run_artifacts(
            connection, workspace_id=workspace_id, run_id=run_id
        ),
        evidence=read_run_evidence(connection, workspace_id=workspace_id, run_id=run_id),
        cleanup_receipts=read_run_cleanup_receipts(
            connection, workspace_id=workspace_id, run_id=run_id
        ),
        approvals=read_run_approvals(
            connection, workspace_id=workspace_id, run_id=run_id
        ),
        capability_grants=tuple(
            issued.grant
            for issued in read_run_capability_grants(
                connection, workspace_id=workspace_id, run_id=run_id
            )
        ),
        correlations=(
            ExternalReference(
                source_kind=_JOB_SOURCE_KIND,
                source_id=job_id,
                workspace_id=workspace_id,
            ),
        ),
        policy=None if policy is None else policy.snapshot,
        budget=None if budget is None else budget.snapshot,
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
    "BlobAvailability",
    "RunAdmission",
    "RunSnapshot",
    "RuntimeWriter",
    "StoredBudgetSnapshot",
    "StoredCapabilityGrant",
    "StoredPolicySnapshot",
    "admit_run",
    "append_artifact",
    "append_budget_snapshot",
    "append_cleanup_receipt",
    "append_evidence_item",
    "append_policy_snapshot",
    "append_run_event",
    "append_run_step",
    "close_wait",
    "finish_attempt",
    "issue_capability_grant",
    "open_wait",
    "read_approval",
    "read_artifact",
    "read_blob_availability",
    "read_budget_snapshot",
    "read_capability_grant",
    "read_cleanup_receipt",
    "read_evidence_item",
    "read_policy_snapshot",
    "read_run",
    "read_run_approvals",
    "read_run_artifacts",
    "read_run_budget_snapshots",
    "read_run_capability_grants",
    "read_run_cleanup_receipts",
    "read_run_events",
    "read_run_evidence",
    "read_run_id_by_job",
    "read_run_id_by_logical_key",
    "read_run_policy_snapshots",
    "read_run_sequence",
    "read_run_steps",
    "read_run_waits",
    "read_workspace_run_ids",
    "record_approval_decision",
    "record_step_status",
    "request_approval",
    "runtime_writer",
    "start_attempt",
    "transaction_local_writer",
]
