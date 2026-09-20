"""The `ingestion.import` executor, against the production path and nothing else.

The workspace here is a real migrated one on a real layout, owned under a real lease
and mutation guard; `import.start` is served by the composed S3 family, through the
real mutation coordinator; and the thing under test is
:class:`~omnivia_core_runtime.service.import_execution.ImportJobExecutor` driven
exactly as `service.main.serve` drives it -- one bounded pass over this workspace's
own pending jobs. Nothing below writes a job state, terminalizes a job by hand or
stands in for the executor.

What each case is here to pin, and why the set is what it is:

* **the fresh journey** -- the state `import.start` leaves behind is carried to a
  terminal `import_completion` with accounting that adds up, one L0 artifact bound to
  the run, and that artifact findable through the production `evidence.search`;
* **a crash between the durable steps** -- the evidence commit stands, the terminal
  observation never happened, the workspace is recovered at a new generation, and the
  recovered attempt finishes the remaining steps rather than importing again;
* **duplicate prevention** -- a second pass over a settled job is not a second
  execution, and the same staged source reached by a second job is not a second
  artifact;
* **the projection barrier** -- an attempt that cannot show the evidence is findable
  fails retryably instead of reporting a success `evidence.search` would contradict;
* **inertness** -- a staged row whose metadata spells a path, a URL and an
  instruction produces evidence that carries none of them, and a job whose events and
  terminal result quote none of them either;
* **fence loss** -- an instance whose generation has been superseded writes nothing at
  all, and leaves the job for whoever owns the workspace now.

The two faults injected are at module boundaries -- `complete_application_job` and
`build_search_projection`, each replaced in the executor's module for one call -- for
the reason `test_evidence_capture_vertical` injects its one there: an interruption
between two durable steps has no other way in from outside, and everything else in
the path stays the production article.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.ownership.fencing import (
    close_guard,
    fenced_transaction,
    open_guard,
)
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    ProcessEvidence,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service import import_execution
from omnivia_core_runtime.service.application import (
    EVIDENCE_SEARCH_OPERATION,
    IMPORT_START_OPERATION,
    JOB_EVENTS_OPERATION,
    JOB_FAMILY_PURPOSES,
    JOB_GET_OPERATION,
    KNOWLEDGE_RETRIEVAL_PURPOSE,
    LOCAL_TRANSPORT_ADAPTER,
    WORKSPACE_INSPECT_OPERATION,
    ApplicationDispatcher,
    build_application_registry,
    build_job_application_dispatcher,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.jobs import (
    JOB_RETRY_OPERATION,
    request_import_source,
)
from omnivia_core_runtime.service.import_execution import ImportJobExecutor
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    server_capability_snapshot,
)
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.jobs import (
    read_accepted_import_source,
    recover_stranded_application_jobs,
)
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations,
    bootstrap_generation_one,
    materialise_phase0_baseline,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ApiError,
    CapabilityRequirement,
    ClientIdentity,
    EvidenceSearchResult,
    ImportCompletionResult,
    ImportStartResult,
    JobEventsResult,
    JobGetResult,
    JobTerminalFailure,
    JobTerminalSuccess,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    decode_job_get_input,
    get_operation_metadata,
    validate_job_get_result,
)

WORKSPACE_ID = "ws-import-0001"
INSTALLATION_ID = "inst-import-0001"
SERVICE_INSTANCE = "svc-import-one"
SUCCESSOR_INSTANCE = "svc-import-two"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")

ARTIFACTS = "omnivia_evidence_artifacts"
PROVENANCE = "omnivia_evidence_provenance_events"
OBSERVATIONS = "omnivia_job_terminal_observations"

#: The staged source every case names. One verified staging over one blob, which is
#: the whole of what `import.start` accepts and the whole of what execution reads.
STAGED_REF = "stg-import-0001"
STAGED_DIGEST = "sha256:" + "1c" * 32
STAGED_KIND = "archive"
STAGED_MEDIA_TYPE = "application/zip"
STAGED_BYTES = 4096

#: A second staged source, identical in every respect that matters except which
#: handle names it, so "the same source twice" and "two sources" are separable.
OTHER_REF = "stg-import-0002"
OTHER_DIGEST = "sha256:" + "2d" * 32

#: What a hostile stager could have written into the staged row's own metadata. Every
#: token here is asserted absent from everything execution produces. `unicode61` splits
#: on the punctuation around them, so each is a searchable word if it ever leaked into
#: the index.
HOSTILE_TOKENS = ("etcpasswd", "exfiltratehost", "ignorepreviousinstructions")
HOSTILE_METADATA = (
    '{"path": "/etc/' + HOSTILE_TOKENS[0] + '",'
    ' "url": "https://' + HOSTILE_TOKENS[1] + '/steal",'
    ' "note": "' + HOSTILE_TOKENS[2] + '", "parser": "shell", "token": "hunter2"}'
)

BASE_US = 1_700_000_000_000_000


# --- a real, owned, migrated workspace -----------------------------------------


@dataclass(frozen=True)
class Served:
    """The service surface the handlers and the executor read, and nothing else."""

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    generation: int
    layout: WorkspaceLayout
    clock: FakeClock


def _identity(instance: str) -> ServiceInstanceIdentity:
    return ServiceInstanceIdentity(
        service_instance_id=instance,
        installation_id=INSTALLATION_ID,
        process=ProcessEvidence(
            pid=5151, start_time="100", boot_id="boot-import", os_principal="me"
        ),
    )


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[Served]:
    """A workspace taken to the canonical schema through the real migrator."""
    layout = WorkspaceLayout(root=tmp_path / "workspace")
    layout.root.mkdir()
    layout.blobs_path.mkdir()
    materialise_phase0_baseline(layout.database_path)
    maintenance = open_database(layout.database_path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = bootstrap_generation_one(
            maintenance,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=True,
            service_instance_id=SERVICE_INSTANCE,
        )
        apply_pending_migrations(
            maintenance,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
    finally:
        maintenance.close()

    clock = FakeClock()
    identity = _identity(SERVICE_INSTANCE)
    connection = open_database(layout.database_path, OpenMode.SERVICE_OWNED)
    lease = acquire_lease(
        connection,
        identity,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    yield Served(
        connection=connection,
        identity=identity,
        generation=lease.fencing_generation,
        layout=layout,
        clock=clock,
    )
    connection.close()


def stage(
    owned: Served,
    *,
    staged_source_ref: str = STAGED_REF,
    digest: str = STAGED_DIGEST,
    metadata: str = '{"kind":"archive"}',
) -> None:
    """One verified staging over one blob, written the way a trusted path writes it.

    Three rows and one transaction, because 0008 makes them one fact: a `verified`
    staging must address a blob, the reference is composite over digest *and* length,
    and the integrity event is how the verification is a recorded fact rather than a
    column somebody set.
    """
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        owned.connection.execute(
            "INSERT INTO omnivia_blob_objects (workspace_id, content_digest, "
            "content_length_bytes, created_at_us, verified_at_us) VALUES (?, ?, ?, ?, ?)",
            (WORKSPACE_ID, digest, STAGED_BYTES, BASE_US, BASE_US + 1),
        )
        owned.connection.execute(
            "INSERT INTO omnivia_blob_integrity_events (integrity_event_id, "
            "workspace_id, content_digest, integrity_sequence, outcome, checked_at_us) "
            "VALUES (?, ?, ?, 1, 'verified', ?)",
            (f"bie-{staged_source_ref}", WORKSPACE_ID, digest, BASE_US + 2),
        )
        owned.connection.execute(
            "INSERT INTO omnivia_staged_sources (staged_source_ref, workspace_id, "
            "source_kind, declared_checksum, content_length_bytes, media_type, "
            "computed_checksum, original_metadata_json, original_metadata_digest, "
            "staging_outcome, blob_workspace_id, blob_content_digest, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?)",
            (
                staged_source_ref,
                WORKSPACE_ID,
                STAGED_KIND,
                digest,
                STAGED_BYTES,
                STAGED_MEDIA_TYPE,
                digest,
                metadata,
                "sha256:" + "3e" * 32,
                WORKSPACE_ID,
                digest,
                BASE_US + 3,
            ),
        )


# --- the production application path --------------------------------------------


def _allocator(tag: str) -> Any:
    counts: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{tag}-{counts[prefix]}"

    return allocate


@pytest.fixture
def router(owned: Served) -> ApplicationDispatcher:
    """The S3 family in front of the reads family, composed as `serve` composes it."""
    reads_registry = build_application_registry()
    reads = ApplicationDispatcher(
        registry=reads_registry,
        session=local_owner_session(
            principal_id=LOCAL_PRINCIPAL,
            installation_id=INSTALLATION_ID,
            workspace_id=WORKSPACE_ID,
            operations=frozenset({WORKSPACE_INSPECT_OPERATION, EVIDENCE_SEARCH_OPERATION}),
        ),
        binding=ServiceBinding(
            installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
        ),
        supported_capabilities=server_capability_snapshot(reads_registry),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset({WORKSPACE_ID}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            owned,
        ),
        record=None,
        service=owned,
    )
    return build_job_application_dispatcher(
        service=owned,
        principal_id=LOCAL_PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=reads,
        clock=owned.clock,
        allocate_identifier=_allocator("imp"),
    )


def executor(owned: Served, *, generation: int | None = None) -> ImportJobExecutor:
    """The executor `service.main.serve` builds, for this instance."""
    return ImportJobExecutor(
        connection=owned.connection,
        identity=owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation if generation is None else generation,
        clock=owned.clock,
        blobs_root=owned.layout.blobs_path,
    )


def _metadata(operation: str, *, request_id: str, key: str | None) -> RequestMetadata:
    entry = get_operation_metadata(operation)
    required = entry.required_capability
    return RequestMetadata(
        request_id=request_id,
        correlation_id=f"cor-{request_id}",
        trace_id=f"trc-{request_id}",
        api_version=CONTRACT_VERSION,
        client=CLIENT,
        workspace_id=WORKSPACE_ID,
        scopes=tuple(entry.scope.required_scopes),
        purpose=JOB_FAMILY_PURPOSES.get(operation, KNOWLEDGE_RETRIEVAL_PURPOSE),
        required_capabilities=(
            CapabilityRequirement(
                id=required.id, minimum_version=required.minimum_version, required=True
            ),
        ),
        idempotency_key=key,
        mutation_precondition=None,
        principal_claim=None,
    )


def request(
    operation: str, payload: dict[str, Any], *, request_id: str, key: str | None = None
) -> RequestEnvelope:
    return RequestEnvelope(
        operation=operation,
        metadata=_metadata(operation, request_id=request_id, key=key),
        input=payload,
    )


def answered(response: ResponseEnvelope) -> SuccessResponseEnvelope:
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def descriptor(staged_source_ref: str = STAGED_REF, digest: str = STAGED_DIGEST) -> dict[str, Any]:
    return {
        "staged_source_ref": staged_source_ref,
        "source_kind": STAGED_KIND,
        "content_checksum": digest,
        "content_length_bytes": STAGED_BYTES,
        "media_type": STAGED_MEDIA_TYPE,
    }


def start(
    router: ApplicationDispatcher,
    *,
    request_id: str = "req-import-1",
    key: str = "idem-import-1",
    staged_source_ref: str = STAGED_REF,
    digest: str = STAGED_DIGEST,
) -> str:
    """One `import.start`, through the production handler, returning the job id."""
    response = answered(
        router.dispatch(
            request(
                IMPORT_START_OPERATION,
                {"source": descriptor(staged_source_ref, digest)},
                request_id=request_id,
                key=key,
            )
        )
    )
    return ImportStartResult.from_wire(response.result).job.identity.job_id


def observed(router: ApplicationDispatcher, job_id: str, *, request_id: str) -> JobGetResult:
    """`job.get`, validated by the contract's own validator, as the handler validates it."""
    response = answered(
        router.dispatch(
            request(JOB_GET_OPERATION, {"job_id": job_id}, request_id=request_id)
        )
    )
    return JobGetResult.from_wire(response.result)


def events(router: ApplicationDispatcher, job_id: str, *, request_id: str) -> JobEventsResult:
    response = answered(
        router.dispatch(
            request(JOB_EVENTS_OPERATION, {"job_id": job_id}, request_id=request_id)
        )
    )
    return JobEventsResult.from_wire(response.result)


def found(router: ApplicationDispatcher, query: str, *, request_id: str) -> tuple[str, ...]:
    response = answered(
        router.dispatch(
            request(EVIDENCE_SEARCH_OPERATION, {"query": query}, request_id=request_id)
        )
    )
    return tuple(
        item.evidence_id
        for item in EvidenceSearchResult.from_wire(response.result).evidence
    )


def completion(result: JobGetResult) -> ImportCompletionResult:
    terminal = result.terminal_result
    assert isinstance(terminal, JobTerminalSuccess), "the job published no success"
    return ImportCompletionResult.from_wire(terminal.result)


def failure(result: JobGetResult) -> ApiError:
    terminal = result.terminal_result
    assert isinstance(terminal, JobTerminalFailure), "the job published no failure"
    return terminal.error


def rows(owned: Served, statement: str, *parameters: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in owned.connection.execute(statement, parameters)]


def count(owned: Served, table: str) -> int:
    return int(owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def supersede(owned: Served) -> tuple[ServiceInstanceIdentity, int]:
    """Another instance takes the workspace, exactly as a restart would take it.

    The same order `ServiceRunner` uses -- the lease, then the guard -- so the
    generation the successor holds is one `fenced_transaction` will validate against
    and the predecessor's is one it will refuse.
    """
    successor = _identity(SUCCESSOR_INSTANCE)
    close_guard(owned.connection)
    lease = acquire_lease(
        owned.connection,
        successor,
        clock=owned.clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        owned.connection,
        successor,
        clock=owned.clock,
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    return successor, lease.fencing_generation


# --- the fresh journey ----------------------------------------------------------


def test_a_fresh_import_publishes_one_artifact_and_a_terminal_completion(
    owned: Served, router: ApplicationDispatcher
) -> None:
    """One staged descriptor in, one L0 artifact and one settled accounting out."""
    stage(owned)
    job_id = start(router)
    owned.clock.advance_wall(1)

    assert executor(owned).run_pending() == (job_id,)

    read = observed(router, job_id, request_id="req-get-1")
    assert read.job.state == "succeeded"
    assert read.job.latest_attempt is not None
    assert read.job.latest_attempt.attempt_number == 1
    assert read.job.latest_attempt.state == "succeeded"

    reported = completion(read)
    assert reported.import_run_id == job_id
    assert reported.source.staged_source_ref == STAGED_REF
    assert (
        reported.discovered_items,
        reported.evidence_records_created,
        reported.skipped_items,
        reported.failed_items,
        reported.partial,
    ) == (1, 1, 0, 0, False)

    # Exactly one artifact, bound to this run, addressing the staged blob and nothing
    # else -- no locator, no retrieval instant, and the staged source's own kind.
    ((evidence_id, kind, locator, retrieved, checksum, staged, run),) = rows(
        owned,
        f"SELECT evidence_id, source_kind, source_locator, source_retrieved_at_us, "
        f"content_checksum, staged_source_ref, import_run_id FROM {ARTIFACTS}",
    )
    assert (kind, locator, retrieved) == (STAGED_KIND, None, None)
    assert (checksum, staged, run) == (STAGED_DIGEST, STAGED_REF, job_id)

    # One provenance event, attributing the act to the service while keeping the
    # request that authorised it reachable through the job's own audit reference.
    assert rows(
        owned,
        f"SELECT evidence_id, provenance_sequence, actor_id, actor_kind, action, "
        f"audit_ref FROM {PROVENANCE}",
    ) == [
        (
            evidence_id,
            1,
            "core-service",
            "service",
            "source.ingested",
            read.job.identity.audit_reference,
        )
    ]

    # The barrier's own claim, made from the outside: the production search answers
    # with the artifact this import created.
    assert found(router, STAGED_KIND, request_id="req-search-1") == (evidence_id,)


def test_the_settled_job_publishes_an_ordered_two_event_history(
    owned: Served, router: ApplicationDispatcher
) -> None:
    """The event stream is the execution: started, then the terminal state."""
    stage(owned)
    job_id = start(router)
    owned.clock.advance_wall(1)
    executor(owned).run_pending()

    page = events(router, job_id, request_id="req-events-1")
    assert [event.sequence for event in page.events] == [0, 1]
    assert [event.state for event in page.events] == ["running", "succeeded"]
    assert page.snapshot_event_count == 2


# --- interruption between the durable steps -------------------------------------


def test_a_crash_after_the_evidence_commit_is_completed_by_the_recovered_attempt(
    owned: Served, router: ApplicationDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence committed, terminal observation lost, recovery finishes what is left.

    The interruption is injected where a crash would fall: after the business commit
    and the barrier, before the terminal observation. What has to hold afterwards is
    that the recovered attempt does *not* import again -- one artifact, its original
    identity, and an accounting that still reports this run as the one that created it.
    """

    def interrupted(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise RuntimeError("the service stopped before the terminal observation")

    stage(owned)
    job_id = start(router)
    owned.clock.advance_wall(1)

    monkeypatch.setattr(import_execution, "complete_application_job", interrupted)
    with pytest.raises(RuntimeError):
        executor(owned).run_pending()
    monkeypatch.undo()

    # The commit stood and nothing reported success.
    ((first_evidence_id,),) = rows(owned, f"SELECT evidence_id FROM {ARTIFACTS}")
    assert count(owned, OBSERVATIONS) == 0

    # A new owner, and the startup pass that requeues what the old one was holding.
    successor, generation = supersede(owned)
    owned.clock.advance_wall(1)
    recover_stranded_application_jobs(
        owned.connection,
        successor,
        workspace_id=WORKSPACE_ID,
        fencing_generation=generation,
        now_us=int(owned.clock.wall_time().timestamp() * 1_000_000),
        clock=owned.clock,
    )
    owned.clock.advance_wall(1)

    resumed = ImportJobExecutor(
        connection=owned.connection,
        identity=successor,
        workspace_id=WORKSPACE_ID,
        fencing_generation=generation,
        clock=owned.clock,
        blobs_root=owned.layout.blobs_path,
    )
    assert resumed.run_pending() == (job_id,)

    read = observed(router, job_id, request_id="req-get-2")
    assert read.job.state == "succeeded"
    assert read.job.latest_attempt is not None
    assert read.job.latest_attempt.attempt_number == 2
    reported = completion(read)
    assert (reported.evidence_records_created, reported.skipped_items) == (1, 0)

    # One artifact, and the one the interrupted attempt wrote.
    assert rows(owned, f"SELECT evidence_id FROM {ARTIFACTS}") == [(first_evidence_id,)]
    assert count(owned, PROVENANCE) == 1


# --- duplicate prevention --------------------------------------------------------


def test_a_second_pass_neither_reruns_a_settled_job_nor_duplicates_its_evidence(
    owned: Served, router: ApplicationDispatcher
) -> None:
    """A settled job is not pending, and the pass that finds none writes none."""
    stage(owned)
    job_id = start(router)
    owned.clock.advance_wall(1)
    assert executor(owned).run_pending() == (job_id,)
    owned.clock.advance_wall(1)

    assert executor(owned).run_pending() == ()
    assert count(owned, ARTIFACTS) == 1
    assert count(owned, PROVENANCE) == 1
    assert count(owned, OBSERVATIONS) == 1


def test_a_second_import_of_one_staged_source_skips_rather_than_duplicating(
    owned: Served, router: ApplicationDispatcher
) -> None:
    """One immutable source is one artifact, and the second run's accounting says so.

    The identity 0041 makes unique is the source's, not the run's, so the second job
    cannot create a second artifact -- and reporting it as created would be a claim
    about a row this run did not write. It reports a skip, which still accounts for
    the one item the descriptor discovered.
    """
    stage(owned)
    first = start(router)
    owned.clock.advance_wall(1)
    executor(owned).run_pending()
    owned.clock.advance_wall(1)

    second = start(router, request_id="req-import-2", key="idem-import-2")
    assert second != first
    owned.clock.advance_wall(1)
    assert executor(owned).run_pending() == (second,)

    reported = completion(observed(router, second, request_id="req-get-3"))
    assert (
        reported.discovered_items,
        reported.evidence_records_created,
        reported.skipped_items,
        reported.failed_items,
        reported.partial,
    ) == (1, 0, 1, 0, False)
    assert count(owned, ARTIFACTS) == 1
    # Still bound to the run that actually published it.
    assert rows(owned, f"SELECT import_run_id FROM {ARTIFACTS}") == [(first,)]


def test_two_staged_sources_are_two_artifacts(
    owned: Served, router: ApplicationDispatcher
) -> None:
    """The identity is derived per staged handle, so two handles do not collide."""
    stage(owned)
    stage(owned, staged_source_ref=OTHER_REF, digest=OTHER_DIGEST)
    first = start(router)
    second = start(
        router,
        request_id="req-import-2",
        key="idem-import-2",
        staged_source_ref=OTHER_REF,
        digest=OTHER_DIGEST,
    )
    owned.clock.advance_wall(1)

    assert set(executor(owned).run_pending()) == {first, second}
    assert count(owned, ARTIFACTS) == 2
    assert sorted(
        run for (run,) in rows(owned, f"SELECT import_run_id FROM {ARTIFACTS}")
    ) == sorted([first, second])


# --- the projection barrier ------------------------------------------------------


def test_a_projection_that_cannot_confirm_the_evidence_fails_the_attempt_retryably(
    owned: Served, router: ApplicationDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No success is reported over evidence `evidence.search` could not answer with.

    The commit stands, because it is durable and correct; what is refused is the
    *claim* that the import finished. The attempt records a retryable failure, the job
    publishes a recovery a caller can act on, and a later attempt over the same
    evidence completes it.
    """

    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise import_execution.StorageError("the projection could not be built")

    stage(owned)
    job_id = start(router)
    owned.clock.advance_wall(1)

    monkeypatch.setattr(import_execution, "build_search_projection", unavailable)
    assert executor(owned).run_pending() == (job_id,)
    monkeypatch.undo()

    read = observed(router, job_id, request_id="req-get-4")
    assert read.job.state == "failed"
    refused = failure(read)
    assert refused.code == "internal_recoverable"
    assert refused.retry_class == "retryable"
    assert read.job.control.recovery == "retryable"
    # The evidence is durable and the failure is only about proving it findable.
    assert count(owned, ARTIFACTS) == 1


def test_a_repaired_projection_lets_the_next_attempt_finish_the_same_evidence(
    owned: Served, router: ApplicationDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remaining steps, and only those, are what a later attempt has left to do."""

    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise import_execution.StorageError("the projection could not be built")

    stage(owned)
    job_id = start(router)
    owned.clock.advance_wall(1)
    monkeypatch.setattr(import_execution, "build_search_projection", unavailable)
    executor(owned).run_pending()
    monkeypatch.undo()
    ((evidence_id,),) = rows(owned, f"SELECT evidence_id FROM {ARTIFACTS}")

    # `job.retry` is the recovery the failed handle advertised; requeueing through it
    # is what a caller does, and the next pass then finds a queued job.
    owned.clock.advance_wall(1)
    answered(
        router.dispatch(
            request(
                JOB_RETRY_OPERATION,
                {"job_id": job_id},
                request_id="req-retry-1",
                key="idem-retry-1",
            )
        )
    )
    owned.clock.advance_wall(1)
    assert executor(owned).run_pending() == (job_id,)

    read = observed(router, job_id, request_id="req-get-5")
    assert read.job.state == "succeeded"
    reported = completion(read)
    assert reported.evidence_records_created == 1
    assert count(owned, ARTIFACTS) == 1
    assert found(router, STAGED_KIND, request_id="req-search-2") == (evidence_id,)


def test_an_unanticipated_failure_becomes_one_durable_failed_attempt(
    owned: Served, router: ApplicationDispatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure nothing here predicted still terminalizes, and says nothing about why.

    This is what keeps a pass bounded rather than a feature: a job left claimed after
    an exception would be selected again by the next request's pass, and by every one
    after it. The recorded message is the module's own frozen sentence, so whatever the
    failure quoted stays out of a durable record a caller reads.
    """

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("/etc/passwd and a bearer token walked into a message")

    stage(owned)
    job_id = start(router)
    owned.clock.advance_wall(1)
    monkeypatch.setattr(import_execution, "require_staged_import_source", broken)
    assert executor(owned).run_pending() == (job_id,)
    monkeypatch.undo()

    read = observed(router, job_id, request_id="req-get-8")
    assert read.job.state == "failed"
    refused = failure(read)
    assert refused.code == "internal_non_recoverable"
    assert refused.retry_class == "non_retryable"
    assert "passwd" not in refused.message and "token" not in refused.message
    assert count(owned, ARTIFACTS) == 0

    # And the pass that follows finds nothing pending rather than the same job again.
    owned.clock.advance_wall(1)
    assert executor(owned).run_pending() == ()


# --- inertness --------------------------------------------------------------------


def test_hostile_staged_metadata_is_never_read_repeated_or_indexed(
    owned: Served, router: ApplicationDispatcher
) -> None:
    """A staged row spelling a path, a URL and an instruction produces none of them.

    Execution reads the six fields the accepted descriptor declares and re-matches them
    against the staged row. `original_metadata_json` is not one of them, and this is
    the assertion that it is not consulted, not copied and not indexed -- the artifact's
    own metadata is written by this service, from the handle and the kind.
    """
    stage(owned, metadata=HOSTILE_METADATA)
    job_id = start(router)
    owned.clock.advance_wall(1)
    executor(owned).run_pending()

    read = observed(router, job_id, request_id="req-get-6")
    assert read.job.state == "succeeded"

    ((native_id, metadata),) = rows(
        owned, f"SELECT source_native_id, original_metadata_json FROM {ARTIFACTS}"
    )
    surfaces = (
        metadata,
        native_id,
        str(completion(read).to_wire()),
        str([event.to_wire() for event in events(router, job_id, request_id="req-ev-2").events]),
    )
    for token in HOSTILE_TOKENS:
        for surface in surfaces:
            assert token not in surface, "hostile staged metadata reached a surface"
    for token in ("parser", "hunter2", "https://", "/etc/"):
        assert token not in metadata, "the artifact repeated the staged row's metadata"

    # And it is not searchable either, which is the index's side of the same claim.
    for token in HOSTILE_TOKENS:
        assert found(router, token, request_id=f"req-search-{token}") == ()


# --- fence loss ---------------------------------------------------------------------


def test_an_executor_whose_fence_has_advanced_writes_nothing(
    owned: Served, router: ApplicationDispatcher
) -> None:
    """A superseded instance does not execute, does not fail the job, and does not raise.

    The job it was holding is left exactly as it was, for whoever owns the workspace
    now to recover: recording a failure against it would be a superseded instance
    writing a verdict on work it can no longer see the end of.
    """
    stage(owned)
    job_id = start(router)
    superseded = executor(owned)
    _successor, _generation = supersede(owned)
    owned.clock.advance_wall(1)

    assert superseded.run_pending() == ()
    assert count(owned, ARTIFACTS) == 0
    assert count(owned, OBSERVATIONS) == 0
    assert rows(
        owned, "SELECT state FROM omnivia_durable_jobs WHERE job_id = ?", job_id
    ) == [("claimed",)]


# --- the whole handle stays contract-valid throughout ---------------------------------


def test_every_published_handle_validates_against_the_contract(
    owned: Served, router: ApplicationDispatcher
) -> None:
    """`job.get`'s own validator, run over the settled handle and its terminal result.

    The handler already runs it, so this asserts nothing new about a single read -- it
    is here because it is the one check that ties the accounting, the attempt history,
    the accepted descriptor and the terminal branch together, and a change that made
    any two of them disagree would pass every assertion above.
    """
    stage(owned)
    job_id = start(router)
    owned.clock.advance_wall(1)
    executor(owned).run_pending()

    read = observed(router, job_id, request_id="req-get-7")
    accepted = read_accepted_import_source(
        owned.connection, workspace_id=WORKSPACE_ID, job_id=job_id
    )
    assert accepted is not None
    validate_job_get_result(
        read,
        decode_job_get_input({"job_id": job_id}),
        accepted_import_source=request_import_source(accepted),
    )
