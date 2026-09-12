"""Service-owned execution of the `ingestion.import` durable application job.

`import.start` settles a job inside the caller's mutation transaction and hands back
its handle; nothing about the import has run at that point, and nothing may run
there. A mutation that also did the work would make an asynchronous operation
synchronous and would tie the job's outcome to the connection of whoever asked for
it. This module is the other half: the service's own execution of that job, under
the service's identity and the fencing generation this instance holds, driven by the
local transport's between-request hook rather than by anything a caller holds. That
is what makes the R004 §8.4 claim true rather than aspirational -- revoking the MCP
principal that started a job stops later observations and does not stop the job.

**What executing one of these jobs is, and what it deliberately is not.** The staged
descriptor `import.start` accepted already names one verified, immutable blob in this
workspace: staging is on the far side of the milestone boundary and produced the
bytes, the digest and the verification before the job existed. So execution publishes
exactly one L0 evidence artifact pointing at that blob. It is not an archive reader
and not a parser lane: nothing here opens a path, fetches a URL, chooses a parser,
reads a credential or looks at a byte of the staged row's `original_metadata_json`.
The only facts consulted are the six the accepted descriptor declares and the staged
row those six are re-matched against.

**Three durable steps, in this order, and the order is the property.**

1. The descriptor is re-validated against the staged row *at execution* rather than
   trusted from admission. A job recovered after a crash executes against a workspace
   that has had time to move, and `require_staged_import_source` is the same predicate
   admission used, so the two cannot drift.
2. The artifact and its first provenance event commit together in one fenced
   transaction, under a source identity derived from the staged handle. Derivation is
   what makes a retry safe, and it is not the only thing making it safe: 0041's unique
   index covers exactly that identity tuple, so a second attempt could not create a
   second artifact even if this module were wrong about having created the first.
3. The projection barrier, after that commit and outside it, and the terminal
   observation only after the barrier passes. A job may not report success until the
   evidence it created is findable by `evidence.search`, and the two are separate
   transactions for the reason `evidence.capture`'s barrier is: nesting the projection
   lifecycle inside the business commit would either deadlock the single write
   connection or roll back durable evidence because an index lagged.

Every step is resumable from the database alone. An attempt that committed the
evidence and then died before the barrier or before the terminal observation is
finished by the next one, which finds the artifact by its derived identity and carries
on from there instead of writing it again. Nothing is remembered in this process.

**No caller value reaches a durable message.** The two refusals below are frozen
sentences, the metadata this module writes onto the artifact carries the server-issued
staging handle and the source kind and nothing else, and the failure recorded on an
attempt names the rule rather than the row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    RETRY_CLASS_RETRYABLE,
    ImportCompletionResult,
    ImportSourceDescriptor,
    decode_import_start_input,
    to_canonical_json,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.jobs import (
    claim_application_job,
    complete_application_job,
    fail_application_job,
)
from omnivia_core_runtime.service.operations import OperationError
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.jobs import (
    read_accepted_import_source,
    require_staged_import_source,
)
from omnivia_core_runtime.storage.projections.fts import (
    build_search_projection,
    open_search_projection,
)

#: The durable job kind this executor consumes, and the terminal result kind
#: `import.start` bound to it when it recorded the job's metadata. Both are stated
#: here so the query below and the observation written at the end read the same words
#: the storage layer wrote.
IMPORT_JOB_KIND: Final = "ingestion.import"
IMPORT_RESULT_KIND: Final = "import_completion"

#: How many pending import jobs one pass may execute. A bound rather than a drain to
#: exhaustion, because this runs on the transport's serving thread between requests:
#: an unbounded pass would hold the accept loop for as long as the queue is long, and
#: whatever it does not reach is picked up by the next request's pass.
DEFAULT_EXECUTION_BUDGET: Final = 8

#: Who this evidence is attributed to. The Core service, on both the actor id and the
#: actor kind, because the service is what executed the import -- the principal that
#: started the job is preserved separately, through the job's own `audit_ref`, which
#: the provenance event carries. Spelt as `ingestion_coordinator` spells it, since a
#: reader comparing two service-written provenance rows should see one actor.
_SERVICE_ACTOR: Final = "core-service"
_SERVICE_ACTOR_KIND: Final = "service"
_INGEST_ACTION: Final = "source.ingested"

#: The artifact's own status surface. The same three an `evidence.capture` writes: the
#: bytes are durable and addressed, and nothing has parsed them.
_SENSITIVITY: Final = "private"
_PARSER_STATUS: Final = "not_parsed"
_INGESTION_STATUS: Final = "ingested"

_MESSAGE_NO_CLAIM: Final = "this import job has no accepted staged descriptor"
_MESSAGE_NOT_SEARCHABLE: Final = (
    "the evidence this import created did not become findable by evidence.search"
)
_MESSAGE_UNEXPECTED: Final = "this import job could not be executed"

#: One pending job of this workspace's own: either already claimed by this instance at
#: this generation -- which is the state `import.start` leaves a fresh job in -- or
#: queued, which is the state startup recovery leaves an interrupted one in. Ordered by
#: the job's own creation instant so the queue is served oldest first.
_PENDING_IMPORT_JOBS: Final = (
    "SELECT j.job_id, j.state FROM omnivia_durable_jobs j "
    "JOIN omnivia_job_application_metadata m ON m.job_id = j.job_id "
    "WHERE m.workspace_id = ? AND m.job_kind = ? AND ("
    "j.state = 'queued' OR (j.state = 'claimed' "
    "AND j.claimed_by_service_instance = ? AND j.fencing_generation = ?)) "
    "ORDER BY m.created_at_us, j.job_id LIMIT 1"
)


def _derived(prefix: str, *parts: str) -> str:
    """A bounded identifier derived from the facts it names.

    The derivation `ingestion_coordinator` already uses for connector evidence, and it
    is here for the same reason: a second attempt at one fact computes the identifier
    the first attempt wrote, so a retry addresses that row instead of racing it.

    Hashed rather than composed. A staged handle is up to 512 printable bytes while
    `source_native_id` is 128 characters of `[A-Za-z0-9._:-]`, so spelling a handle
    into the identity would be refused by the schema for perfectly ordinary handles --
    and a derived id cannot carry a path, a URL or a secret out of one either.
    """
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"{prefix}-{digest}"


@dataclass(frozen=True)
class ImportJobExecutor:
    """One instance's execution of its own workspace's `ingestion.import` jobs.

    Every field is a fact this service instance holds and no request can reach: the
    exclusive connection, this service identity, the workspace the lease names, the
    generation it was granted at, the clock and the workspace's blob root. A second
    executor built from anything else would be a writer this workspace has not
    authorised, which is why there is no constructor that takes less than this.
    """

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    workspace_id: str
    fencing_generation: int
    clock: Clock
    blobs_root: Path

    def run_pending(self, *, budget: int = DEFAULT_EXECUTION_BUDGET) -> tuple[str, ...]:
        """Execute at most `budget` pending import jobs, and report which ran.

        Bounded and total. It is driven by the transport's sole accept loop, so it
        neither runs for an unbounded time nor raises: a job that cannot be executed
        is failed durably, and a failure that means this instance no longer owns the
        workspace ends the pass with nothing written rather than being recorded as
        that job's fault.
        """
        executed: list[str] = []
        try:
            while len(executed) < budget:
                job_id = self._next_pending()
                if job_id is None:
                    break
                self._execute(job_id)
                executed.append(job_id)
        except (StorageError, sqlite3.Error):
            # Fence loss above all -- `StaleGeneration` is one of these. A superseded
            # instance writes nothing further, and it does not report work it could
            # not finish. The next owner's startup recovery requeues what was open.
            # Contention on the connection this pass shares with the lease-renewal loop
            # is the other member of the set, and it is the same answer: stop, and let
            # the next request's pass do the work.
            pass
        return tuple(executed)

    # --- claiming -------------------------------------------------------------

    def _next_pending(self) -> str | None:
        """The next import job this instance may execute, claimed and ready, or `None`.

        Two states qualify and they arrive by different routes. A job `import.start`
        has just settled is already `claimed` by this instance at this generation with
        its first attempt open, so it is returned as it stands -- claiming it again
        would open a second attempt for one execution. A job startup recovery requeued
        after a crash is `queued`, and goes through `claim_application_job`, which is
        what opens its next attempt and writes the event saying so.
        """
        row = self.connection.execute(
            _PENDING_IMPORT_JOBS,
            (
                self.workspace_id,
                IMPORT_JOB_KIND,
                self.identity.service_instance_id,
                self.fencing_generation,
            ),
        ).fetchone()
        if row is None:
            return None
        job_id = str(row[0])
        if str(row[1]) == "claimed":
            return job_id
        claimed = claim_application_job(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            clock=self.clock,
            job_id=job_id,
        )
        return None if claimed is None else job_id

    # --- one job --------------------------------------------------------------

    def _execute(self, job_id: str) -> None:
        """Carry one claimed job to a terminal observation, whichever one it earns.

        Both branches terminalize, and that is what keeps the pass bounded: a job left
        `claimed` would be selected again by the next pass, and again by the one after
        it. A refusal this module states becomes that attempt's error verbatim; a
        failure it did not anticipate becomes one frozen internal failure rather than
        an exception escaping into the accept loop, because the alternative to writing
        a durable failure here is a job that is retried forever and never says why.
        `BaseException` is deliberately not caught.

        A `StorageError` is the one thing that is *not* this job's fault and is
        re-raised rather than recorded: a lost fence, or a connection this pass could
        not use, says nothing about the import and this instance may not write a
        verdict on work it can no longer see the end of. `run_pending` ends the pass on
        it and leaves the job for whoever owns the workspace next.
        """
        error: Mapping[str, object] | None = None
        result: Mapping[str, Any] | None = None
        try:
            result = self._import(job_id)
        except OperationError as refused:
            error = {
                "code": refused.code,
                "message": refused.message,
                "retry_class": refused.retry_class,
            }
        except (StorageError, sqlite3.Error):
            raise
        except Exception:  # noqa: BLE001 - see the docstring above
            error = {
                "code": ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                "message": _MESSAGE_UNEXPECTED,
                "retry_class": "non_retryable",
            }
        if error is not None:
            fail_application_job(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                job_id=job_id,
                fencing_generation=self.fencing_generation,
                clock=self.clock,
                error=error,
            )
            return
        assert result is not None
        complete_application_job(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            job_id=job_id,
            fencing_generation=self.fencing_generation,
            clock=self.clock,
            result_kind=IMPORT_RESULT_KIND,
            result=result,
        )

    def _import(self, job_id: str) -> Mapping[str, Any]:
        """The import itself: re-validate, publish, prove findable, report.

        The accounting is a statement about this run and not a constant. One item is
        discovered, because the staged descriptor names one immutable blob; that item
        is *created* when this job's own execution wrote the artifact, and *skipped*
        when the workspace already held evidence of that exact source from an earlier
        import run -- the same source, imported twice, is one artifact, which is the
        rule 0041 makes the database's rather than a caller's. Nothing here can fail
        an item without failing the job, so `failed_items` is zero and `partial` with
        it.
        """
        accepted = read_accepted_import_source(
            self.connection, workspace_id=self.workspace_id, job_id=job_id
        )
        if accepted is None:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_CLAIM)
        # Decoded through the contract rather than read field by field, so the
        # descriptor this run validates and the descriptor it reports are one value.
        claim = decode_import_start_input({"source": dict(accepted)})
        require_staged_import_source(
            self.connection, workspace_id=self.workspace_id, claim=claim
        )
        source = claim.source
        evidence_id, created = self._publish_evidence(job_id, source)
        self._require_findable(evidence_id)
        return ImportCompletionResult(
            import_run_id=job_id,
            source=source,
            discovered_items=1,
            evidence_records_created=1 if created else 0,
            skipped_items=0 if created else 1,
            failed_items=0,
            partial=False,
        ).to_wire()

    # --- the durable business commit -----------------------------------------

    def _publish_evidence(
        self, job_id: str, source: ImportSourceDescriptor
    ) -> tuple[str, bool]:
        """Publish this import's one artifact, or adopt the one already published.

        The identity is derived from the workspace and the staged handle, so the row
        this looks for is the row a previous attempt of this job would have written --
        addressed by primary key, never searched for. What the lookup decides is only
        the *accounting*: an artifact this job's `import_run_id` already names was
        created by this job on an earlier attempt and is reported as created, and one
        naming another run is evidence of the same source that another import already
        published, which this run skips rather than duplicates.

        The artifact and its provenance event are one transaction because they are one
        fact. The schema does not require the event -- 0008 says so in as many words --
        but an artifact with no provenance history is one `validate_evidence_artifact`
        refuses to return, so an artifact written without one could never be read.
        """
        evidence_id = _derived("evd", self.workspace_id, source.staged_source_ref)
        native_id = _derived("imp", self.workspace_id, source.staged_source_ref)
        now_us = int(self.clock.wall_time().timestamp() * 1_000_000)
        metadata = to_canonical_json(
            {
                "import": source.source_kind,
                "staged_source_ref": source.staged_source_ref,
            }
        )
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            existing = self.connection.execute(
                "SELECT import_run_id FROM omnivia_evidence_artifacts "
                "WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if existing is not None:
                return evidence_id, existing[0] is not None and str(existing[0]) == job_id
            self.connection.execute(
                "INSERT INTO omnivia_evidence_artifacts "
                "(evidence_id, workspace_id, source_kind, source_native_id, "
                "source_locator, source_retrieved_at_us, event_at_us, observed_at_us, "
                "ingested_at_us, recorded_at_us, content_checksum, blob_content_digest, "
                "media_type, original_metadata_json, original_metadata_digest, "
                "sensitivity, parser_status, ingestion_status, staged_source_ref, "
                "import_run_id) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id,
                    self.workspace_id,
                    source.source_kind,
                    native_id,
                    now_us,
                    now_us,
                    source.content_checksum,
                    source.content_checksum,
                    source.media_type,
                    metadata,
                    f"sha256:{sha256(metadata.encode('utf-8')).hexdigest()}",
                    _SENSITIVITY,
                    _PARSER_STATUS,
                    _INGESTION_STATUS,
                    source.staged_source_ref,
                    job_id,
                ),
            )
            self.connection.execute(
                "INSERT INTO omnivia_evidence_provenance_events "
                "(provenance_event_id, evidence_id, workspace_id, provenance_sequence, "
                "actor_id, actor_kind, action, occurred_at_us, reason_code, "
                "reason_comment, parser_status, ingestion_status, "
                "tombstoned_observation, source_kind, source_native_id, audit_ref) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, NULL, NULL, ?, ?, 0, ?, ?, ?)",
                (
                    _derived("prv", self.workspace_id, source.staged_source_ref, "1"),
                    evidence_id,
                    self.workspace_id,
                    _SERVICE_ACTOR,
                    _SERVICE_ACTOR_KIND,
                    _INGEST_ACTION,
                    now_us,
                    _PARSER_STATUS,
                    _INGESTION_STATUS,
                    source.source_kind,
                    native_id,
                    self._origin_audit_ref(job_id),
                ),
            )
        return evidence_id, True

    def _origin_audit_ref(self, job_id: str) -> str | None:
        """The `import.start` audit reference this job was settled under, or `None`.

        The lineage half of the attribution: the actor on the provenance event is this
        service, because the service is what executed the import, and this is how the
        request that authorised it is still reachable from the evidence. Optional in
        the schema, so a job whose metadata row is unreadable produces evidence with no
        correlation rather than no evidence.
        """
        row = self.connection.execute(
            "SELECT audit_ref FROM omnivia_job_application_metadata "
            "WHERE workspace_id = ? AND job_id = ?",
            (self.workspace_id, job_id),
        ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    # --- the projection barrier ----------------------------------------------

    def _require_findable(self, evidence_id: str) -> None:
        """Gate A for an import: refuse to report success the search would contradict.

        The same barrier `evidence.capture` runs after its own commit, and the same
        two calls: the idempotent builder brings the projection level with the
        workspace this commit just moved, and the open re-materialises this session's
        material so `evidence.search` answers from it rather than from the run before
        it. What is asserted is membership of that material, which is exactly what
        `SearchProjection.project` requires of an authorized candidate -- an id the
        material has no document for is a refusal at every later read, so reporting
        success on one would be reporting a success the next search denies.

        Membership, not `content_indexed`. A capture's barrier asks whether the words
        the caller submitted reached the index, because the caller submitted words. An
        import names a staged blob whose bytes this milestone does not read and which
        may not be text at all, so the honest claim is the one the identity surface
        supports: this artifact is in the projection `evidence.search` serves.

        Nothing here falls back to the authoritative table. A projection that has not
        caught up is a retryable failure of this attempt, recorded as one, and the next
        attempt finds the evidence already committed and runs only what is left.
        """
        failed = False
        try:
            build_search_projection(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
                now_us=int(self.clock.wall_time().timestamp() * 1_000_000),
            )
            projection = open_search_projection(
                self.connection,
                workspace_id=self.workspace_id,
                blobs_root=self.blobs_root,
            )
            failed = evidence_id not in projection.material
        except (StorageError, OSError):
            # `ProjectionError` and `StaleGeneration` are both `StorageError`, and both
            # mean the same thing at this point: this attempt cannot show the evidence
            # is findable. They part company one step later rather than here -- a lost
            # fence refuses the failed attempt's own write too, so it ends the pass with
            # nothing recorded, which is the right answer for an instance that no longer
            # owns the workspace. Contained rather than chained: the projection's own
            # messages name run ids and checkpoints, and this one is written to a
            # durable attempt a caller reads.
            failed = True
        if not failed:
            return
        raise OperationError(
            ERROR_CODE_INTERNAL_RECOVERABLE,
            _MESSAGE_NOT_SEARCHABLE,
            retry_class=RETRY_CLASS_RETRYABLE,
        )


__all__ = [
    "DEFAULT_EXECUTION_BUDGET",
    "IMPORT_JOB_KIND",
    "IMPORT_RESULT_KIND",
    "ImportJobExecutor",
]
