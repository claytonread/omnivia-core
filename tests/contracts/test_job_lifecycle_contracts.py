"""Tests for the A2.4 provider-neutral import and durable-job contract slice:
`import.start`, `job.get`, `job.cancel`, `job.retry`, and `job.events` (ADR-039, T-0631).

Covers the five operation input/result DTO pairs (strict schema conformance, tolerant
decoder round trips, additive-unknown-field tolerance vs. strict schema rejection) and the
semantic hardening in :mod:`omnivia_core.contracts.v1.semantics_jobs`: the job state
machine, exact attempt chronology, typed terminal success bound to `ImportCompletionResult`,
accepted-versus-refused control dispositions with no `job.resume` anywhere, scoped
idempotency equivalence, and snapshot-stable `job.events` pagination proven through a
trusted, non-wire page binding rather than by decoding an opaque token.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts import v1
from omnivia_core.contracts.v1 import codec, generated
from omnivia_core.contracts.v1 import semantics_evidence as sem_evidence
from omnivia_core.contracts.v1 import semantics_jobs as sem_jobs
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    ApiError,
    CapabilityRequirement,
    ClientIdentity,
    ContractDecodeError,
    EvidenceArtifact,
    ImportCompletionResult,
    ImportSourceDescriptor,
    ImportStartInput,
    ImportStartResult,
    JobAttempt,
    JobCancelInput,
    JobCancellationOutcome,
    JobCancelResult,
    JobControl,
    JobEvent,
    JobEventsInput,
    JobEventsResult,
    JobGetInput,
    JobGetResult,
    JobHandle,
    JobIdentity,
    JobProgress,
    JobReference,
    JobRetryInput,
    JobRetryResult,
    JobTerminalCancellation,
    JobTerminalFailure,
    JobTerminalSuccess,
    MutationPrecondition,
    OperationIdempotencyMetadata,
    OperationPreconditionMetadata,
    PageMetadata,
    RequestMetadata,
)
from omnivia_core.contracts.v1.semantics_jobs import JobEventsPageBinding

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
BASE_URI = "https://contracts.omnivia.dev/application/v1/"

ALL_SCHEMAS = (
    "common",
    "compatibility",
    "errors",
    "envelopes",
    "service",
    "records",
    "jobs",
    "operations",
    "workspace",
    "memory",
    "evidence",
    "knowledge",
    "graph",
    "context-pack",
    "compatibility-matrix",
    "application-v1",
)


def _registry() -> Registry:
    entries: list[tuple[str, Resource[Any]]] = []
    for name in ALL_SCHEMAS:
        document = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        resource = Resource.from_contents(document)
        resource_id = resource.id()
        assert resource_id is not None
        entries.append((resource_id, resource))
    return Registry().with_resources(entries)


REGISTRY = _registry()


def _strict_validator(def_name: str, schema_file: str = "jobs") -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"{BASE_URI}{schema_file}.schema.json#/$defs/{def_name}"},
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _assert_schema_valid(def_name: str, document: Any, schema_file: str = "jobs") -> None:
    errors = list(_strict_validator(def_name, schema_file).iter_errors(document))
    assert not errors, f"expected {def_name} to be schema-valid, found {errors}"


def _assert_schema_invalid(def_name: str, document: Any, schema_file: str = "jobs") -> None:
    errors = list(_strict_validator(def_name, schema_file).iter_errors(document))
    assert errors, f"expected {def_name} to be schema-invalid, found none"


# --------------------------------------------------------------------------
# Builders. Every one produces a value that is valid on its own, so a test can
# say exactly what it breaks.
# --------------------------------------------------------------------------

CHECKSUM = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
JOB_ID = "job-1"
WORKSPACE_ID = "workspace-1"
PRINCIPAL_ID = "principal-1"

T0 = "2026-07-30T00:00:00Z"
T1 = "2026-07-30T00:00:05Z"
T2 = "2026-07-30T00:01:00Z"
T3 = "2026-07-30T00:02:00Z"


def _identity(
    job_id: str = JOB_ID,
    *,
    job_kind: str = sem_jobs.IMPORT_JOB_KIND,
    originating_operation: str = sem_jobs.IMPORT_START_OPERATION,
) -> JobIdentity:
    return JobIdentity(
        job_id=job_id,
        job_kind=job_kind,
        originating_operation=originating_operation,
        audit_reference="audit-1",
        workspace_id=WORKSPACE_ID,
    )


def _source(**overrides: Any) -> ImportSourceDescriptor:
    source = ImportSourceDescriptor(
        staged_source_ref="staged-abc",
        source_kind="archive",
        content_checksum=CHECKSUM,
        content_length_bytes=2048,
        media_type="application/zip",
        source_version="v3",
    )
    return dataclasses.replace(source, **overrides) if overrides else source


def _attempt(
    number: int = 1,
    *,
    started_at: str = T0,
    finished_at: str | None = T2,
    state: str = "succeeded",
    error: ApiError | None = None,
) -> JobAttempt:
    return JobAttempt(
        attempt_number=number,
        started_at=started_at,
        finished_at=finished_at,
        state=state,
        error=error,
    )


def _error() -> ApiError:
    return ApiError(code="internal_recoverable", message="transient", retry_class="retryable")


def _handle(
    *,
    state: str = "running",
    cancellation: str | None = None,
    recovery: str | None = None,
    progress: JobProgress | None = None,
    latest_attempt: JobAttempt | None = None,
    identity: JobIdentity | None = None,
    updated_at: str = T2,
) -> JobHandle:
    defaults = {
        "queued": ("cancellable", "not_retryable"),
        "running": ("cancellable", "not_retryable"),
        "succeeded": ("not_cancellable", "not_retryable"),
        "failed": ("not_cancellable", "retryable"),
        "cancelled": ("cancelled", "not_retryable"),
    }
    default_cancellation, default_recovery = defaults.get(state, ("not_cancellable", "not_retryable"))
    if latest_attempt is None:
        if state == "running":
            latest_attempt = _attempt(finished_at=None, state="running")
        elif state in {"succeeded", "failed"}:
            latest_attempt = _attempt(
                state=state, error=_error() if state == "failed" else None
            )
    return JobHandle(
        identity=identity if identity is not None else _identity(),
        state=state,
        created_at=T0,
        updated_at=updated_at,
        control=JobControl(
            cancellation=cancellation if cancellation is not None else default_cancellation,
            recovery=recovery if recovery is not None else default_recovery,
        ),
        progress=progress,
        latest_attempt=latest_attempt,
    )


def _completion(**overrides: Any) -> ImportCompletionResult:
    completion = ImportCompletionResult(
        import_run_id=JOB_ID,
        source=_source(),
        discovered_items=10,
        evidence_records_created=7,
        skipped_items=3,
        failed_items=0,
        partial=False,
    )
    return dataclasses.replace(completion, **overrides) if overrides else completion


def _success(**overrides: Any) -> JobTerminalSuccess:
    terminal = JobTerminalSuccess(
        identity=_identity(),
        state="succeeded",
        finished_at=T2,
        attempts=(_attempt(state="succeeded"),),
        result_kind=sem_jobs.JOB_TERMINAL_RESULT_KIND_IMPORT_COMPLETION,
        result=_completion().to_wire(),
    )
    return dataclasses.replace(terminal, **overrides) if overrides else terminal


def _failure(**overrides: Any) -> JobTerminalFailure:
    terminal = JobTerminalFailure(
        identity=_identity(),
        state="failed",
        finished_at=T2,
        attempts=(_attempt(state="failed", error=_error()),),
        error=_error(),
    )
    return dataclasses.replace(terminal, **overrides) if overrides else terminal


def _cancellation(**overrides: Any) -> JobTerminalCancellation:
    terminal = JobTerminalCancellation(
        identity=_identity(),
        state="cancelled",
        finished_at=T2,
        attempts=(),
        cancellation=JobCancellationOutcome(reason="caller_requested"),
    )
    return dataclasses.replace(terminal, **overrides) if overrides else terminal


def _request_metadata(**overrides: Any) -> RequestMetadata:
    metadata = RequestMetadata(
        request_id="req-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        api_version="1.2",
        client=ClientIdentity(id="omnivia.desktop", version="1.0.0"),
        workspace_id=WORKSPACE_ID,
        scopes=("memory:write", "job:control"),
        purpose="ingestion",
        required_capabilities=(
            CapabilityRequirement(id="ingestion.import", minimum_version="1.0", required=True),
        ),
        idempotency_key="key-1",
    )
    return dataclasses.replace(metadata, **overrides) if overrides else metadata


def _binding(**overrides: Any) -> JobEventsPageBinding:
    binding = JobEventsPageBinding(
        token_format_version=sem_jobs.JOB_EVENTS_PAGE_BINDING_FORMAT_VERSION,
        principal_id=PRINCIPAL_ID,
        workspace_id=WORKSPACE_ID,
        operation=sem_jobs.JOB_EVENTS_OPERATION,
        job_id=JOB_ID,
        ordering=sem_jobs.JOB_EVENTS_ORDERING_SEQUENCE_ASC,
        snapshot_event_count=4,
        next_sequence=0,
    )
    return binding._replace(**overrides) if overrides else binding


def _events(*sequences: int, state: str = "running") -> tuple[JobEvent, ...]:
    return tuple(
        JobEvent(sequence=sequence, occurred_at=T0, state=state) for sequence in sequences
    )


def _events_result(
    events: tuple[JobEvent, ...],
    *,
    snapshot: int = 4,
    token: str | None = None,
    job_id: str = JOB_ID,
) -> JobEventsResult:
    return JobEventsResult(
        job_id=job_id,
        events=events,
        snapshot_event_count=snapshot,
        page=PageMetadata(continuation_token=token),
    )


# --------------------------------------------------------------------------
# Wire shape: strict schema conformance and tolerant decoding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("def_name", "value"),
    [
        ("ImportSourceDescriptor", _source()),
        ("ImportStartInput", ImportStartInput(source=_source())),
        ("ImportStartResult", ImportStartResult(job=_handle())),
        ("ImportCompletionResult", _completion()),
        ("JobGetInput", JobGetInput(job_id=JOB_ID)),
        ("JobGetResult", JobGetResult(job=_handle())),
        ("JobCancelInput", JobCancelInput(job_id=JOB_ID, reason="caller_requested")),
        (
            "JobCancelResult",
            JobCancelResult(job=_handle(), cancellation_disposition="cancellation_requested"),
        ),
        ("JobRetryInput", JobRetryInput(job_id=JOB_ID)),
        (
            "JobRetryResult",
            JobRetryResult(job=_handle(state="queued"), recovery_disposition="retry_scheduled"),
        ),
        ("JobEventsInput", JobEventsInput(job_id=JOB_ID, limit=100)),
        ("JobEventsResult", _events_result(_events(0, 1, 2, 3))),
    ],
)
def test_payload_round_trips_and_is_schema_valid(def_name: str, value: Any) -> None:
    wire = value.to_wire()
    _assert_schema_valid(def_name, wire)
    assert type(value).from_wire(wire) == value


def test_import_start_input_accepts_nothing_but_a_staged_source() -> None:
    """`import.start` must not admit a path, URL, inline archive, credential, workspace id,
    parser name, or storage option -- the wire shape is what makes that unforgeable rather
    than merely documented."""
    assert {field.name for field in dataclasses.fields(ImportStartInput)} == {"source"}
    for smuggled in (
        "path",
        "url",
        "archive",
        "credentials",
        "workspace_id",
        "parser",
        "storage_backend",
    ):
        document = {"source": _source().to_wire(), smuggled: "x"}
        _assert_schema_invalid("ImportStartInput", document)


def test_import_source_descriptor_names_only_staged_content() -> None:
    assert {field.name for field in dataclasses.fields(ImportSourceDescriptor)} == {
        "staged_source_ref",
        "source_kind",
        "content_checksum",
        "content_length_bytes",
        "media_type",
        "source_version",
    }


def test_import_start_result_carries_only_a_job_handle() -> None:
    assert {field.name for field in dataclasses.fields(ImportStartResult)} == {"job"}


@pytest.mark.parametrize(
    "checksum",
    [
        "sha256:" + "9F86D081884C7D659A2FEAA0C55AD015A3BF4F1B2B0B822CD15D6C15B0F00A08",
        "sha256:" + "9f86d081884c7d659a2feaa0c55ad015",
        "sha512:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    ],
)
def test_content_checksum_admits_exactly_one_algorithm_length_and_case(checksum: str) -> None:
    _assert_schema_invalid("ContentChecksum", checksum)
    with pytest.raises(ContractSemanticError, match="ContentChecksum"):
        sem_jobs.validate_import_source_descriptor(_source(content_checksum=checksum))


def test_tolerant_decode_ignores_an_additive_unknown_field_the_schema_rejects() -> None:
    """The two halves of the posture, on one document: a newer peer's added field still
    decodes in production, and the strict schema still refuses it."""
    wire = ImportStartInput(source=_source()).to_wire()
    wire["experimental_dry_run"] = True
    _assert_schema_invalid("ImportStartInput", wire)
    assert ImportStartInput.from_wire(wire) == ImportStartInput(source=_source())


@pytest.mark.parametrize(
    ("value", "field_name"),
    [
        (JobControl(cancellation="future.unseen", recovery="not_retryable"), "cancellation"),
        (JobControl(cancellation="cancellable", recovery="future.unseen"), "recovery"),
    ],
)
def test_unknown_control_availability_decodes_and_is_preserved(
    value: JobControl, field_name: str
) -> None:
    decoded = JobControl.from_wire(value.to_wire())
    assert getattr(decoded, field_name) == "future.unseen"


def test_job_control_has_no_resume_member() -> None:
    """There is one recovery operation, so there is one recovery availability. A `resume`
    member would advertise a control `job.retry` already covers and no operation exposes."""
    assert {field.name for field in dataclasses.fields(JobControl)} == {
        "cancellation",
        "recovery",
    }
    document = {"cancellation": "cancellable", "recovery": "not_retryable", "resume": "resumable"}
    _assert_schema_invalid("JobControl", document)


def test_no_job_resume_operation_or_dto_is_published() -> None:
    published = set(v1.__all__)
    assert not {name for name in published if "Resume" in name or "RESUME" in name.upper()} - {
        "JOB_RECOVERY_DISPOSITION_RESUME_SCHEDULED",
        "JOB_RECOVERY_AVAILABILITY_RESUMABLE",
        # Not a job resume, and not reachable from one. This names why a *Workflow
        # Run's journal* forbids resuming -- quarantined on an unanswered integrity
        # finding, or past a recorded retention boundary -- which is a statement about
        # verifiable history, made on a record the job family neither owns nor reads.
        # `job.retry` remains the single recovery operation on the job wire.
        "WorkflowResumeDiagnostic",
        "WORKFLOW_RESUME_DIAGNOSTICS",
    }
    assert "job.resume" not in sem_jobs.JOB_LIFECYCLE_OPERATIONS
    assert "job.resume" not in sem_jobs.JOB_LIFECYCLE_OPERATION_POSTURES


def test_terminal_success_is_typed_not_opaque() -> None:
    """`result_kind` is required, so a success payload is always read under a declared shape
    rather than guessed at from the job kind."""
    wire = _success().to_wire()
    _assert_schema_valid("JobTerminalResult", wire)
    del wire["result_kind"]
    _assert_schema_invalid("JobTerminalResult", wire)
    with pytest.raises(ContractDecodeError, match="missing required field 'result_kind'"):
        JobTerminalSuccess.from_wire(wire)


# --------------------------------------------------------------------------
# Job state machine
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ("queued", "queued"),
        ("queued", "running"),
        ("queued", "failed"),
        ("queued", "cancelled"),
        ("running", "running"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("succeeded", "succeeded"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_known_state_transitions_are_permitted(previous: str, current: str) -> None:
    sem_jobs.validate_job_state_transition(previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ("queued", "succeeded"),
        ("succeeded", "running"),
        ("succeeded", "failed"),
        ("succeeded", "queued"),
        ("failed", "running"),
        ("failed", "succeeded"),
        ("cancelled", "running"),
        ("cancelled", "succeeded"),
        ("running", "queued"),
    ],
)
def test_illegal_state_transitions_are_refused(previous: str, current: str) -> None:
    with pytest.raises(ContractSemanticError, match="may not move from"):
        sem_jobs.validate_job_state_transition(previous, current)


@pytest.mark.parametrize("previous", ["failed", "cancelled"])
def test_a_terminal_job_reaches_queued_only_through_an_accepted_recovery(previous: str) -> None:
    with pytest.raises(ContractSemanticError, match="no accepted job.retry was declared"):
        sem_jobs.validate_job_state_transition(previous, "queued")
    sem_jobs.validate_job_state_transition(previous, "queued", recovery_accepted=True)


def test_recovery_never_reopens_a_succeeded_job() -> None:
    with pytest.raises(ContractSemanticError, match="may not move from"):
        sem_jobs.validate_job_state_transition("succeeded", "queued", recovery_accepted=True)


@pytest.mark.parametrize(
    ("previous", "current"),
    [("future.unseen", "running"), ("running", "future.unseen"), ("a.b", "c.d")],
)
def test_transitions_touching_an_unknown_state_are_not_judged(previous: str, current: str) -> None:
    """An unknown state implies neither terminality nor any particular successor, so this
    build refuses to invent an answer in either direction."""
    sem_jobs.validate_job_state_transition(previous, current)


def test_unknown_states_are_never_terminal_and_grant_no_control() -> None:
    assert not sem_jobs.is_terminal_job_state("future.unseen")
    assert not sem_jobs.permits_cancellation("future.unseen")
    assert not sem_jobs.permits_recovery("future.unseen")
    assert not sem_jobs.is_accepted_cancellation_disposition("future.unseen")
    assert not sem_jobs.is_accepted_recovery_disposition("future.unseen")


def test_availability_and_disposition_vocabularies_stay_distinct() -> None:
    """A handle says what a caller *may* do; a control result says what one call *did*. They
    share `not_cancellable`/`not_retryable` and nothing else, so neither vocabulary can be
    read as the other by accident."""
    assert set(sem_jobs.JOB_CANCELLATION_AVAILABILITIES) & set(
        sem_jobs.JOB_CANCELLATION_DISPOSITIONS
    ) == {"cancelled", "not_cancellable"}
    assert set(sem_jobs.JOB_RECOVERY_AVAILABILITIES) & set(
        sem_jobs.JOB_RECOVERY_DISPOSITIONS
    ) == {"not_retryable"}
    assert sem_jobs.permits_recovery("resumable")
    assert not sem_jobs.is_accepted_recovery_disposition("resumable")


# --------------------------------------------------------------------------
# Attempt chronology
# --------------------------------------------------------------------------


def test_attempt_history_accepts_an_exact_chronology_with_abutting_instants() -> None:
    sem_jobs.validate_job_attempt_history(
        (
            _attempt(1, started_at=T0, finished_at=T1, state="failed", error=_error()),
            _attempt(2, started_at=T1, finished_at=T2, state="succeeded"),
        )
    )


def test_attempt_numbers_are_contiguous_from_one() -> None:
    with pytest.raises(ContractSemanticError, match="numbered 1..N contiguously"):
        sem_jobs.validate_job_attempt_history((_attempt(2, state="succeeded"),))
    with pytest.raises(ContractSemanticError, match="numbered 1..N contiguously"):
        sem_jobs.validate_job_attempt_history(
            (
                _attempt(1, started_at=T0, finished_at=T1, state="failed", error=_error()),
                _attempt(3, started_at=T1, finished_at=T2, state="succeeded"),
            )
        )
    with pytest.raises(ContractSemanticError, match="attempts are 1-based"):
        sem_jobs.validate_job_attempt_history((_attempt(0, state="succeeded"),))


def test_attempts_never_overlap_or_move_backwards() -> None:
    with pytest.raises(ContractSemanticError, match="never overlap or move backward"):
        sem_jobs.validate_job_attempt_history(
            (
                _attempt(1, started_at=T1, finished_at=T2, state="failed", error=_error()),
                _attempt(2, started_at=T0, finished_at=T3, state="succeeded"),
            )
        )


def test_an_attempt_never_finishes_before_it_starts() -> None:
    with pytest.raises(ContractSemanticError, match="precedes"):
        sem_jobs.validate_job_attempt(
            _attempt(1, started_at=T2, finished_at=T0, state="succeeded")
        )


def test_only_the_final_attempt_of_an_executing_job_may_be_unfinished() -> None:
    unfinished_first = (
        _attempt(1, started_at=T0, finished_at=None, state="running"),
        _attempt(2, started_at=T1, finished_at=T2, state="succeeded"),
    )
    with pytest.raises(ContractSemanticError, match="only the final attempt may be unfinished"):
        sem_jobs.validate_job_attempt_history(unfinished_first, executing=True)

    final_unfinished = (_attempt(1, started_at=T0, finished_at=None, state="running"),)
    with pytest.raises(ContractSemanticError, match="actively\n?\\s*executing|actively executing"):
        sem_jobs.validate_job_attempt_history(final_unfinished, executing=False)
    sem_jobs.validate_job_attempt_history(final_unfinished, executing=True)


def test_an_attempt_carries_a_terminal_error_exactly_when_it_failed() -> None:
    with pytest.raises(ContractSemanticError, match="must carry its terminal error"):
        sem_jobs.validate_job_attempt(_attempt(state="failed"))
    for state in ("succeeded", "cancelled", "running", "future.unseen"):
        with pytest.raises(ContractSemanticError, match="only a failed attempt"):
            sem_jobs.validate_job_attempt(
                _attempt(state=state, finished_at=None if state == "running" else T2, error=_error())
            )


def test_running_attempts_are_unfinished_and_terminal_attempts_are_finished() -> None:
    with pytest.raises(ContractSemanticError, match="a running attempt has not finished"):
        sem_jobs.validate_job_attempt(_attempt(state="running", finished_at=T2))
    for state in ("succeeded", "cancelled"):
        with pytest.raises(ContractSemanticError, match="must carry finished_at"):
            sem_jobs.validate_job_attempt(_attempt(state=state, finished_at=None))


def test_attempt_instants_fall_inside_the_jobs_own_lifetime() -> None:
    with pytest.raises(ContractSemanticError, match="precedes the job's created_at"):
        sem_jobs.validate_job_attempt_history(
            (_attempt(1, started_at=T0, finished_at=T2, state="succeeded"),),
            created_at=T1,
            updated_at=T3,
        )
    with pytest.raises(ContractSemanticError, match="follows the job's updated_at"):
        sem_jobs.validate_job_attempt_history(
            (_attempt(1, started_at=T0, finished_at=T3, state="succeeded"),),
            created_at=T0,
            updated_at=T2,
        )


def test_queued_is_a_state_of_the_job_never_of_an_execution() -> None:
    """An attempt exists because execution started. A `queued` attempt claims a number in the
    history of executions while describing one that never began, which makes every later
    ordinal in that history mean something other than what it says."""
    with pytest.raises(ContractSemanticError, match="is a state of the job, not of one of its"):
        sem_jobs.validate_job_attempt(_attempt(state="queued", finished_at=None))
    with pytest.raises(ContractSemanticError, match="is a state of the job, not of one of its"):
        sem_jobs.validate_job_attempt_history((_attempt(state="queued", finished_at=T2),))
    with pytest.raises(ContractSemanticError, match="is a state of the job, not of one of its"):
        sem_jobs.validate_job_handle(
            _handle(state="running", latest_attempt=_attempt(state="queued", finished_at=None))
        )


def test_a_succeeded_attempt_is_final_and_is_never_followed_by_another() -> None:
    """Retrying a failed execution and resuming a cancelled one are the two things that
    produce a next attempt. Nothing reopens a success, so an attempt after one says the job
    both finished and kept going."""
    first = _attempt(1, started_at=T0, finished_at=T1, state="succeeded")
    second = _attempt(2, started_at=T1, finished_at=T2, state="succeeded")
    with pytest.raises(ContractSemanticError, match="a succeeded attempt is final"):
        sem_jobs.validate_job_attempt_history((first, second))
    # A failed or cancelled attempt is exactly what may be followed.
    sem_jobs.validate_job_attempt_history(
        (
            _attempt(1, started_at=T0, finished_at=T1, state="failed", error=_error()),
            _attempt(2, started_at=T1, finished_at=T2, state="cancelled"),
            _attempt(3, started_at=T2, finished_at=T3, state="succeeded"),
        )
    )


@pytest.mark.parametrize(
    ("attempt_state", "finished_at", "expected"),
    [
        ("succeeded", T2, "a queued job's latest attempt"),
        ("running", None, "carries no unfinished attempt"),
    ],
    ids=["succeeded", "running"],
)
def test_a_queued_handle_carries_only_the_attempt_a_recovery_retained(
    attempt_state: str, finished_at: str | None, expected: str
) -> None:
    """A queued job either never executed, or is waiting to run again after an accepted
    recovery -- in which case its latest attempt is the finished failed or cancelled one that
    recovery was scheduled from. A succeeded one claims a finished job waiting to restart; a
    running one claims an execution nobody is performing."""
    attempt = _attempt(state=attempt_state, finished_at=finished_at)
    with pytest.raises(ContractSemanticError, match=expected):
        sem_jobs.validate_job_handle(_handle(state="queued", latest_attempt=attempt))
    # The retained attempt an accepted recovery leaves behind is exactly what is allowed.
    sem_jobs.validate_job_handle(
        _handle(state="queued", latest_attempt=_attempt(state="failed", error=_error()))
    )
    sem_jobs.validate_job_handle(
        _handle(state="queued", latest_attempt=_attempt(state="cancelled"))
    )


def test_attempt_history_is_bounded() -> None:
    attempts = tuple(
        _attempt(number, started_at=T0, finished_at=T0, state="failed", error=_error())
        for number in range(1, 258)
    )
    with pytest.raises(ContractSemanticError, match="exceeds the maximum"):
        sem_jobs.validate_job_attempt_history(attempts)


# --------------------------------------------------------------------------
# Job handle coherence
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["queued", "running", "succeeded", "failed", "cancelled"])
def test_a_well_formed_handle_of_every_known_state_validates(state: str) -> None:
    sem_jobs.validate_job_handle(_handle(state=state))


def test_a_handle_never_advertises_a_control_its_state_forbids() -> None:
    with pytest.raises(ContractSemanticError, match="control.cancellation"):
        sem_jobs.validate_job_handle(_handle(state="succeeded", cancellation="cancellable"))
    with pytest.raises(ContractSemanticError, match="control.recovery"):
        sem_jobs.validate_job_handle(_handle(state="succeeded", recovery="retryable"))
    with pytest.raises(ContractSemanticError, match="control.recovery"):
        sem_jobs.validate_job_handle(_handle(state="running", recovery="retryable"))


def test_a_cancelled_job_may_be_resumable_and_a_failed_job_retryable() -> None:
    sem_jobs.validate_job_handle(_handle(state="cancelled", recovery="resumable"))
    sem_jobs.validate_job_handle(_handle(state="failed", recovery="retryable"))


def test_an_unknown_state_is_judged_only_for_shape() -> None:
    handle = _handle(state="future.unseen", cancellation="cancellable", recovery="retryable")
    sem_jobs.validate_job_handle(handle)
    assert not sem_jobs.is_terminal_job_state(handle.state)


@pytest.mark.parametrize("field", ["cancellation", "recovery"])
def test_a_known_state_preserves_an_unknown_availability_without_acting_on_it(field: str) -> None:
    """The fail-safe direction for an open vocabulary on a *known* state. This build has never
    seen the value, so it cannot know it is wrong -- refusing it would reject a newer peer's
    legitimate handle. Nothing is granted by keeping it: both readers answer false, so no
    control call can ever be accepted on the strength of one."""
    handle = _handle(state="succeeded", **{field: "future.unseen"})
    sem_jobs.validate_job_handle(handle)
    assert getattr(handle.control, field) == "future.unseen"
    assert not sem_jobs.permits_cancellation(handle.control.cancellation)
    assert not sem_jobs.permits_recovery(handle.control.recovery)


def test_a_handles_timestamps_bracket_its_own_history() -> None:
    with pytest.raises(ContractSemanticError, match="updated_at .* precedes created_at"):
        sem_jobs.validate_job_handle(_handle(state="queued", updated_at="2026-07-29T00:00:00Z"))


def test_a_running_job_reports_the_attempt_it_is_running_under() -> None:
    bare = JobHandle(
        identity=_identity(),
        state="running",
        created_at=T0,
        updated_at=T1,
        control=JobControl(cancellation="cancellable", recovery="not_retryable"),
    )
    with pytest.raises(ContractSemanticError, match="must report the attempt"):
        sem_jobs.validate_job_handle(bare)
    running_with_finished = JobHandle(
        identity=_identity(),
        state="running",
        created_at=T0,
        updated_at=T1,
        control=JobControl(cancellation="cancellable", recovery="not_retryable"),
        latest_attempt=_attempt(state="succeeded", finished_at=T1),
    )
    with pytest.raises(ContractSemanticError, match="latest attempt must itself be running"):
        sem_jobs.validate_job_handle(running_with_finished)


def test_a_queued_job_may_still_report_the_previous_terminal_attempt() -> None:
    """An accepted recovery returns a job to `queued` without creating an attempt, so the
    previous terminal attempt is legitimately still the latest one."""
    sem_jobs.validate_job_handle(
        _handle(state="queued", latest_attempt=_attempt(state="failed", error=_error()))
    )


def test_a_finished_job_reports_the_attempt_that_produced_its_outcome() -> None:
    for state in ("succeeded", "failed"):
        bare = JobHandle(
            identity=_identity(),
            state=state,
            created_at=T0,
            updated_at=T1,
            control=JobControl(
                cancellation="not_cancellable",
                recovery="not_retryable" if state == "succeeded" else "retryable",
            ),
        )
        with pytest.raises(ContractSemanticError, match="must report the attempt"):
            sem_jobs.validate_job_handle(bare)


def test_progress_never_overstates_what_happened() -> None:
    with pytest.raises(ContractSemanticError, match="exceeds total_units"):
        sem_jobs.validate_job_handle(
            _handle(state="running", progress=JobProgress(unit="item", completed_units=11, total_units=10))
        )
    with pytest.raises(ContractSemanticError, match="must have completed it"):
        sem_jobs.validate_job_handle(
            _handle(
                state="succeeded",
                progress=JobProgress(unit="item", completed_units=7, total_units=10),
            )
        )
    sem_jobs.validate_job_handle(
        _handle(
            state="succeeded", progress=JobProgress(unit="item", completed_units=10, total_units=10)
        )
    )


def test_a_jobs_progress_unit_is_fixed_for_its_history() -> None:
    first = _handle(state="running", progress=JobProgress(unit="item", completed_units=1))
    same_unit = _handle(
        state="running", progress=JobProgress(unit="item", completed_units=2), updated_at=T2
    )
    changed_unit = _handle(
        state="running", progress=JobProgress(unit="byte", completed_units=2), updated_at=T2
    )
    sem_jobs.validate_job_observation_progression(first, same_unit)
    with pytest.raises(ContractSemanticError, match="progress.unit changed"):
        sem_jobs.validate_job_observation_progression(first, changed_unit)


def test_an_observation_never_changes_a_jobs_identity_or_moves_time_backwards() -> None:
    first = _handle(state="running", updated_at=T1)
    with pytest.raises(ContractSemanticError, match="identity changed"):
        sem_jobs.validate_job_observation_progression(
            first, _handle(state="running", identity=_identity("job-2"), updated_at=T2)
        )
    with pytest.raises(ContractSemanticError, match="updated_at moved backwards"):
        sem_jobs.validate_job_observation_progression(
            _handle(state="running", updated_at=T2), _handle(state="running", updated_at=T1)
        )


def test_an_observation_progression_enforces_the_transition_table() -> None:
    failed = _handle(state="failed", updated_at=T2)
    queued = _handle(
        state="queued",
        updated_at=T3,
        latest_attempt=_attempt(state="failed", error=_error()),
    )
    with pytest.raises(ContractSemanticError, match="may not move from"):
        sem_jobs.validate_job_observation_progression(failed, queued)
    sem_jobs.validate_job_observation_progression(failed, queued, recovery_accepted=True)


def test_polling_may_skip_states_and_attempts_without_ever_regressing() -> None:
    """Polling is sampling, not witnessing. A caller that reads a job as `queued` and next as
    `succeeded` missed `running`; the second reading is still a true statement about a later
    instant, and rejecting it would punish the caller for its timing. The adjacent-step table
    still exists for `job.events`, where every step is on the record."""
    queued = _handle(state="queued", updated_at=T0, latest_attempt=None)
    succeeded = _handle(state="succeeded", updated_at=T2)
    sem_jobs.validate_job_observation_progression(queued, succeeded)
    with pytest.raises(ContractSemanticError, match="may not move from"):
        sem_jobs.validate_job_state_transition("queued", "succeeded")

    # Skipping an attempt is likewise sampling, not regression.
    at_one = _handle(
        state="queued",
        updated_at=T0,
        latest_attempt=_attempt(1, started_at=T0, finished_at=T0, state="failed", error=_error()),
    )
    at_three = _handle(
        state="running",
        updated_at=T2,
        latest_attempt=_attempt(3, started_at=T1, finished_at=None, state="running"),
    )
    sem_jobs.validate_job_observation_progression(at_one, at_three)


def test_an_observation_never_unwinds_or_drops_an_attempt() -> None:
    at_two = _handle(
        state="running",
        updated_at=T1,
        latest_attempt=_attempt(2, started_at=T1, finished_at=None, state="running"),
    )
    at_one = _handle(
        state="running",
        updated_at=T2,
        latest_attempt=_attempt(1, started_at=T0, finished_at=None, state="running"),
    )
    with pytest.raises(ContractSemanticError, match="attempt_number moved backwards"):
        sem_jobs.validate_job_observation_progression(at_two, at_one)

    forgotten = _handle(state="queued", updated_at=T2, latest_attempt=None)
    with pytest.raises(ContractSemanticError, match="a later observation never drops it"):
        sem_jobs.validate_job_observation_progression(
            _handle(
                state="queued",
                updated_at=T1,
                latest_attempt=_attempt(1, started_at=T0, finished_at=T1, state="failed", error=_error()),
            ),
            forgotten,
        )


@pytest.mark.parametrize(
    ("rewrite", "current_state", "expected"),
    [
        ({"started_at": T1}, "queued", "one execution starts once"),
        ({"finished_at": T3}, "queued", "may not rewrite it"),
        ({"state": "cancelled", "error": None}, "queued", "may not rewrite it"),
        (
            {"error": ApiError(code="not_found", message="other", retry_class="non_retryable")},
            "queued",
            "may not rewrite it",
        ),
        ({"finished_at": None, "state": "running", "error": None}, "running", "may not rewrite it"),
    ],
    ids=["started_at", "finished_at", "state", "error", "unfinished-again"],
)
def test_an_observation_never_rewrites_the_same_finished_attempt(
    rewrite: dict[str, Any], current_state: str, expected: str
) -> None:
    """Attempt N is one execution. Once it has finished, everything it says about how it
    ended is history; a later reading that disagrees is not an observation of that execution
    but a second, contradictory account of it.

    Both handles here are individually coherent -- a queued job legitimately retains a
    finished failed or cancelled attempt, and a running job legitimately carries a running one
    -- so it is the *progression* rule that fires, not a per-handle rule that would have
    caught the mutation for an unrelated reason."""
    finished = _attempt(1, started_at=T0, finished_at=T1, state="failed", error=_error())
    previous = _handle(state="queued", updated_at=T1, latest_attempt=finished)
    rewritten = dataclasses.replace(finished, **rewrite)
    current = _handle(state=current_state, updated_at=T3, latest_attempt=rewritten)
    with pytest.raises(ContractSemanticError, match=expected):
        sem_jobs.validate_job_observation_progression(previous, current)


def test_an_observation_never_uncompletes_work_or_restates_the_total() -> None:
    """A caller that saw 7 of 10 done and later reads 3 cannot tell whether the job lost work
    or the counter lost meaning. A total behaves the same way in the other direction: revising
    or withdrawing it retroactively changes what every fraction already read meant. Learning a
    total that was not known before states more than it did, not something different."""
    counted = _handle(
        state="running", progress=JobProgress(unit="item", completed_units=7, total_units=10)
    )
    with pytest.raises(ContractSemanticError, match="completed_units fell"):
        sem_jobs.validate_job_observation_progression(
            counted,
            _handle(
                state="running",
                updated_at=T3,
                progress=JobProgress(unit="item", completed_units=3, total_units=10),
            ),
        )
    with pytest.raises(ContractSemanticError, match="total_units changed"):
        sem_jobs.validate_job_observation_progression(
            counted,
            _handle(
                state="running",
                updated_at=T3,
                progress=JobProgress(unit="item", completed_units=7, total_units=12),
            ),
        )
    with pytest.raises(ContractSemanticError, match="total_units was stated"):
        sem_jobs.validate_job_observation_progression(
            counted,
            _handle(
                state="running",
                updated_at=T3,
                progress=JobProgress(unit="item", completed_units=7),
            ),
        )
    sem_jobs.validate_job_observation_progression(
        _handle(state="running", progress=JobProgress(unit="item", completed_units=7)),
        _handle(
            state="running",
            updated_at=T3,
            progress=JobProgress(unit="item", completed_units=7, total_units=10),
        ),
    )


def test_the_recovery_observation_sequence_this_contract_requires_is_accepted() -> None:
    """The one sequence every retry produces, end to end: a failed job with its finished
    attempt 1, the same job returned to `queued` still reporting that attempt because recovery
    creates none, and then attempt 2 once execution actually starts."""
    attempt_one = _attempt(1, started_at=T0, finished_at=T1, state="failed", error=_error())
    failed = _handle(
        state="failed", recovery="retryable", updated_at=T1, latest_attempt=attempt_one
    )
    queued = _handle(state="queued", updated_at=T2, latest_attempt=attempt_one)
    running = _handle(
        state="running",
        updated_at=T3,
        latest_attempt=_attempt(2, started_at=T2, finished_at=None, state="running"),
    )
    sem_jobs.validate_job_observation_progression(failed, queued, recovery_accepted=True)
    sem_jobs.validate_job_observation_progression(queued, running)


# --------------------------------------------------------------------------
# Terminal results and typed import completion
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("terminal", "accepted"), [(_success(), _source()), (_failure(), None), (_cancellation(), None)]
)
def test_a_well_formed_terminal_result_of_every_branch_validates(
    terminal: Any, accepted: Any
) -> None:
    sem_jobs.validate_job_terminal_result(terminal, accepted_import_source=accepted)


@pytest.mark.parametrize(
    ("terminal", "wrong_state", "accepted"),
    [
        (_success(), "failed", _source()),
        (_failure(), "cancelled", None),
        (_cancellation(), "succeeded", None),
    ],
)
def test_each_terminal_branch_states_its_own_state_exactly(
    terminal: Any, wrong_state: str, accepted: Any
) -> None:
    with pytest.raises(ContractSemanticError, match="exactly"):
        sem_jobs.validate_job_terminal_result(
            dataclasses.replace(terminal, state=wrong_state), accepted_import_source=accepted
        )


def test_success_and_failure_require_at_least_one_attempt_and_cancellation_does_not() -> None:
    for terminal, accepted in ((_success(), _source()), (_failure(), None)):
        with pytest.raises(ContractSemanticError, match="must have executed at least once"):
            sem_jobs.validate_job_terminal_result(
                dataclasses.replace(terminal, attempts=()), accepted_import_source=accepted
            )
    sem_jobs.validate_job_terminal_result(_cancellation(), accepted_import_source=None)


def test_the_final_attempt_agrees_with_the_terminal_branch() -> None:
    with pytest.raises(ContractSemanticError, match="final attempt must be 'succeeded'"):
        sem_jobs.validate_job_terminal_result(
            _success(attempts=(_attempt(state="failed", error=_error()),)),
            accepted_import_source=_source(),
        )


def test_a_job_finished_when_its_last_attempt_did() -> None:
    with pytest.raises(ContractSemanticError, match="must equal the final attempt's finished_at"):
        sem_jobs.validate_job_terminal_result(
            _success(finished_at=T3), accepted_import_source=_source()
        )


def test_a_failed_terminal_result_reports_the_final_attempts_own_error() -> None:
    """Two spellings of one failure. The error that ended the last attempt *is* the error that
    ended the job, so a terminal failure carrying a different one leaves a caller unable to say
    which of the two is the outcome."""
    other = ApiError(code="internal_non_recoverable", message="other", retry_class="non_retryable")
    with pytest.raises(ContractSemanticError, match="not the error the final attempt ended with"):
        sem_jobs.validate_job_terminal_result(
            _failure(error=other), accepted_import_source=None
        )
    sem_jobs.validate_job_terminal_result(_failure(), accepted_import_source=None)


def test_a_cancellation_with_attempts_ends_on_its_cancelled_attempt() -> None:
    """Zero attempts is the pre-execution cancellation. Once a job has executed, its final
    attempt is the cancelled one, and it finished when the job did."""
    cancelled_attempt = _attempt(state="cancelled", finished_at=T2)
    sem_jobs.validate_job_terminal_result(
        _cancellation(attempts=(cancelled_attempt,)), accepted_import_source=None
    )
    with pytest.raises(ContractSemanticError, match="final attempt must be 'cancelled'"):
        sem_jobs.validate_job_terminal_result(
            _cancellation(attempts=(_attempt(state="succeeded"),)), accepted_import_source=None
        )
    with pytest.raises(ContractSemanticError, match="must equal the final attempt's finished_at"):
        sem_jobs.validate_job_terminal_result(
            _cancellation(attempts=(_attempt(state="cancelled", finished_at=T1),)),
            accepted_import_source=None,
        )


def test_import_success_is_bound_to_an_import_completion_result_in_both_directions() -> None:
    with pytest.raises(ContractSemanticError, match="must report 'import_completion'"):
        sem_jobs.validate_job_terminal_result(
            _success(result_kind="opaque.blob"), accepted_import_source=_source()
        )
    foreign = _success(
        identity=_identity(job_kind="search.reindex", originating_operation="search.reindex")
    )
    with pytest.raises(ContractSemanticError, match="appears only on a"):
        sem_jobs.validate_job_terminal_result(foreign, accepted_import_source=_source())


def test_a_non_import_job_may_still_report_an_unknown_result_kind() -> None:
    """Open vocabulary: a job this build has no typed shape for still decodes and validates,
    it simply carries a kind a caller must not interpret -- and it has no accepted import
    source to be checked against, which is the one case `None` states."""
    sem_jobs.validate_job_terminal_result(
        _success(
            identity=_identity(job_kind="search.reindex", originating_operation="search.reindex"),
            result_kind="future_kind.not_yet_known",
            result={"anything": True},
        ),
        accepted_import_source=None,
    )


def test_import_completion_accounts_for_every_discovered_item_exactly_once() -> None:
    with pytest.raises(ContractSemanticError, match="does not equal"):
        sem_jobs.validate_import_completion_result_shape(_completion(discovered_items=11))
    with pytest.raises(ContractSemanticError, match="does not equal"):
        sem_jobs.validate_import_completion_result_shape(_completion(skipped_items=2))


def test_import_completion_partial_is_exactly_failed_items_greater_than_zero() -> None:
    with pytest.raises(ContractSemanticError, match="partial must be exactly"):
        sem_jobs.validate_import_completion_result_shape(_completion(partial=True))
    with pytest.raises(ContractSemanticError, match="partial must be exactly"):
        sem_jobs.validate_import_completion_result_shape(
            _completion(evidence_records_created=5, failed_items=2, partial=False)
        )
    sem_jobs.validate_import_completion_result_shape(
        _completion(evidence_records_created=5, failed_items=2, partial=True)
    )


def test_import_completion_run_id_equals_the_job_id() -> None:
    with pytest.raises(ContractSemanticError, match="does not equal the job id"):
        sem_jobs.validate_import_completion_result(
            _completion(import_run_id="other"), job_id=JOB_ID, accepted_import_source=_source()
        )
    sem_jobs.validate_import_completion_result(
        _completion(), job_id=JOB_ID, accepted_import_source=_source()
    )


def test_import_completion_reports_the_immutable_descriptor_import_start_accepted() -> None:
    accepted = _source()
    drifted = _source(content_checksum="sha256:" + "0" * 64)
    with pytest.raises(ContractSemanticError, match="immutable descriptor import.start accepted"):
        sem_jobs.validate_import_completion_result(
            _completion(source=drifted), job_id=JOB_ID, accepted_import_source=accepted
        )
    sem_jobs.validate_import_completion_result(
        _completion(), job_id=JOB_ID, accepted_import_source=accepted
    )


@pytest.mark.parametrize(
    "drift",
    [
        {"staged_source_ref": "staged-other"},
        {"source_kind": "document"},
        {"content_checksum": "sha256:" + "1" * 64},
        {"content_length_bytes": 2049},
        {"media_type": "application/x-tar"},
        {"source_version": "v4"},
        {"source_version": None},
    ],
    ids=[
        "staged_source_ref",
        "source_kind",
        "content_checksum",
        "content_length_bytes",
        "media_type",
        "source_version",
        "source_version-dropped",
    ],
)
def test_every_field_of_the_accepted_source_is_part_of_the_immutability_proof(
    drift: dict[str, Any],
) -> None:
    """Immutability is not "the handle matches". Every field the descriptor carries is part of
    what was accepted, so a report that agrees on the staged handle and disagrees on the bytes
    it resolved to, the type it was read as, or the version it named is a report about a
    different source."""
    with pytest.raises(ContractSemanticError, match="immutable descriptor import.start accepted"):
        sem_jobs.validate_import_completion_result(
            _completion(source=_source(**drift)),
            job_id=JOB_ID,
            accepted_import_source=_source(),
        )


def test_a_full_import_completion_check_refuses_to_run_without_its_trusted_context() -> None:
    """The repaired shape: `job_id` and `accepted_import_source` are arguments, not options. A
    completion checked against nothing is self-asserted, and the failure mode of an optional
    argument is that it is omitted."""
    with pytest.raises(ContractSemanticError, match="job_id: the job that performed this run"):
        sem_jobs.validate_import_completion_result(
            _completion(), job_id=None, accepted_import_source=_source()
        )
    with pytest.raises(ContractSemanticError, match="accepted_import_source"):
        sem_jobs.validate_import_completion_result(
            _completion(), job_id=JOB_ID, accepted_import_source=None
        )
    with pytest.raises(TypeError):
        sem_jobs.validate_import_completion_result(_completion())  # type: ignore[call-arg]


def test_import_success_is_validated_as_an_import_completion_through_the_terminal_result() -> None:
    broken = _success(result=_completion(discovered_items=99).to_wire())
    with pytest.raises(ContractSemanticError, match="does not equal"):
        sem_jobs.validate_job_terminal_result(broken, accepted_import_source=_source())
    with pytest.raises(ContractSemanticError, match="immutable descriptor"):
        sem_jobs.validate_job_terminal_result(
            _success(), accepted_import_source=_source(staged_source_ref="staged-other")
        )


def test_an_import_success_without_a_trusted_accepted_source_is_refused() -> None:
    """The counterexample the optional keyword allowed: a caller that simply does not pass the
    accepted descriptor used to get a fully validated import success whose reported source
    nothing had checked. `None` is now a rejection for this branch, and omitting the argument
    entirely is a `TypeError` at the call site rather than a silent downgrade."""
    with pytest.raises(ContractSemanticError, match="accepted_import_source"):
        sem_jobs.validate_job_terminal_result(_success(), accepted_import_source=None)
    with pytest.raises(TypeError):
        sem_jobs.validate_job_terminal_result(_success())  # type: ignore[call-arg]
    with pytest.raises(ContractSemanticError, match="accepted_import_source"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=_handle(state="succeeded"), terminal_result=_success()),
            accepted_import_source=None,
        )


# --- the run id lives in the job id's own domain --------------------------------

OPAQUE_JOB_ID = "job/opaque-token"


def test_a_job_id_outside_the_identifier_vocabulary_is_still_a_valid_job_id() -> None:
    """`job_id` is an `OpaqueToken`: bounded, printable ASCII, and parsed by nobody. The
    equality `import_run_id == job_id` therefore has to hold in *that* domain. Typed as the
    narrower `Identifier`, a run id could not even spell this job's id, so the equality the
    contract states would have been unstateable for a whole class of legitimate jobs."""
    _assert_schema_valid("OpaqueToken", OPAQUE_JOB_ID, schema_file="common")
    _assert_schema_invalid("Identifier", OPAQUE_JOB_ID, schema_file="common")


def test_an_opaque_job_id_carries_through_handle_completion_and_terminal_result() -> None:
    identity = _identity(OPAQUE_JOB_ID)
    completion = _completion(import_run_id=OPAQUE_JOB_ID)
    terminal = _success(identity=identity, result=completion.to_wire())
    handle = _handle(state="succeeded", identity=identity)

    _assert_schema_valid("JobHandle", handle.to_wire())
    _assert_schema_valid("ImportCompletionResult", completion.to_wire())
    _assert_schema_valid("JobTerminalResult", terminal.to_wire())

    sem_jobs.validate_job_handle(handle)
    sem_jobs.validate_import_completion_result(
        completion, job_id=OPAQUE_JOB_ID, accepted_import_source=_source()
    )
    sem_jobs.validate_job_get_result(
        JobGetResult(job=handle, terminal_result=terminal),
        JobGetInput(job_id=OPAQUE_JOB_ID),
        accepted_import_source=_source(),
    )
    # And the equality is still enforced in the wider domain, not merely permitted.
    with pytest.raises(ContractSemanticError, match="does not equal the job id"):
        sem_jobs.validate_import_completion_result(
            _completion(import_run_id="job/other-token"),
            job_id=OPAQUE_JOB_ID,
            accepted_import_source=_source(),
        )


def test_the_generated_run_id_type_matches_the_job_id_type_in_both_languages() -> None:
    """Generation parity for the same fact: a run id a caller cannot spell in Python or
    TypeScript is a contract that only reads correctly."""
    assert ImportCompletionResult.__annotations__["import_run_id"] == "OpaqueToken"
    assert JobIdentity.__annotations__["job_id"] == "OpaqueToken"
    typescript = (REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts").read_text(
        encoding="utf-8"
    )
    assert "readonly import_run_id: OpaqueToken;" in typescript
    assert "readonly job_id: OpaqueToken;" in typescript


def test_import_completion_reports_l0_evidence_only() -> None:
    """The count is of evidence artifacts, never of governed records: nothing in this result
    asserts that knowledge was proposed, approved, or accepted."""
    field_names = {field.name for field in dataclasses.fields(ImportCompletionResult)}
    assert "evidence_records_created" in field_names
    assert not {
        name
        for name in field_names
        if "governed" in name or "record" in name and name != "evidence_records_created"
    } - {"import_run_id"}


def _evidence_artifact_wire(**overrides: Any) -> dict[str, Any]:
    """One valid L0 `EvidenceArtifact` document, so a test can say exactly what it changes.

    Built here rather than imported from the governed-knowledge suite deliberately: the fact
    under test is the *job* side's promise about what the evidence it produced may name, and
    it has to be provable from this suite alone.
    """
    document: dict[str, Any] = {
        "evidence_id": "ev-1",
        "workspace_id": WORKSPACE_ID,
        "source": {"kind": "archive", "source_id": "doc-1"},
        "temporal": {"ingested_at": T0, "recorded_at": T0},
        "content_checksum": CHECKSUM,
        "media_type": "text/plain",
        "metadata": {},
        "permission_labels": [],
        "sensitivity": "public",
        "tombstoned": False,
        "parser_status": "parsed",
        "ingestion_status": "ingested",
        "provenance_history": [
            {"actor_id": "actor-1", "actor_kind": "service", "action": "created", "occurred_at": T0}
        ],
    }
    document.update(overrides)
    return document


def test_an_opaque_run_id_reaches_the_evidence_backlink_the_completion_promises() -> None:
    """`ImportCompletionResult` promises the L0 evidence this run produced points back at the
    run through `EvidenceArtifact.import_run_id`. That promise is keepable only if the backlink
    lives in the *same* token domain as the run id it names: typed any narrower, a job that
    legitimately completed under `job/opaque-token` would hold a run id no artifact could ever
    record, and the traceability the contract states would silently not exist for it.

    Proven along the whole path the one token has to travel: strict schema at both ends, the
    generated Python and TypeScript shapes, and public semantic validation on both sides.
    """
    completion = _completion(import_run_id=OPAQUE_JOB_ID)
    artifact_wire = _evidence_artifact_wire(import_run_id=OPAQUE_JOB_ID)

    _assert_schema_valid("OpaqueToken", OPAQUE_JOB_ID, schema_file="common")
    _assert_schema_valid("ImportCompletionResult", completion.to_wire())
    _assert_schema_valid("EvidenceArtifact", artifact_wire, schema_file="evidence")

    artifact = EvidenceArtifact.from_wire(artifact_wire)
    assert artifact.import_run_id == OPAQUE_JOB_ID
    assert artifact.to_wire()["import_run_id"] == OPAQUE_JOB_ID
    assert EvidenceArtifact.__annotations__["import_run_id"] == "OpaqueToken | None"
    typescript = (REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts").read_text(
        encoding="utf-8"
    )
    assert "readonly import_run_id?: OpaqueToken;" in typescript

    sem_jobs.validate_import_completion_result(
        completion, job_id=OPAQUE_JOB_ID, accepted_import_source=_source()
    )
    sem_evidence.validate_evidence_artifact(artifact)


@pytest.mark.parametrize(
    "malformed",
    ["", "job token", "job\ttoken", "job/tokené", "j" * 513],
)
def test_a_malformed_import_run_id_is_still_refused_on_the_evidence_side(malformed: str) -> None:
    """Widening the backlink to the run id's own domain widens it to `OpaqueToken` and no
    further: empty, whitespace-bearing, control-bearing, non-ASCII, and over-length spellings
    are each still refused, by strict schema and by semantic validation alike."""
    wire = _evidence_artifact_wire(import_run_id=malformed)
    _assert_schema_invalid("EvidenceArtifact", wire, schema_file="evidence")
    with pytest.raises(ContractSemanticError, match="import_run_id"):
        sem_evidence.validate_evidence_artifact(EvidenceArtifact.from_wire(wire))


def test_the_evidence_backlink_widening_leaves_every_other_artifact_invariant_intact() -> None:
    """An artifact carrying a perfectly valid opaque run id is still held to everything else it
    was: a run id is not a licence to skip the artifact's own integrity."""
    sem_evidence.validate_evidence_artifact(
        EvidenceArtifact.from_wire(_evidence_artifact_wire(import_run_id=OPAQUE_JOB_ID))
    )
    for field, value, expected in (
        ("evidence_id", "-not-an-evidence-id", "evidence_id"),
        ("content_checksum", "not-a-checksum", "content_checksum"),
        ("provenance_history", [], "provenance_history"),
        ("tombstoned", True, "tombstoned"),
    ):
        wire = _evidence_artifact_wire(import_run_id=OPAQUE_JOB_ID, **{field: value})
        with pytest.raises(ContractSemanticError, match=expected):
            sem_evidence.validate_evidence_artifact(EvidenceArtifact.from_wire(wire))


# --- the opaque-token domain is one domain, anchored the same way in both halves ---
#
# `OpaqueToken` is enforced twice over: by the frozen JSON Schema `pattern`, and by the
# public semantic validators, which match the *whole* string. Those two halves have to
# admit exactly the same tokens, or the contract has two answers to "is this a token?"
# and a caller's choice of validator decides which one it gets.
#
# A bare `^[!-~]+$` does not give that. `$` is an end-of-*line* anchor in a conforming
# regex engine, matching before a final line terminator as well as at end of input, so
# `"job/opaque-token\n"` satisfied the schema while the semantic validators refused it.
# The pattern therefore ends in a negative lookahead asserting no character follows,
# which pins the anchor to absolute end of input without touching the character class:
# the domain is still exactly printable ASCII, still bounded 1..512.


OPAQUE_TOKEN_SPELLINGS: tuple[tuple[str, str, bool], ...] = (
    ("printable", OPAQUE_JOB_ID, True),
    ("shortest", "j", True),
    ("longest", "j" * 512, True),
    ("trailing_lf", OPAQUE_JOB_ID + "\n", False),
    ("trailing_crlf", OPAQUE_JOB_ID + "\r\n", False),
    ("trailing_cr", OPAQUE_JOB_ID + "\r", False),
    ("embedded_lf", "job/\nopaque-token", False),
    ("leading_lf", "\n" + OPAQUE_JOB_ID, False),
    ("lf_only", "\n", False),
    ("empty", "", False),
    ("space", "job token", False),
    ("tab", "job\ttoken", False),
    ("control", "job\x01token", False),
    ("non_ascii", "job/tokené", False),
    ("over_length", "j" * 513, False),
)


def _opaque_token_agreement(token: str) -> dict[str, tuple[bool, bool]]:
    """Return, per public `OpaqueToken` field, `(strict schema accepts, semantics accept)`.

    One token spelling is pushed down all three fields the A2.4 slice publishes in this
    domain, through both enforcement halves, so a disagreement is reported as the pair it
    is rather than as whichever half a test happened to call first.
    """
    identity = _identity(token)
    completion = _completion(import_run_id=token)
    artifact_wire = _evidence_artifact_wire(import_run_id=token)

    def _schema_accepts(def_name: str, document: Any, schema_file: str) -> bool:
        return not list(_strict_validator(def_name, schema_file).iter_errors(document))

    def _semantics_accept(validate: Any) -> bool:
        try:
            validate()
        except ContractSemanticError:
            return False
        return True

    return {
        "OpaqueToken": (
            _schema_accepts("OpaqueToken", token, "common"),
            _semantics_accept(
                lambda: sem_jobs.validate_job_get_input(JobGetInput(job_id=token))
            ),
        ),
        "JobIdentity.job_id": (
            _schema_accepts("JobIdentity", identity.to_wire(), "jobs"),
            _semantics_accept(lambda: sem_jobs.validate_job_handle(_handle(identity=identity))),
        ),
        "ImportCompletionResult.import_run_id": (
            _schema_accepts("ImportCompletionResult", completion.to_wire(), "jobs"),
            _semantics_accept(
                lambda: sem_jobs.validate_import_completion_result(
                    completion, job_id=token, accepted_import_source=_source()
                )
            ),
        ),
        "EvidenceArtifact.import_run_id": (
            _schema_accepts("EvidenceArtifact", artifact_wire, "evidence"),
            _semantics_accept(
                lambda: sem_evidence.validate_evidence_artifact(
                    EvidenceArtifact.from_wire(artifact_wire)
                )
            ),
        ),
    }


@pytest.mark.parametrize(
    ("token", "accepted"),
    [pytest.param(token, accepted, id=name) for name, token, accepted in OPAQUE_TOKEN_SPELLINGS],
)
def test_strict_schema_and_semantics_admit_the_same_opaque_tokens(
    token: str, accepted: bool
) -> None:
    """Parity, proven field by field across the whole spelling table.

    Each cell is asserted twice on purpose: that the two halves *agree with each other*
    (no caller can pick a validator and get a different answer), and that they agree on
    the *right* answer (agreeing on a wrong one would be parity with no contract behind
    it). The three fields are checked together because they are one domain reached from
    three places -- `import_run_id` equals `job_id`, and the evidence backlink names that
    same run -- so a token any one of them refused would break the equality the completion
    contract states.
    """
    for field, (schema_accepts, semantics_accept) in _opaque_token_agreement(token).items():
        assert schema_accepts == semantics_accept, (
            f"{field}: strict schema and semantics disagree on {token!r} "
            f"(schema={schema_accepts}, semantics={semantics_accept})"
        )
        assert schema_accepts == accepted, f"{field}: {token!r} should be accepted={accepted}"


def test_a_trailing_newline_no_longer_slips_past_the_schema_anchor() -> None:
    """The exact regression, named on its own so it cannot be lost in a table.

    `"job/opaque-token\\n"` is not a token: a server never issued it, and a client that
    round-tripped it verbatim would be round-tripping a line, not an identifier. Before the
    anchor was pinned, strict schema accepted it at all three fields while every semantic
    validator refused it -- a job could be schema-valid and semantically impossible at once.
    """
    newline_token = OPAQUE_JOB_ID + "\n"
    for field, (schema_accepts, semantics_accept) in _opaque_token_agreement(
        newline_token
    ).items():
        assert not schema_accepts, f"{field}: schema still accepts a trailing newline"
        assert not semantics_accept, f"{field}: semantics unexpectedly accept a trailing newline"

    # And the fix is the anchor, not a narrowed character class: drop the newline and the
    # very same token is accepted everywhere.
    assert all(
        pair == (True, True) for pair in _opaque_token_agreement(OPAQUE_JOB_ID).values()
    )


def test_pinning_the_anchor_left_the_printable_ascii_domain_untouched() -> None:
    """The lookahead asserts about position, never about characters.

    Swept character by character over the whole Latin-1 range plus a sample beyond it, the
    schema pattern admits exactly `!`..`~` -- the same 94 printable ASCII characters it
    admitted before -- and the semantic regex admits exactly the same set.
    """
    validator = _strict_validator("OpaqueToken", "common")
    codepoints = [*range(0x100), 0x2028, 0x2029, 0x1F600]
    for codepoint in codepoints:
        character = chr(codepoint)
        printable_ascii = 0x21 <= codepoint <= 0x7E
        schema_accepts = not list(validator.iter_errors(character))
        semantics_accept = bool(re.compile(generated.OPAQUE_TOKEN_PATTERN).fullmatch(character))
        assert schema_accepts == printable_ascii, f"schema disagrees at U+{codepoint:04X}"
        assert semantics_accept == printable_ascii, f"semantics disagree at U+{codepoint:04X}"


def test_the_anchored_pattern_is_published_verbatim_in_both_generated_languages() -> None:
    """The frozen schema is the single source; the deterministic artifacts carry it exactly.

    A TypeScript caller compiling `OPAQUE_TOKEN_PATTERN` into a `RegExp` and a Python caller
    compiling the same constant have to reject the trailing newline for the same reason the
    schema does, so the constant is asserted to be the schema's own string in both.
    """
    schema_pattern = json.loads(
        (SCHEMA_DIR / "common.schema.json").read_text(encoding="utf-8")
    )["$defs"]["OpaqueToken"]["pattern"]
    assert schema_pattern == "^[!-~]+$(?![\\s\\S])"
    assert generated.OPAQUE_TOKEN_PATTERN == schema_pattern

    typescript = (REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts").read_text(
        encoding="utf-8"
    )
    assert (
        f"export const OPAQUE_TOKEN_PATTERN: string = {json.dumps(schema_pattern)};" in typescript
    )


def test_the_opaque_token_bound_is_still_exactly_one_to_five_hundred_and_twelve() -> None:
    """Anchoring changed where the pattern may stop matching, not how much it may match."""
    schema = json.loads((SCHEMA_DIR / "common.schema.json").read_text(encoding="utf-8"))["$defs"][
        "OpaqueToken"
    ]
    assert (schema["type"], schema["minLength"], schema["maxLength"]) == ("string", 1, 512)


# --------------------------------------------------------------------------
# import.start result / response-metadata job reference
# --------------------------------------------------------------------------


def test_import_start_result_binds_its_handle_to_the_response_job_reference() -> None:
    result = ImportStartResult(job=_handle(state="queued"))
    sem_jobs.validate_import_start_result(
        result, response_job_reference=JobReference(job_id=JOB_ID)
    )
    with pytest.raises(ContractSemanticError, match="does not equal the response metadata"):
        sem_jobs.validate_import_start_result(
            result, response_job_reference=JobReference(job_id="job-2")
        )


def test_import_start_result_requires_the_response_job_reference() -> None:
    with pytest.raises(ContractSemanticError, match="response_job_reference"):
        sem_jobs.validate_import_start_result(
            ImportStartResult(job=_handle(state="queued")), response_job_reference=None
        )


def test_import_start_returns_an_unfinished_ingestion_import_job() -> None:
    result = ImportStartResult(job=_handle(state="succeeded"))
    with pytest.raises(ContractSemanticError, match="has not reached"):
        sem_jobs.validate_import_start_result(
            result, response_job_reference=JobReference(job_id=JOB_ID)
        )
    foreign = ImportStartResult(
        job=_handle(state="queued", identity=_identity(job_kind="search.reindex"))
    )
    with pytest.raises(ContractSemanticError, match="job_kind must be"):
        sem_jobs.validate_import_start_result(
            foreign, response_job_reference=JobReference(job_id=JOB_ID)
        )


@pytest.mark.parametrize(
    "operation", ["job.get", "job.cancel", "job.retry", "job.events"]
)
def test_synchronous_job_operations_add_no_second_job_reference(operation: str) -> None:
    sem_jobs.validate_synchronous_job_response_job_reference(operation, None)
    with pytest.raises(ContractSemanticError, match="completes synchronously"):
        sem_jobs.validate_synchronous_job_response_job_reference(
            operation, JobReference(job_id=JOB_ID)
        )


def test_import_start_is_not_a_synchronous_job_operation() -> None:
    with pytest.raises(ContractSemanticError, match="always returns a job"):
        sem_jobs.validate_synchronous_job_response_job_reference("import.start", None)


# --------------------------------------------------------------------------
# job.get
# --------------------------------------------------------------------------


def test_job_get_reports_a_terminal_result_exactly_when_the_state_is_known_terminal() -> None:
    running = JobGetResult(job=_handle(state="running"))
    sem_jobs.validate_job_get_result(
        running, JobGetInput(job_id=JOB_ID), accepted_import_source=None
    )

    finished = JobGetResult(job=_handle(state="succeeded"), terminal_result=_success())
    sem_jobs.validate_job_get_result(
        finished, JobGetInput(job_id=JOB_ID), accepted_import_source=_source()
    )

    with pytest.raises(ContractSemanticError, match="must report its terminal_result"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=_handle(state="succeeded")), accepted_import_source=_source()
        )
    with pytest.raises(ContractSemanticError, match="does not know to be terminal"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=_handle(state="running"), terminal_result=_success()),
            accepted_import_source=_source(),
        )


def test_job_get_never_reports_a_terminal_result_for_an_unknown_state() -> None:
    handle = _handle(state="future.unseen", cancellation="not_cancellable", recovery="not_retryable")
    sem_jobs.validate_job_get_result(JobGetResult(job=handle), accepted_import_source=None)
    with pytest.raises(ContractSemanticError, match="does not know to be terminal"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=handle, terminal_result=_success()),
            accepted_import_source=_source(),
        )


def test_job_get_handle_and_terminal_result_describe_the_same_job() -> None:
    other = _failure(identity=_identity("job-2"))
    with pytest.raises(ContractSemanticError, match="identifies a different job"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=_handle(state="failed"), terminal_result=other),
            accepted_import_source=None,
        )
    with pytest.raises(ContractSemanticError, match="disagrees with"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=_handle(state="succeeded"), terminal_result=_cancellation()),
            accepted_import_source=None,
        )


def test_job_get_answers_the_job_that_was_asked_for() -> None:
    with pytest.raises(ContractSemanticError, match="does not answer the requested job"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=_handle(state="running")),
            JobGetInput(job_id="job-2"),
            accepted_import_source=None,
        )


# --- job.get: the terminal history is closed against the handle it accompanies ---


def test_job_get_binds_the_handles_latest_attempt_to_the_final_terminal_attempt() -> None:
    """A handle and the history beside it describe the same executions. A handle still showing
    attempt 1 next to a history that ends at attempt 2 states two irreconcilable accounts of
    the same job, and neither tells a caller which attempt actually produced the outcome."""
    first = _attempt(1, started_at=T0, finished_at=T1, state="failed", error=_error())
    second = _attempt(2, started_at=T1, finished_at=T2, state="succeeded")
    handle = _handle(state="succeeded", latest_attempt=second)
    terminal = _success(attempts=(first, second))
    sem_jobs.validate_job_get_result(
        JobGetResult(job=handle, terminal_result=terminal), accepted_import_source=_source()
    )

    stale = _handle(
        state="succeeded",
        latest_attempt=_attempt(1, started_at=T0, finished_at=T2, state="succeeded"),
    )
    with pytest.raises(ContractSemanticError, match="not the final attempt of"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=stale, terminal_result=terminal), accepted_import_source=_source()
        )


def test_job_get_rejects_a_latest_attempt_that_merely_renumbers_the_final_one() -> None:
    """Equality, not "same number": a handle whose attempt agrees on the ordinal and disagrees
    on when it ran or how it ended is the same contradiction wearing a matching label."""
    rewritten = _attempt(1, started_at=T1, finished_at=T2, state="succeeded")
    with pytest.raises(ContractSemanticError, match="not the final attempt of"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=_handle(state="succeeded", latest_attempt=rewritten),
                         terminal_result=_success()),
            accepted_import_source=_source(),
        )


def test_job_get_binds_an_empty_terminal_history_to_a_handle_with_no_attempt() -> None:
    """A pre-execution cancellation has no attempt anywhere: not in the history, and not on
    the handle either."""
    handle = _handle(state="cancelled", latest_attempt=None)
    sem_jobs.validate_job_get_result(
        JobGetResult(job=handle, terminal_result=_cancellation()), accepted_import_source=None
    )
    executed = _handle(
        state="cancelled", latest_attempt=_attempt(state="cancelled", finished_at=T2)
    )
    with pytest.raises(ContractSemanticError, match="not the final attempt of"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=executed, terminal_result=_cancellation()),
            accepted_import_source=None,
        )


def test_job_get_bounds_the_terminal_history_by_the_handles_own_lifetime() -> None:
    """A job cannot have executed before it existed. The escape this pins is invisible to the
    handle alone: the handle's own `latest_attempt` sits comfortably inside the lifetime, and
    it is the *earlier* attempt in the history -- an attempt no handle ever carried -- that
    claims to have started before the job was created."""
    escaped = _attempt(
        1, started_at="2026-07-29T23:00:00Z", finished_at=T1, state="failed", error=_error()
    )
    final = _attempt(2, started_at=T1, finished_at=T2, state="succeeded")
    handle = _handle(state="succeeded", latest_attempt=final)
    with pytest.raises(
        ContractSemanticError, match=r"attempts\[0\].started_at precedes the job's created_at"
    ):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=handle, terminal_result=_success(attempts=(escaped, final))),
            accepted_import_source=_source(),
        )


def test_a_terminal_history_never_reaches_past_the_lifetime_it_was_given() -> None:
    """The other end of the same bound, stated directly against the validator that takes it."""
    with pytest.raises(ContractSemanticError, match="finished_at follows the job's updated_at"):
        sem_jobs.validate_job_terminal_result(
            _success(attempts=(_attempt(state="succeeded", finished_at=T3),), finished_at=T3),
            accepted_import_source=_source(),
            created_at=T0,
            updated_at=T2,
        )


def test_job_get_rejects_a_terminal_failure_whose_error_is_not_the_final_attempts() -> None:
    other = ApiError(code="internal_non_recoverable", message="other", retry_class="non_retryable")
    with pytest.raises(ContractSemanticError, match="not the error the final attempt ended with"):
        sem_jobs.validate_job_get_result(
            JobGetResult(job=_handle(state="failed"), terminal_result=_failure(error=other)),
            accepted_import_source=None,
        )


# --------------------------------------------------------------------------
# Control dispositions: refusal is a successful, idempotent result
# --------------------------------------------------------------------------


def test_an_accepted_cancellation_is_checked_against_the_handle_it_acted_on() -> None:
    running = _handle(state="running", cancellation="cancellable")
    accepted = JobCancelResult(
        job=_handle(state="running", cancellation="cancellation_pending", updated_at=T3),
        cancellation_disposition="cancellation_requested",
    )
    assert (
        sem_jobs.validate_job_cancel_result(
            accepted, JobCancelInput(job_id=JOB_ID), previous=running
        )
        == "cancellation_requested"
    )

    # Cancellable when the call was made, but the job finished before it took effect: the
    # honest disposition is then a refusal, not an acceptance of something that never stopped.
    with pytest.raises(ContractSemanticError, match="already finished"):
        sem_jobs.validate_job_cancel_result(
            JobCancelResult(
                job=_handle(state="succeeded", updated_at=T3),
                cancellation_disposition="cancellation_requested",
            ),
            previous=running,
        )


def test_an_accepted_cancellation_leaves_a_handle_that_is_no_longer_cancellable() -> None:
    """A cancellation that was accepted and comes back still advertising `cancellable` has
    recorded nothing: the next caller reads the same permission and asks again."""
    running = _handle(state="running", cancellation="cancellable")
    still_cancellable = JobCancelResult(
        job=_handle(state="running", cancellation="cancellable", updated_at=T3),
        cancellation_disposition="cancellation_requested",
    )
    with pytest.raises(ContractSemanticError, match="still reports 'cancellable'"):
        sem_jobs.validate_job_cancel_result(still_cancellable, previous=running)


@pytest.mark.parametrize(
    "previous_state", ["succeeded", "failed", "cancelled"], ids=["succeeded", "failed", "cancelled"]
)
def test_a_finished_job_is_never_rewound_into_a_fresh_cancellation(previous_state: str) -> None:
    """`cancellation_requested` says a job that had not finished has been asked to stop. A
    succeeded, failed, or already-cancelled job offers no cancellation on its handle, and the
    availability is what the acceptance is checked against -- not the disposition's own word."""
    previous = _handle(state=previous_state)
    result = JobCancelResult(
        job=_handle(state=previous_state, updated_at=T3),
        cancellation_disposition="cancellation_requested",
    )
    with pytest.raises(ContractSemanticError, match="only 'cancellable' accepts a cancellation"):
        sem_jobs.validate_job_cancel_result(result, previous=previous)


def test_an_already_pending_cancellation_is_not_a_fresh_acceptance() -> None:
    """`cancellation_pending` is an answer, not a permission: the job was asked once already,
    and a second acceptance would record a control transition that never happened."""
    pending = _handle(state="running", cancellation="cancellation_pending")
    result = JobCancelResult(
        job=_handle(state="running", cancellation="cancellation_pending", updated_at=T3),
        cancellation_disposition="cancellation_requested",
    )
    with pytest.raises(ContractSemanticError, match="only 'cancellable' accepts a cancellation"):
        sem_jobs.validate_job_cancel_result(result, previous=pending)


def test_an_already_cancelled_disposition_reports_a_call_that_changed_nothing() -> None:
    """`cancelled` means the job was found already cancelled, so it is a refusal-shaped,
    idempotent outcome: it owes the same unchanged-handle proof `not_cancellable` owes, and it
    cannot be reported for a job that is not in fact cancelled."""
    cancelled = _handle(state="cancelled", latest_attempt=None)
    already = JobCancelResult(job=cancelled, cancellation_disposition="cancelled")
    assert sem_jobs.validate_job_cancel_result(already, previous=cancelled) == "cancelled"

    with pytest.raises(ContractSemanticError, match="already cancelled"):
        sem_jobs.validate_job_cancel_result(
            JobCancelResult(job=_handle(state="running"), cancellation_disposition="cancelled"),
            previous=_handle(state="running"),
        )
    changed = JobCancelResult(
        job=_handle(state="cancelled", latest_attempt=None, updated_at=T3),
        cancellation_disposition="cancelled",
    )
    with pytest.raises(ContractSemanticError, match="returns the current handle unchanged"):
        sem_jobs.validate_job_cancel_result(changed, previous=cancelled)


@pytest.mark.parametrize("disposition", ["not_cancellable", "future.unseen"])
def test_a_refused_cancellation_returns_the_current_handle_unchanged(disposition: str) -> None:
    previous = _handle(state="succeeded")
    refused = JobCancelResult(job=previous, cancellation_disposition=disposition)
    assert sem_jobs.validate_job_cancel_result(refused, previous=previous) == disposition
    changed = JobCancelResult(
        job=_handle(state="succeeded", updated_at=T3), cancellation_disposition=disposition
    )
    with pytest.raises(ContractSemanticError, match="returns the current handle unchanged"):
        sem_jobs.validate_job_cancel_result(changed, previous=previous)


def test_a_full_control_validation_refuses_to_run_without_the_pre_mutation_handle() -> None:
    """The repaired shape. A control result states what *changed*, and what changed cannot be
    read from the new handle alone: `retry_scheduled` beside a queued job is either an honest
    recovery or a fabrication, and the two are identical documents. So `previous` is required
    on the full validators, and the structural-only readings are named for what they are."""
    result = JobCancelResult(
        job=_handle(state="running", cancellation="cancellation_pending"),
        cancellation_disposition="cancellation_requested",
    )
    with pytest.raises(ContractSemanticError, match="handle observed before this call"):
        sem_jobs.validate_job_cancel_result(result, previous=None)
    with pytest.raises(TypeError):
        sem_jobs.validate_job_cancel_result(result)  # type: ignore[call-arg]

    retry = JobRetryResult(
        job=_handle(state="queued", latest_attempt=None), recovery_disposition="retry_scheduled"
    )
    with pytest.raises(ContractSemanticError, match="handle observed before this call"):
        sem_jobs.validate_job_retry_result(retry, previous=None)
    with pytest.raises(TypeError):
        sem_jobs.validate_job_retry_result(retry)  # type: ignore[call-arg]

    # The structural readings remain available and are explicitly named as such.
    assert sem_jobs.validate_job_cancel_result_shape(result) == "cancellation_requested"
    assert sem_jobs.validate_job_retry_result_shape(retry) == "retry_scheduled"


def test_a_failed_job_yields_retry_scheduled_and_a_cancelled_resumable_one_resume_scheduled() -> None:
    failed = _handle(state="failed", recovery="retryable")
    queued_after_retry = _handle(
        state="queued",
        updated_at=T3,
        latest_attempt=_attempt(state="failed", error=_error()),
    )
    assert (
        sem_jobs.validate_job_retry_result(
            JobRetryResult(job=queued_after_retry, recovery_disposition="retry_scheduled"),
            JobRetryInput(job_id=JOB_ID),
            previous=failed,
        )
        == "retry_scheduled"
    )

    cancelled = _handle(state="cancelled", recovery="resumable", latest_attempt=None)
    queued_after_resume = _handle(state="queued", updated_at=T3, latest_attempt=None)
    assert (
        sem_jobs.validate_job_retry_result(
            JobRetryResult(job=queued_after_resume, recovery_disposition="resume_scheduled"),
            previous=cancelled,
        )
        == "resume_scheduled"
    )

    with pytest.raises(ContractSemanticError, match="'resume_scheduled' recovers a 'cancelled'"):
        sem_jobs.validate_job_retry_result(
            JobRetryResult(job=queued_after_retry, recovery_disposition="resume_scheduled"),
            previous=failed,
        )


@pytest.mark.parametrize(
    ("disposition", "previous_state", "previous_recovery", "expected"),
    [
        ("retry_scheduled", "cancelled", "resumable", "recovers a 'failed' job"),
        ("retry_scheduled", "queued", "not_retryable", "recovers a 'failed' job"),
        ("retry_scheduled", "failed", "not_retryable", "offered 'retryable'"),
        ("resume_scheduled", "failed", "retryable", "recovers a 'cancelled' job"),
        ("resume_scheduled", "queued", "not_retryable", "recovers a 'cancelled' job"),
        ("resume_scheduled", "cancelled", "not_retryable", "offered 'resumable'"),
    ],
    ids=[
        "retry-from-cancelled",
        "retry-from-queued",
        "retry-without-retryable",
        "resume-from-failed",
        "resume-from-queued",
        "resume-without-resumable",
    ],
)
def test_each_recovery_requires_its_own_exact_prior_state_and_availability(
    disposition: str, previous_state: str, previous_recovery: str, expected: str
) -> None:
    """The pairing is not a default the disposition may fall back on. A retry recovers a
    `failed` job its handle called `retryable`; a resume recovers a `cancelled` job its handle
    called `resumable`. Deriving the expected disposition from the prior state alone let a
    `resumable` handle yield `retry_scheduled`, and taking `permits_recovery` as sufficient let
    an availability that was never about this recovery grant it. Nothing recovers a queued job:
    it has not run yet, so there is nothing to recover from."""
    previous = _handle(
        state=previous_state,
        recovery=previous_recovery,
        latest_attempt=None if previous_state != "failed" else _attempt(state="failed", error=_error()),
    )
    queued = _handle(
        state="queued",
        updated_at=T3,
        latest_attempt=previous.latest_attempt,
    )
    with pytest.raises(ContractSemanticError, match=expected):
        sem_jobs.validate_job_retry_result(
            JobRetryResult(job=queued, recovery_disposition=disposition), previous=previous
        )


def test_an_accepted_recovery_queues_the_same_job_and_creates_no_attempt() -> None:
    failed = _handle(state="failed", recovery="retryable")
    with pytest.raises(ContractSemanticError, match="returns the job to 'queued'"):
        sem_jobs.validate_job_retry_result(
            JobRetryResult(job=_handle(state="running"), recovery_disposition="retry_scheduled"),
            previous=failed,
        )
    with_new_attempt = _handle(
        state="queued",
        updated_at=T3,
        latest_attempt=_attempt(2, started_at=T2, finished_at=T3, state="failed", error=_error()),
    )
    with pytest.raises(ContractSemanticError, match="creates no attempt"):
        sem_jobs.validate_job_retry_result(
            JobRetryResult(job=with_new_attempt, recovery_disposition="retry_scheduled"),
            previous=failed,
        )


def test_recovery_is_never_accepted_for_a_handle_that_refused_it() -> None:
    not_retryable = _handle(state="failed", recovery="not_retryable")
    queued = _handle(
        state="queued", updated_at=T3, latest_attempt=_attempt(state="failed", error=_error())
    )
    with pytest.raises(ContractSemanticError, match="offered 'retryable'"):
        sem_jobs.validate_job_retry_result(
            JobRetryResult(job=queued, recovery_disposition="retry_scheduled"),
            previous=not_retryable,
        )


def test_an_unknown_recovery_availability_grants_no_recovery() -> None:
    """Open vocabularies decode but never grant. A handle carrying an availability this build
    has never seen is preserved -- it is judged for shape only -- and it authorizes nothing:
    an acceptance resting on it is rejected exactly as one resting on `not_retryable` is."""
    unseen = _handle(state="failed", recovery="future.unseen")
    sem_jobs.validate_job_handle(unseen)
    assert not sem_jobs.permits_recovery(unseen.control.recovery)
    queued = _handle(
        state="queued", updated_at=T3, latest_attempt=_attempt(state="failed", error=_error())
    )
    with pytest.raises(ContractSemanticError, match="offered 'retryable'"):
        sem_jobs.validate_job_retry_result(
            JobRetryResult(job=queued, recovery_disposition="retry_scheduled"), previous=unseen
        )


def test_an_unknown_cancellation_availability_grants_no_cancellation() -> None:
    unseen = _handle(state="running", cancellation="future.unseen")
    sem_jobs.validate_job_handle(unseen)
    assert not sem_jobs.permits_cancellation(unseen.control.cancellation)
    with pytest.raises(ContractSemanticError, match="only 'cancellable' accepts a cancellation"):
        sem_jobs.validate_job_cancel_result(
            JobCancelResult(
                job=_handle(state="running", cancellation="cancellation_pending", updated_at=T3),
                cancellation_disposition="cancellation_requested",
            ),
            previous=unseen,
        )


@pytest.mark.parametrize(
    ("state", "cancellation", "recovery"),
    [
        ("succeeded", "cancellable", "not_retryable"),
        ("succeeded", "not_cancellable", "retryable"),
        ("running", "cancelled", "not_retryable"),
        ("failed", "not_cancellable", "resumable"),
        ("cancelled", "cancellable", "not_retryable"),
    ],
    ids=["succeeded-cancellable", "succeeded-retryable", "running-cancelled",
         "failed-resumable", "cancelled-cancellable"],
)
def test_a_known_state_still_rejects_a_known_contradictory_availability(
    state: str, cancellation: str, recovery: str
) -> None:
    """The other half of the fail-safe. Preserving an unrecognized value is not the same as
    accepting a recognized falsehood: this build knows what `cancellable` means and knows a
    succeeded job cannot offer it."""
    with pytest.raises(ContractSemanticError, match="may only report"):
        sem_jobs.validate_job_handle(
            _handle(state=state, cancellation=cancellation, recovery=recovery)
        )


@pytest.mark.parametrize("state", ["queued", "running", "succeeded", "failed", "cancelled"])
def test_withholding_cancellation_is_legal_in_every_state(state: str) -> None:
    """Cancellability is not a function of state alone, so `not_cancellable` never lies.

    A job whose *kind* is steered by its own operation rather than by `job.cancel` -- a
    `workflow.execute` job, cancelled through `workflow.control` -- withholds cancellation
    while queued, running and cancelled alike. The asymmetry with `cancellable` is the
    point: withholding a control can only under-promise, while offering one on a finished
    job promises what cannot exist.
    """
    sem_jobs.validate_job_handle(
        _handle(state=state, cancellation="not_cancellable", recovery="not_retryable")
    )


@pytest.mark.parametrize("disposition", ["not_retryable", "future.unseen"])
def test_a_refused_recovery_returns_the_current_handle_unchanged(disposition: str) -> None:
    previous = _handle(state="succeeded")
    refused = JobRetryResult(job=previous, recovery_disposition=disposition)
    assert sem_jobs.validate_job_retry_result(refused, previous=previous) == disposition
    with pytest.raises(ContractSemanticError, match="returns the current handle unchanged"):
        sem_jobs.validate_job_retry_result(
            JobRetryResult(
                job=_handle(state="succeeded", updated_at=T3), recovery_disposition=disposition
            ),
            previous=previous,
        )


def test_a_control_call_never_changes_a_jobs_identity() -> None:
    previous = _handle(state="failed", recovery="retryable")
    other = _handle(
        state="queued",
        identity=_identity("job-2"),
        updated_at=T3,
        latest_attempt=_attempt(state="failed", error=_error()),
    )
    with pytest.raises(ContractSemanticError, match="never changes a job's identity"):
        sem_jobs.validate_job_retry_result(
            JobRetryResult(job=other, recovery_disposition="retry_scheduled"), previous=previous
        )


def test_a_replay_preserves_disposition_and_identity_over_an_evolving_job() -> None:
    """The job goes on evolving between the recorded response and the replay. A cancellation
    that was `cancellation_requested` while the job ran may since have taken effect, and the
    replay reports that truth: it is checked as a legal *observation* of the same job, not for
    equality with the instant the first call returned."""
    running_attempt = _attempt(finished_at=None, state="running")
    first = JobCancelResult(
        job=_handle(state="running", cancellation="cancellation_pending"),
        cancellation_disposition="cancellation_requested",
    )
    later = JobCancelResult(
        job=_handle(
            state="cancelled",
            updated_at=T3,
            latest_attempt=dataclasses.replace(
                running_attempt, finished_at=T3, state="cancelled"
            ),
        ),
        cancellation_disposition="cancellation_requested",
    )
    sem_jobs.validate_job_control_replay(first, later)

    with pytest.raises(ContractSemanticError, match="preserves the semantic disposition"):
        sem_jobs.validate_job_control_replay(
            first, dataclasses.replace(later, cancellation_disposition="not_cancellable")
        )
    with pytest.raises(ContractSemanticError, match="names the same job"):
        sem_jobs.validate_job_control_replay(
            first,
            dataclasses.replace(
                later,
                job=dataclasses.replace(later.job, identity=_identity("job-2")),
            ),
        )


def test_a_replay_compares_the_whole_immutable_identity_not_just_the_job_id() -> None:
    """A job id is one member of an identity that is immutable in full. A replay answering
    under the same id but a drifted job kind, originating operation, audit reference, or
    workspace is a different job wearing a familiar name -- and the audit linkage is exactly
    what a replay must not be able to swap."""
    first = JobCancelResult(
        job=_handle(state="cancelled", latest_attempt=None),
        cancellation_disposition="cancelled",
    )
    for drift in (
        {"job_kind": "search.reindex"},
        {"originating_operation": "search.reindex"},
    ):
        replay = dataclasses.replace(
            first, job=dataclasses.replace(first.job, identity=_identity(**drift))
        )
        with pytest.raises(ContractSemanticError, match="names the same job"):
            sem_jobs.validate_job_control_replay(first, replay)
    drifted_audit = dataclasses.replace(
        first,
        job=dataclasses.replace(
            first.job, identity=dataclasses.replace(first.job.identity, audit_reference="audit-2")
        ),
    )
    with pytest.raises(ContractSemanticError, match="names the same job"):
        sem_jobs.validate_job_control_replay(first, drifted_audit)


def test_an_honest_retry_replay_survives_the_job_starting_to_run() -> None:
    """The failure the repair had to avoid while closing the hole: re-applying the *original*
    call's transition rules to the replay observation would reject this, because a running job
    at attempt 2 is not what an accepted recovery returns. It is, however, exactly what an
    honest replay of that recovery reports a moment later."""
    retained = _attempt(state="failed", error=_error())
    recorded = JobRetryResult(
        job=_handle(state="queued", updated_at=T2, latest_attempt=retained),
        recovery_disposition="retry_scheduled",
    )
    running = JobRetryResult(
        job=_handle(
            state="running",
            updated_at=T3,
            latest_attempt=_attempt(2, started_at=T2, finished_at=None, state="running"),
        ),
        recovery_disposition="retry_scheduled",
    )
    sem_jobs.validate_job_control_replay(recorded, running)

    terminal = JobRetryResult(
        job=_handle(
            state="succeeded",
            updated_at=T3,
            latest_attempt=_attempt(2, started_at=T2, finished_at=T3, state="succeeded"),
        ),
        recovery_disposition="retry_scheduled",
    )
    sem_jobs.validate_job_control_replay(recorded, terminal)


def test_a_replay_never_schedules_a_second_recovery_or_starts_an_attempt() -> None:
    """Still queued, but a different latest attempt: the job did not run (it is queued), so a
    new attempt beside an unchanged state is execution the replay claims to have started."""
    retained = _attempt(state="failed", error=_error())
    recorded = JobRetryResult(
        job=_handle(state="queued", updated_at=T2, latest_attempt=retained),
        recovery_disposition="retry_scheduled",
    )
    reattempted = JobRetryResult(
        job=_handle(
            state="queued",
            updated_at=T3,
            latest_attempt=_attempt(2, started_at=T2, finished_at=T3, state="failed", error=_error()),
        ),
        recovery_disposition="retry_scheduled",
    )
    with pytest.raises(ContractSemanticError, match="starts no execution"):
        sem_jobs.validate_job_control_replay(recorded, reattempted)


def test_a_replay_never_drives_a_second_transition_or_rewinds_the_job() -> None:
    """A replay performs no mutation, so the one recovery that already happened is not
    re-granted: a replay observation that has gone terminal and back to `queued` is a second
    recovery, and one that unwinds a state is not an observation of the same job at all."""
    retained = _attempt(state="failed", error=_error())
    recorded = JobRetryResult(
        job=_handle(state="queued", updated_at=T2, latest_attempt=retained),
        recovery_disposition="retry_scheduled",
    )
    failed_again = JobRetryResult(
        job=_handle(state="failed", updated_at=T3, latest_attempt=retained),
        recovery_disposition="retry_scheduled",
    )
    sem_jobs.validate_job_control_replay(recorded, failed_again)

    # The recorded call refused; a replay of it that shows the job back in `queued` is a
    # recovery that happened between the two, and the replay declares none.
    refused = JobRetryResult(
        job=_handle(state="failed", recovery="not_retryable", updated_at=T2),
        recovery_disposition="not_retryable",
    )
    recovered = JobRetryResult(
        job=_handle(state="queued", updated_at=T3, latest_attempt=retained),
        recovery_disposition="not_retryable",
    )
    with pytest.raises(ContractSemanticError, match="no accepted job.retry was declared"):
        sem_jobs.validate_job_control_replay(refused, recovered)

    regressed = JobCancelResult(
        job=_handle(state="running", cancellation="cancellation_pending", updated_at=T2),
        cancellation_disposition="cancellation_requested",
    )
    rewound = JobCancelResult(
        job=_handle(state="queued", updated_at=T3, latest_attempt=None),
        cancellation_disposition="cancellation_requested",
    )
    with pytest.raises(ContractSemanticError, match="may not move from"):
        sem_jobs.validate_job_control_replay(regressed, rewound)


def test_a_replay_rejects_a_progress_regression() -> None:
    counted = JobCancelResult(
        job=_handle(
            state="running",
            cancellation="cancellation_pending",
            progress=JobProgress(unit="item", completed_units=7, total_units=10),
        ),
        cancellation_disposition="cancellation_requested",
    )
    regressed = JobCancelResult(
        job=_handle(
            state="running",
            cancellation="cancellation_pending",
            updated_at=T3,
            progress=JobProgress(unit="item", completed_units=3, total_units=10),
        ),
        cancellation_disposition="cancellation_requested",
    )
    with pytest.raises(ContractSemanticError, match="completed_units fell"):
        sem_jobs.validate_job_control_replay(counted, regressed)


def test_a_replay_of_import_start_starts_no_second_job() -> None:
    first = ImportStartResult(job=_handle(state="queued", latest_attempt=None, updated_at=T0))
    later = ImportStartResult(job=_handle(state="running", updated_at=T3))
    sem_jobs.validate_job_control_replay(first, later)
    with pytest.raises(ContractSemanticError, match="names the same job"):
        sem_jobs.validate_job_control_replay(
            first,
            ImportStartResult(
                job=_handle(
                    state="queued", latest_attempt=None, identity=_identity("job-2"), updated_at=T0
                )
            ),
        )


def test_a_replay_answers_the_same_operation() -> None:
    with pytest.raises(ContractSemanticError, match="answers the same operation"):
        sem_jobs.validate_job_control_replay(
            JobCancelResult(job=_handle(state="cancelled"), cancellation_disposition="cancelled"),
            JobRetryResult(job=_handle(state="queued", latest_attempt=None), recovery_disposition="not_retryable"),
        )


# --------------------------------------------------------------------------
# job.events: snapshot stability and adversarial page bindings
# --------------------------------------------------------------------------


def _validate_events(
    result: JobEventsResult,
    request: JobEventsInput,
    *,
    request_binding: Any = None,
    result_binding: Any = None,
) -> None:
    sem_jobs.validate_job_events_result(
        result,
        request,
        principal_id=PRINCIPAL_ID,
        workspace_id=WORKSPACE_ID,
        request_binding=request_binding,
        result_binding=result_binding,
    )


def test_a_first_page_starts_at_zero_and_offers_a_continuation_while_more_remains() -> None:
    _validate_events(
        _events_result(_events(0, 1), token="tok-2"),
        JobEventsInput(job_id=JOB_ID, limit=2),
        result_binding=_binding(next_sequence=2),
    )


def test_a_final_page_exhausts_the_snapshot_and_offers_no_continuation() -> None:
    _validate_events(
        _events_result(_events(2, 3)),
        JobEventsInput(job_id=JOB_ID, limit=2, page=PageMetadata(continuation_token="tok-2")),
        request_binding=_binding(next_sequence=2),
    )


def test_an_empty_snapshot_is_exhausted_immediately() -> None:
    _validate_events(_events_result((), snapshot=0), JobEventsInput(job_id=JOB_ID))


def test_a_page_is_contiguous_from_the_position_it_continued_from() -> None:
    for events in (_events(1, 2), _events(0, 2), _events(1, 0), _events(0, 0)):
        with pytest.raises(ContractSemanticError, match="a page is contiguous"):
            _validate_events(
                _events_result(events, token="tok"),
                JobEventsInput(job_id=JOB_ID),
                result_binding=_binding(next_sequence=2),
            )


def test_an_event_recorded_after_the_snapshot_never_appears_in_the_session() -> None:
    with pytest.raises(ContractSemanticError, match="falls outside the captured snapshot"):
        _validate_events(
            _events_result(_events(0, 1, 2, 3, 4), snapshot=4),
            JobEventsInput(job_id=JOB_ID),
        )


def test_a_continuation_is_offered_exactly_when_more_of_the_snapshot_remains() -> None:
    with pytest.raises(ContractSemanticError, match="must carry a continuation_token"):
        _validate_events(_events_result(_events(0, 1)), JobEventsInput(job_id=JOB_ID))
    with pytest.raises(ContractSemanticError, match="offers a continuation while"):
        _validate_events(
            _events_result(_events(0, 1, 2, 3), token="tok"),
            JobEventsInput(job_id=JOB_ID),
            result_binding=_binding(next_sequence=4),
        )


def test_a_page_that_returns_nothing_can_never_exhaust_the_session() -> None:
    with pytest.raises(ContractSemanticError, match="can never exhaust the session"):
        _validate_events(_events_result((), token="tok"), JobEventsInput(job_id=JOB_ID))


def test_a_page_never_exceeds_the_requested_or_declared_ceiling() -> None:
    with pytest.raises(ContractSemanticError, match="exceeds the page ceiling of 1"):
        _validate_events(
            _events_result(_events(0, 1), token="tok"),
            JobEventsInput(job_id=JOB_ID, limit=1),
            result_binding=_binding(next_sequence=2),
        )
    assert sem_jobs.JOB_EVENTS_MAX_PAGE_SIZE == 1000


def test_the_snapshot_count_never_changes_within_one_session() -> None:
    with pytest.raises(
        ContractSemanticError, match=sem_jobs.JOB_EVENTS_PAGE_BINDING_REJECTION_MESSAGE
    ):
        _validate_events(
            _events_result(_events(2, 3), snapshot=9),
            JobEventsInput(job_id=JOB_ID, page=PageMetadata(continuation_token="tok-2")),
            request_binding=_binding(next_sequence=2, snapshot_event_count=4),
        )


def test_the_offered_continuation_binds_the_exact_next_position() -> None:
    with pytest.raises(
        ContractSemanticError, match=sem_jobs.JOB_EVENTS_PAGE_BINDING_REJECTION_MESSAGE
    ):
        _validate_events(
            _events_result(_events(0, 1), token="tok"),
            JobEventsInput(job_id=JOB_ID),
            result_binding=_binding(next_sequence=3),
        )


@pytest.mark.parametrize(
    "override",
    [
        {"principal_id": "principal-2"},
        {"workspace_id": "workspace-2"},
        {"operation": "job.get"},
        {"job_id": "job-2"},
        {"ordering": "sequence_desc"},
        {"token_format_version": 2},
        {"next_sequence": 5, "snapshot_event_count": 4},
    ],
)
def test_every_cross_binding_replay_fails_with_one_uniform_diagnostic(
    override: dict[str, Any]
) -> None:
    """A rejected token says only that it is not valid here. Naming which member disagreed
    would turn the rejection into an oracle for what a valid token contains."""
    with pytest.raises(ContractSemanticError) as raised:
        sem_jobs.validate_job_events_page_binding(
            _binding(**override),
            principal_id=PRINCIPAL_ID,
            workspace_id=WORKSPACE_ID,
            job_id=JOB_ID,
        )
    assert str(raised.value) == sem_jobs.JOB_EVENTS_PAGE_BINDING_REJECTION_MESSAGE


@pytest.mark.parametrize(
    "binding",
    [
        None,
        "a string",
        (1, 2, 3),
        _binding()._replace(snapshot_event_count="four"),
        _binding()._replace(next_sequence=-1),
    ],
)
def test_a_malformed_binding_fails_the_same_uniform_way(binding: Any) -> None:
    with pytest.raises(ContractSemanticError) as raised:
        sem_jobs.validate_job_events_page_binding(
            binding, principal_id=PRINCIPAL_ID, workspace_id=WORKSPACE_ID, job_id=JOB_ID
        )
    assert str(raised.value) == sem_jobs.JOB_EVENTS_PAGE_BINDING_REJECTION_MESSAGE


def test_a_request_carrying_a_token_must_present_its_binding() -> None:
    with pytest.raises(
        ContractSemanticError, match=sem_jobs.JOB_EVENTS_PAGE_BINDING_REJECTION_MESSAGE
    ):
        _validate_events(
            _events_result(_events(2, 3)),
            JobEventsInput(job_id=JOB_ID, page=PageMetadata(continuation_token="tok-2")),
        )


def test_present_request_page_metadata_must_name_a_continuation() -> None:
    with pytest.raises(ContractSemanticError, match="names no continuation_token"):
        sem_jobs.validate_job_events_input(
            JobEventsInput(job_id=JOB_ID, page=PageMetadata())
        )


def test_job_events_result_page_is_required_on_the_wire() -> None:
    wire = _events_result(_events(0, 1, 2, 3)).to_wire()
    assert wire["page"] == {}
    del wire["page"]
    _assert_schema_invalid("JobEventsResult", wire)


def test_job_events_answers_the_job_that_was_asked_for() -> None:
    with pytest.raises(ContractSemanticError, match="does not answer the requested job"):
        _validate_events(
            _events_result(_events(0, 1, 2, 3), job_id="job-2"), JobEventsInput(job_id=JOB_ID)
        )


def test_event_timestamps_are_non_decreasing_and_transitions_are_legal() -> None:
    backwards = (
        JobEvent(sequence=0, occurred_at=T2, state="running"),
        JobEvent(sequence=1, occurred_at=T0, state="running"),
    )
    with pytest.raises(ContractSemanticError, match="moves backwards"):
        _validate_events(
            _events_result(backwards, snapshot=2), JobEventsInput(job_id=JOB_ID)
        )
    illegal = (
        JobEvent(sequence=0, occurred_at=T0, state="succeeded"),
        JobEvent(sequence=1, occurred_at=T1, state="running"),
    )
    with pytest.raises(ContractSemanticError, match="may not move from"):
        _validate_events(_events_result(illegal, snapshot=2), JobEventsInput(job_id=JOB_ID))


def test_an_event_stream_records_an_accepted_recovery() -> None:
    """A `queued` event after a terminal one is exactly what an accepted `job.retry` looks
    like in the stream, so the stream is read with the recovery edges open."""
    recovered = (
        JobEvent(sequence=0, occurred_at=T0, state="failed"),
        JobEvent(sequence=1, occurred_at=T1, state="queued"),
        JobEvent(sequence=2, occurred_at=T2, state="running"),
    )
    _validate_events(_events_result(recovered, snapshot=3), JobEventsInput(job_id=JOB_ID))


def test_a_fresh_tokenless_request_captures_a_new_snapshot() -> None:
    """The same job read twice may legitimately report a larger snapshot: a new session
    captures the count as it stands, and the earlier session is unaffected."""
    _validate_events(_events_result(_events(0, 1, 2, 3), snapshot=4), JobEventsInput(job_id=JOB_ID))
    _validate_events(
        _events_result(_events(0, 1, 2, 3, 4, 5), snapshot=6), JobEventsInput(job_id=JOB_ID)
    )


def test_a_binding_supplied_for_a_tokenless_request_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="carries no page"):
        _validate_events(
            _events_result(_events(0, 1, 2, 3)),
            JobEventsInput(job_id=JOB_ID),
            request_binding=_binding(),
        )


# --------------------------------------------------------------------------
# Idempotency equivalence
# --------------------------------------------------------------------------


def _equivalence(
    metadata: RequestMetadata | None = None,
    *,
    operation: str = "job.cancel",
    payload: Any = None,
    principal_id: str = PRINCIPAL_ID,
    workspace_id: str | None = WORKSPACE_ID,
) -> Any:
    return sem_jobs.idempotency_equivalence(
        operation,
        metadata if metadata is not None else _request_metadata(),
        payload if payload is not None else JobCancelInput(job_id=JOB_ID).to_wire(),
        principal_id=principal_id,
        workspace_id=workspace_id,
    )


def test_the_same_request_repeated_is_a_replay() -> None:
    assert (
        sem_jobs.classify_idempotent_replay(_equivalence(), _equivalence())
        == sem_jobs.IDEMPOTENCY_REPLAY
    )


@pytest.mark.parametrize(
    "override",
    [
        {"request_id": "req-2"},
        {"correlation_id": "corr-2"},
        {"trace_id": "trace-2"},
        {"deadline_ms": 5000},
        {"client": ClientIdentity(id="omnivia.desktop", version="2.0.0")},
    ],
)
def test_an_honest_retry_stays_a_replay(override: dict[str, Any]) -> None:
    """Every field here is one a genuine retry is expected to change. Folding any of them
    into the fingerprint would turn every retry into a conflict."""
    assert (
        sem_jobs.classify_idempotent_replay(
            _equivalence(), _equivalence(_request_metadata(**override))
        )
        == sem_jobs.IDEMPOTENCY_REPLAY
    )


@pytest.mark.parametrize(
    ("kwargs", "override"),
    [
        ({}, {"purpose": "diagnostics"}),
        ({}, {"scopes": ("memory:write",)}),
        (
            {},
            {
                "required_capabilities": (
                    CapabilityRequirement(id="job.control", minimum_version="1.0", required=True),
                )
            },
        ),
    ],
)
def test_a_different_request_under_the_same_key_is_a_conflict(
    kwargs: dict[str, Any], override: dict[str, Any]
) -> None:
    assert (
        sem_jobs.classify_idempotent_replay(
            _equivalence(**kwargs), _equivalence(_request_metadata(**override), **kwargs)
        )
        == sem_jobs.IDEMPOTENCY_CONFLICT
    )


def test_the_required_capability_flag_is_part_of_what_a_request_asked_for() -> None:
    """"I need this capability" and "use it if you have it" are different requests. Folding
    only the id and the minimum version into the fingerprint made them equal, so the second
    would have been answered from the first's recorded outcome -- handing back a result
    produced under a capability floor the second caller never stated."""
    optional = _request_metadata(
        required_capabilities=(
            CapabilityRequirement(id="ingestion.import", minimum_version="1.0", required=False),
        )
    )
    assert (
        sem_jobs.classify_idempotent_replay(_equivalence(), _equivalence(optional))
        == sem_jobs.IDEMPOTENCY_CONFLICT
    )
    assert "required_capabilities" in sem_jobs.IDEMPOTENCY_EQUIVALENCE_INCLUDED_INPUTS


def test_a_different_operation_input_under_the_same_key_is_a_conflict() -> None:
    assert (
        sem_jobs.classify_idempotent_replay(
            _equivalence(),
            _equivalence(payload=JobCancelInput(job_id="job-2").to_wire()),
        )
        == sem_jobs.IDEMPOTENCY_CONFLICT
    )
    assert (
        sem_jobs.classify_idempotent_replay(
            _equivalence(),
            _equivalence(payload=JobCancelInput(job_id=JOB_ID, reason="deadline").to_wire()),
        )
        == sem_jobs.IDEMPOTENCY_CONFLICT
    )


def test_scopes_and_capability_requirements_compare_as_sets() -> None:
    reordered = _request_metadata(scopes=("job:control", "memory:write"))
    assert (
        sem_jobs.classify_idempotent_replay(_equivalence(), _equivalence(reordered))
        == sem_jobs.IDEMPOTENCY_REPLAY
    )
    duplicated = _request_metadata(scopes=("memory:write", "job:control", "memory:write"))
    assert (
        sem_jobs.classify_idempotent_replay(_equivalence(), _equivalence(duplicated))
        == sem_jobs.IDEMPOTENCY_REPLAY
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"principal_id": "principal-2"},
        {"workspace_id": "workspace-2"},
        {"workspace_id": None},
        {"operation": "job.retry"},
    ],
)
def test_a_key_is_scoped_to_one_principal_workspace_and_operation(kwargs: dict[str, Any]) -> None:
    assert (
        sem_jobs.classify_idempotent_replay(_equivalence(), _equivalence(**kwargs))
        == sem_jobs.IDEMPOTENCY_DISTINCT
    )


def test_a_different_key_is_a_different_request() -> None:
    assert (
        sem_jobs.classify_idempotent_replay(
            _equivalence(), _equivalence(_request_metadata(idempotency_key="key-2"))
        )
        == sem_jobs.IDEMPOTENCY_DISTINCT
    )


def test_an_equivalence_key_needs_an_idempotency_key() -> None:
    with pytest.raises(ContractSemanticError, match="carries no idempotency key"):
        _equivalence(_request_metadata(idempotency_key=None))


def test_the_included_and_excluded_equivalence_inputs_are_disjoint_and_frozen() -> None:
    assert not set(sem_jobs.IDEMPOTENCY_EQUIVALENCE_INCLUDED_INPUTS) & set(
        sem_jobs.IDEMPOTENCY_EQUIVALENCE_EXCLUDED_INPUTS
    )
    assert sem_jobs.IDEMPOTENCY_EQUIVALENCE_INCLUDED_INPUTS == (
        "operation_input",
        "purpose",
        "scopes",
        "required_capabilities",
    )
    assert sem_jobs.IDEMPOTENCY_EQUIVALENCE_EXCLUDED_INPUTS == (
        "request_id",
        "correlation_id",
        "trace_id",
        "deadline_ms",
        "client",
    )


@pytest.mark.parametrize(
    ("supports", "required", "safe", "match"),
    [
        (False, True, False, "cannot require an idempotency key it does not support"),
        (True, True, True, "safe to retry without a key"),
        (False, True, True, "cannot require an idempotency key it does not support"),
    ],
)
def test_operation_idempotency_metadata_must_be_satisfiable(
    supports: bool, required: bool, safe: bool, match: str
) -> None:
    with pytest.raises(ContractSemanticError, match=match):
        sem_jobs.validate_operation_idempotency_metadata(
            OperationIdempotencyMetadata(
                supports_idempotency_key=supports, required=required, safe_to_retry=safe
            )
        )


def test_a_missing_required_key_or_unsupported_precondition_is_an_invalid_request() -> None:
    mutation = OperationIdempotencyMetadata(
        supports_idempotency_key=True, required=True, safe_to_retry=False
    )
    read = OperationIdempotencyMetadata(
        supports_idempotency_key=False, required=False, safe_to_retry=True
    )
    no_precondition = OperationPreconditionMetadata(
        supports_mutation_precondition=False, required=False
    )
    sem_jobs.validate_request_idempotency(mutation, no_precondition, _request_metadata())

    with pytest.raises(ContractSemanticError, match="required by this operation and is absent"):
        sem_jobs.validate_request_idempotency(
            mutation, no_precondition, _request_metadata(idempotency_key=None)
        )
    with pytest.raises(ContractSemanticError, match="honours none"):
        sem_jobs.validate_request_idempotency(read, no_precondition, _request_metadata())
    with pytest.raises(ContractSemanticError, match="applies none"):
        sem_jobs.validate_request_idempotency(
            mutation,
            no_precondition,
            _request_metadata(mutation_precondition=MutationPrecondition(record_version="v1")),
        )
    with pytest.raises(ContractSemanticError, match="mutation_precondition is required"):
        sem_jobs.validate_request_idempotency(
            mutation,
            OperationPreconditionMetadata(supports_mutation_precondition=True, required=True),
            _request_metadata(),
        )


# --------------------------------------------------------------------------
# Frozen operation metadata posture for the later catalogue
# --------------------------------------------------------------------------


def test_the_frozen_posture_covers_exactly_the_five_job_lifecycle_operations() -> None:
    assert tuple(sem_jobs.JOB_LIFECYCLE_OPERATION_POSTURES) == sem_jobs.JOB_LIFECYCLE_OPERATIONS
    assert sem_jobs.JOB_LIFECYCLE_OPERATIONS == (
        "import.start",
        "job.get",
        "job.cancel",
        "job.retry",
        "job.events",
    )


@pytest.mark.parametrize(
    ("operation", "capability", "scope", "side_effect", "completion", "audit"),
    [
        ("import.start", "ingestion.import", "memory:write", "create", "always_returns_job", "mutation"),
        ("job.get", "job.read", "job:read", "none", "synchronous", "read"),
        ("job.cancel", "job.control", "job:control", "update", "synchronous", "mutation"),
        ("job.retry", "job.control", "job:control", "update", "synchronous", "mutation"),
        ("job.events", "job.read", "job:read", "none", "synchronous", "read"),
    ],
)
def test_each_frozen_operation_posture_matches_the_a2_4_freeze(
    operation: str,
    capability: str,
    scope: str,
    side_effect: str,
    completion: str,
    audit: str,
) -> None:
    posture = sem_jobs.JOB_LIFECYCLE_OPERATION_POSTURES[operation]
    assert posture.required_capability_id == capability
    assert posture.scope.required_scopes == (scope,)
    assert posture.scope.scope_kind == "workspace"
    assert posture.scope.side_effect == side_effect
    assert posture.job.completion_mode == completion
    assert posture.audit == v1.OperationAuditMetadata(audited=True, audit_category=audit)
    assert posture.precondition == OperationPreconditionMetadata(
        supports_mutation_precondition=False, required=False
    )
    sem_jobs.validate_operation_idempotency_metadata(posture.idempotency)


@pytest.mark.parametrize("operation", ["import.start", "job.cancel", "job.retry"])
def test_every_frozen_mutation_requires_an_idempotency_key(operation: str) -> None:
    idempotency = sem_jobs.JOB_LIFECYCLE_OPERATION_POSTURES[operation].idempotency
    assert idempotency.supports_idempotency_key is True
    assert idempotency.required is True
    assert idempotency.safe_to_retry is False


@pytest.mark.parametrize("operation", ["job.get", "job.events"])
def test_every_frozen_read_is_safe_to_retry_without_a_key(operation: str) -> None:
    idempotency = sem_jobs.JOB_LIFECYCLE_OPERATION_POSTURES[operation].idempotency
    assert idempotency.supports_idempotency_key is False
    assert idempotency.required is False
    assert idempotency.safe_to_retry is True


def test_only_import_start_declares_a_job_and_only_job_events_paginates() -> None:
    postures = sem_jobs.JOB_LIFECYCLE_OPERATION_POSTURES
    assert postures["import.start"].job.job_kind == "ingestion.import"
    assert (
        postures["import.start"].job.terminal_result_schema_ref
        == sem_jobs.IMPORT_COMPLETION_RESULT_SCHEMA_REF
    )
    for name in ("job.get", "job.cancel", "job.retry", "job.events"):
        assert postures[name].job.job_kind is None
        assert postures[name].job.terminal_result_schema_ref is None
    assert postures["job.events"].pagination == v1.OperationPaginationMetadata(
        paginated=True, max_page_size=1000
    )
    for name in ("import.start", "job.get", "job.cancel", "job.retry"):
        assert postures[name].pagination == v1.OperationPaginationMetadata(paginated=False)


def test_the_import_terminal_result_schema_ref_resolves_in_the_published_registry() -> None:
    """The reference the catalogue will bind must name a definition that actually exists,
    or A2.5 would publish a dangling pointer to the shape this module already enforces."""
    registry_document = json.loads(
        (SCHEMA_DIR / "application-v1.schema.json").read_text(encoding="utf-8")
    )
    assert "ImportCompletionResult" in registry_document["$defs"]
    assert (
        registry_document["$defs"]["ImportCompletionResult"]["$ref"]
        == sem_jobs.IMPORT_COMPLETION_RESULT_SCHEMA_REF
    )


# --------------------------------------------------------------------------
# Direct-entry safety: a hand-built DTO never leaks a raw Python exception
# --------------------------------------------------------------------------


def _broken_cases() -> list[tuple[str, Any, tuple[Any, ...], dict[str, Any]]]:
    handle = _handle(state="running")
    return [
        ("source is not a descriptor", sem_jobs.validate_import_source_descriptor, ({},), {}),
        (
            "source length is a string",
            sem_jobs.validate_import_source_descriptor,
            (_source(content_length_bytes="2048"),),
            {},
        ),
        (
            "source media_type is None",
            sem_jobs.validate_import_source_descriptor,
            (_source(media_type=None),),
            {},
        ),
        ("start input is a plain dict", sem_jobs.validate_import_start_input, ({"source": {}},), {}),
        (
            "start result handle is None",
            sem_jobs.validate_import_start_result,
            (ImportStartResult(job=None),),
            {"response_job_reference": JobReference(job_id=JOB_ID)},
        ),
        ("attempt is a string", sem_jobs.validate_job_attempt, ("attempt",), {}),
        (
            "attempt number is a string",
            sem_jobs.validate_job_attempt,
            (JobAttempt(
                attempt_number="1", started_at=T0, finished_at=T2, state="succeeded"
            ),),
            {},
        ),
        ("attempt history is not a sequence", sem_jobs.validate_job_attempt_history, (7,), {}),
        ("attempt history holds a non-attempt", sem_jobs.validate_job_attempt_history, ((None,),), {}),
        ("handle is None", sem_jobs.validate_job_handle, (None,), {}),
        (
            "handle identity is a dict",
            sem_jobs.validate_job_handle,
            (dataclasses.replace(handle, identity={"job_id": JOB_ID}),),
            {},
        ),
        (
            "handle control is None",
            sem_jobs.validate_job_handle,
            (dataclasses.replace(handle, control=None),),
            {},
        ),
        (
            "handle state is an int",
            sem_jobs.validate_job_handle,
            (dataclasses.replace(handle, state=7),),
            {},
        ),
        (
            "handle created_at is not a timestamp",
            sem_jobs.validate_job_handle,
            (dataclasses.replace(handle, created_at="yesterday"),),
            {},
        ),
        (
            "progress completed_units is a float string",
            sem_jobs.validate_job_handle,
            (dataclasses.replace(handle, progress=JobProgress(unit="item", completed_units="3")),),
            {},
        ),
        (
            "terminal result is a dict",
            sem_jobs.validate_job_terminal_result,
            ({},),
            {"accepted_import_source": None},
        ),
        (
            "terminal attempts is a string",
            sem_jobs.validate_job_terminal_result,
            (_success(attempts="attempts"),),
            {"accepted_import_source": _source()},
        ),
        (
            "terminal success result is not a mapping",
            sem_jobs.validate_job_terminal_result,
            (_success(result="not-a-mapping"),),
            {"accepted_import_source": _source()},
        ),
        (
            "terminal lifetime bound is not a timestamp",
            sem_jobs.validate_job_terminal_result,
            (_success(),),
            {"accepted_import_source": _source(), "created_at": 7},
        ),
        (
            "completion is None",
            sem_jobs.validate_import_completion_result,
            (None,),
            {"job_id": JOB_ID, "accepted_import_source": _source()},
        ),
        ("completion shape is None", sem_jobs.validate_import_completion_result_shape, (None,), {}),
        (
            "completion partial is a string",
            sem_jobs.validate_import_completion_result_shape,
            (_completion(partial="yes"),),
            {},
        ),
        (
            "completion accepted source is a dict",
            sem_jobs.validate_import_completion_result,
            (_completion(),),
            {"job_id": JOB_ID, "accepted_import_source": {}},
        ),
        ("get input is a string", sem_jobs.validate_job_get_input, ("job-1",), {}),
        (
            "get result is a tuple",
            sem_jobs.validate_job_get_result,
            ((),),
            {"accepted_import_source": None},
        ),
        ("cancel input is None", sem_jobs.validate_job_cancel_input, (None,), {}),
        (
            "cancel result is None",
            sem_jobs.validate_job_cancel_result,
            (None,),
            {"previous": handle},
        ),
        (
            "cancel disposition is an int",
            sem_jobs.validate_job_cancel_result,
            (JobCancelResult(job=handle, cancellation_disposition=7),),
            {"previous": handle},
        ),
        (
            "cancel previous handle is a string",
            sem_jobs.validate_job_cancel_result,
            (JobCancelResult(job=handle, cancellation_disposition="not_cancellable"),),
            {"previous": "handle"},
        ),
        (
            "cancel shape result is None",
            sem_jobs.validate_job_cancel_result_shape,
            (None,),
            {},
        ),
        ("retry input is a list", sem_jobs.validate_job_retry_input, ([],), {}),
        (
            "retry result is a list",
            sem_jobs.validate_job_retry_result,
            ([],),
            {"previous": handle},
        ),
        (
            "retry previous handle is an int",
            sem_jobs.validate_job_retry_result,
            (JobRetryResult(job=handle, recovery_disposition="not_retryable"),),
            {"previous": 7},
        ),
        ("retry shape result is a list", sem_jobs.validate_job_retry_result_shape, ([],), {}),
        ("events input is a set", sem_jobs.validate_job_events_input, (set(),), {}),
        (
            "events input limit is a string",
            sem_jobs.validate_job_events_input,
            (JobEventsInput(job_id=JOB_ID, limit="10"),),
            {},
        ),
        (
            "events result is None",
            sem_jobs.validate_job_events_result,
            (None, JobEventsInput(job_id=JOB_ID)),
            {"principal_id": PRINCIPAL_ID, "workspace_id": WORKSPACE_ID},
        ),
        (
            "events sequence is a string",
            sem_jobs.validate_job_events_result,
            (
                _events_result((JobEvent(sequence="0", occurred_at=T0, state="queued"),), snapshot=1),
                JobEventsInput(job_id=JOB_ID),
            ),
            {"principal_id": PRINCIPAL_ID, "workspace_id": WORKSPACE_ID},
        ),
        (
            "events list is not a sequence",
            sem_jobs.validate_job_events_result,
            (
                JobEventsResult(job_id=JOB_ID, events=7, snapshot_event_count=1, page=PageMetadata()),
                JobEventsInput(job_id=JOB_ID),
            ),
            {"principal_id": PRINCIPAL_ID, "workspace_id": WORKSPACE_ID},
        ),
        ("replay pair is nonsense", sem_jobs.validate_job_control_replay, (7, 7), {}),
        (
            "equivalence metadata is a dict",
            sem_jobs.idempotency_equivalence,
            ("job.cancel", {}, {}),
            {"principal_id": PRINCIPAL_ID},
        ),
        (
            "equivalence scopes is not a sequence",
            sem_jobs.idempotency_equivalence,
            ("job.cancel", _request_metadata(scopes=7), {}),
            {"principal_id": PRINCIPAL_ID},
        ),
        (
            "equivalence input is not JSON",
            sem_jobs.idempotency_equivalence,
            ("job.cancel", _request_metadata(), {"x": object()}),
            {"principal_id": PRINCIPAL_ID},
        ),
        ("replay classification takes equivalences", sem_jobs.classify_idempotent_replay, (1, 2), {}),
        (
            "idempotency metadata is a dict",
            sem_jobs.validate_operation_idempotency_metadata,
            ({"required": True},),
            {},
        ),
        (
            "request idempotency metadata is None",
            sem_jobs.validate_request_idempotency,
            (
                OperationIdempotencyMetadata(
                    supports_idempotency_key=True, required=True, safe_to_retry=False
                ),
                OperationPreconditionMetadata(
                    supports_mutation_precondition=False, required=False
                ),
                None,
            ),
            {},
        ),
        (
            "state transition takes strings",
            sem_jobs.validate_job_state_transition,
            (None, "running"),
            {},
        ),
        (
            "synchronous reference operation is an int",
            sem_jobs.validate_synchronous_job_response_job_reference,
            (7, None),
            {},
        ),
    ]


@pytest.mark.parametrize(
    ("label", "function", "args", "kwargs"),
    [(case[0], case[1], case[2], case[3]) for case in _broken_cases()],
    ids=[case[0] for case in _broken_cases()],
)
def test_a_hand_built_dto_never_leaks_a_raw_exception(
    label: str, function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    """Every public validator here is a direct entry point: a caller may reach it without a
    tolerant `from_wire` decode in front, so each must turn a wrongly typed value into a
    `ContractSemanticError` rather than let an `AttributeError`/`TypeError`/bare `ValueError`
    escape the contract layer.

    The assertion is on the *exact* type, not `isinstance`. `ContractSemanticError` is itself
    a `ValueError` subclass, so an `isinstance` check would pass for a raw `ValueError` raised
    incidentally by `datetime.fromisoformat` or `int()` -- precisely the leak this catches.
    """
    # Deliberately catching bare `Exception`: the point is to observe whatever actually
    # escapes, then assert on its exact type below.
    with pytest.raises(Exception) as raised:
        function(*args, **kwargs)
    assert type(raised.value) is ContractSemanticError, (
        f"{label}: leaked {type(raised.value).__name__}: {raised.value}"
    )


@pytest.mark.parametrize("value", ["true", "", 1, 0, None, [], object()], ids=repr)
def test_every_public_boolean_argument_is_type_checked_not_merely_truthy(value: Any) -> None:
    """Python truthiness is not semantic validation. `"false"` is truthy, `0` is falsy, and
    both are statements a caller never made -- so every boolean that decides something here is
    checked for its type first. `recovery_accepted` unlocks a step out of a terminal state and
    `executing` licenses an unfinished attempt; guessing at either from a wrong-typed value
    silently grants an exception nobody asked for."""
    with pytest.raises(ContractSemanticError, match="recovery_accepted"):
        sem_jobs.validate_job_state_transition("failed", "queued", recovery_accepted=value)
    with pytest.raises(ContractSemanticError, match="executing"):
        sem_jobs.validate_job_attempt_history((), executing=value)
    with pytest.raises(ContractSemanticError, match="recovery_accepted"):
        sem_jobs.validate_job_observation_progression(
            _handle(state="failed"),
            _handle(state="queued", updated_at=T3, latest_attempt=_attempt(state="failed", error=_error())),
            recovery_accepted=value,
        )


@pytest.mark.parametrize("field", ["supports_idempotency_key", "required", "safe_to_retry"])
@pytest.mark.parametrize("value", ["yes", 1, None], ids=repr)
def test_every_idempotency_metadata_boolean_is_type_checked(field: str, value: Any) -> None:
    metadata = OperationIdempotencyMetadata(
        supports_idempotency_key=True, required=True, safe_to_retry=False
    )
    with pytest.raises(ContractSemanticError, match=field):
        sem_jobs.validate_operation_idempotency_metadata(
            dataclasses.replace(metadata, **{field: value})
        )


@pytest.mark.parametrize("field", ["supports_mutation_precondition", "required"])
@pytest.mark.parametrize("value", ["yes", 1, None], ids=repr)
def test_every_precondition_metadata_boolean_is_type_checked(field: str, value: Any) -> None:
    precondition = OperationPreconditionMetadata(
        supports_mutation_precondition=True, required=False
    )
    with pytest.raises(ContractSemanticError, match=f"precondition.{field}"):
        sem_jobs.validate_request_idempotency(
            OperationIdempotencyMetadata(
                supports_idempotency_key=True, required=True, safe_to_retry=False
            ),
            dataclasses.replace(precondition, **{field: value}),
            _request_metadata(mutation_precondition=MutationPrecondition(record_version="v1")),
        )


@pytest.mark.parametrize("value", ["yes", 1, None], ids=repr)
def test_the_capability_required_flag_is_type_checked_before_it_is_fingerprinted(
    value: Any,
) -> None:
    metadata = _request_metadata(
        required_capabilities=(
            CapabilityRequirement(id="ingestion.import", minimum_version="1.0", required=value),
        )
    )
    with pytest.raises(ContractSemanticError, match="required"):
        _equivalence(metadata)


@pytest.mark.parametrize("value", ["yes", 1, None, 0], ids=repr)
def test_the_import_completion_partial_flag_is_type_checked(value: Any) -> None:
    with pytest.raises(ContractSemanticError, match="partial"):
        sem_jobs.validate_import_completion_result_shape(_completion(partial=value))


# --------------------------------------------------------------------------
# The canonical invalid-request taxonomy
# --------------------------------------------------------------------------


def test_the_page_binding_rejection_resolves_through_the_published_error_catalogue() -> None:
    """One canonical code, published once and read everywhere. A `job.events` token that is
    not valid for the request presenting it is one more request the server cannot execute as
    stated, so it reports the catalogue's own `invalid_request` rather than a second,
    operation-specific alias two peers could classify differently."""
    code = sem_jobs.JOB_EVENTS_PAGE_BINDING_REJECTION_CODE
    assert code == "invalid_request"

    catalogue = json.loads(
        (SCHEMA_DIR / "errors.schema.json").read_text(encoding="utf-8")
    )["x-omnivia-error-catalogue"]
    assert catalogue[code] == sem_jobs.JOB_EVENTS_PAGE_BINDING_REJECTION_RETRY_CLASS

    assert generated.ERROR_CODE_INVALID_REQUEST == code
    assert code in generated.FROZEN_ERROR_CODES
    assert generated.DEFAULT_RETRY_CLASSIFICATION[code] == "non_retryable"
    assert not codec.is_error_retryable(
        ApiError(code=code, message="x", retry_class="non_retryable")
    )
    assert v1.ERROR_CODE_INVALID_REQUEST is generated.ERROR_CODE_INVALID_REQUEST


def test_the_canonical_invalid_request_code_reaches_the_typescript_vocabulary() -> None:
    typescript = (REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts").read_text(
        encoding="utf-8"
    )
    assert '  "invalid_request",\n' in typescript
    assert '  invalid_request: "non_retryable",\n' in typescript


def test_there_is_exactly_one_invalid_request_code_in_the_catalogue() -> None:
    """No alias. A second spelling of "the server cannot execute this request as stated" is a
    second answer to one question, and the A2.5 operation catalogue would then have to pick."""
    catalogue = json.loads(
        (SCHEMA_DIR / "errors.schema.json").read_text(encoding="utf-8")
    )["x-omnivia-error-catalogue"]
    aliases = [code for code in catalogue if "invalid" in code and "request" in code]
    assert aliases == ["invalid_request"]


def test_the_public_barrel_re_exports_the_job_semantics_surface() -> None:
    for name in sem_jobs.__all__:
        assert hasattr(v1, name), f"v1 is missing semantics_jobs name {name!r}"
        assert getattr(v1, name) is getattr(sem_jobs, name)
    assert v1.semantics_jobs is sem_jobs


def test_the_frozen_posture_mapping_is_read_only() -> None:
    with pytest.raises(TypeError):
        sem_jobs.JOB_LIFECYCLE_OPERATION_POSTURES["job.get"] = None  # type: ignore[index]
    snapshot = copy.deepcopy(dict(sem_jobs.JOB_LIFECYCLE_OPERATION_POSTURES))
    assert snapshot == dict(sem_jobs.JOB_LIFECYCLE_OPERATION_POSTURES)
