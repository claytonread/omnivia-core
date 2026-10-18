"""Code-analysis policy, import and status (TECH-CORE-RETRIEVAL-CODE-001, W13-C).

W12 made one storage seam that turns a verified batch into governed, applicable code
facts, and W13-A/B made `code.query` read them. Three operations connect them to a caller,
and none of them adds an authority of its own:

* `code.policy.register` -- an owner authorises one versioned, deterministic acceptance
  policy. It is the W12 seam's `register_import_policy` run as the domain callback of the
  executor, so the authoriser is the settlement's authenticated principal and nothing the
  document says. There is no default policy and enabling analysis registers none.
* `code.import` -- run one producer this service *declares*, under its declared profile,
  over one sealed, complete captured snapshot, and activate what one registered policy
  accepts. The request carries identities only: no path, no executable code, no profile
  body, no principal, no workspace. The analysis reads the sealed capture index and the
  content-addressed blobs and nothing else, and it runs *before* the write transaction,
  so a slow, failed or cancelled analysis writes nothing and leaves the applicable graph
  exactly as it was. The import itself is the W12 seam inside `execute_mutation`: one
  fenced transaction holds the audit, the idempotency claim, the staged import, the file
  evidence and the activation, and a refusal anywhere rolls all of it back.
* `code.status` -- a read beside `code.query`: which declared producers this service can
  run, which analysis is applicable for a repository, whether it answers for a given
  snapshot, how much of that snapshot it covers and whether a newer capture has made it
  stale. A caller discovers bindings here instead of needing a receipt.

**Replays, conflicts and stale attempts.** The executor's idempotency claim is the replay
authority: the same key and request returns the stored receipt, a different request under
the same key is `idempotency_conflict`, and neither runs the producer again -- a W12
receipt keyed by the caller and key is checked before analysis for exactly that. The
scope's expected activation is the envelope's `mutation_precondition`, the decimal
activation sequence `code.status` reports; an import planned against an analysis another
import has since replaced is `mutation_precondition_failed`, and W12's own fence checks
the same thing again inside the transaction. The request's deadline is honoured between
files: an analysis still running when it passes is abandoned as `deadline_exceeded` with
nothing written, and a later attempt under the same key starts afresh.

**Only Python runs here.** The TypeScript producer is declared, so its availability is
stated rather than hidden, but this service has no verified Node runtime to run its
packaged analyser on: a request for it is `dependency_unavailable` with
`runtime_not_configured`, never a silent fallback and never a guess at `PATH`.

**The file evidence must be searchable before success is reported.** An import adds one L0
artifact per captured file it read, which moves the evidence checkpoint, so it runs the
same post-commit barrier `evidence.capture` runs: the search projection is brought level
and must hold every one of those artifacts. A barrier that fails is
`internal_recoverable`; the import stands, and replaying the same key re-runs only the
barrier.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_DEADLINE_EXCEEDED,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    RETRY_CLASS_RETRYABLE,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    CodeAnalysisBinding,
    CodeAnalysisCoverage,
    CodeAnalysisStatus,
    CodeImportInput,
    CodeImportResult,
    CodePolicyReference,
    CodePolicyRegisterInput,
    CodePolicyRegisterResult,
    CodeSnapshotFreshness,
    CodeSnapshotStatus,
    CodeStatusInput,
    CodeStatusResult,
    CodeUnsupportedFiles,
    ContractDecodeError,
    ContractSemanticError,
    idempotency_equivalence,
    validate_code_import_input,
    validate_code_policy_register_input,
    validate_code_status_input,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock, SystemClock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.handlers.code import (
    _timestamp,
    file_admitted,
    newest_visible_capture,
)
from omnivia_core_runtime.service.mutation import (
    MutationIdempotencyConflict,
    MutationPreconditionFailed,
    MutationSeamFault,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
    application_refusal,
)
from omnivia_core_runtime.storage import code_analysis_python, code_analysis_typescript
from omnivia_core_runtime.storage.code_analysis_candidates import (
    MAX_CANDIDATES,
    CodeAnalysisBindingError,
    VerifiedCodeAnalysisBatch,
)
from omnivia_core_runtime.storage.code_analysis_import import (
    CodeAnalysisImportConflict,
    CodeAnalysisImportError,
    CodeAnalysisImportPolicy,
    active_import_id,
    current_activation_sequence,
    import_code_analysis,
    import_file_evidence_ids,
    import_receipt,
    read_import_policies,
    read_import_policy,
    register_import_policy,
)
from omnivia_core_runtime.storage.code_query import (
    read_active_scope,
    read_import_files,
    read_snapshot_files,
)
from omnivia_core_runtime.storage.connection import StorageError, authorised
from omnivia_core_runtime.storage.engineering_source import covered_snapshot
from omnivia_core_runtime.storage.memory import IdentifierAllocator, random_identifier
from omnivia_core_runtime.storage.projections.fts import (
    build_search_projection,
    open_search_projection,
)
from omnivia_core_runtime.storage.repository import read_evidence_labels
from omnivia_core_runtime.storage.retrieval import (
    EvidenceLabelGrant,
    local_owner_label_grant,
)

CODE_STATUS_OPERATION: Final = "code.status"

#: `CodeAnalysisStatus` vocabulary.
AVAILABLE: Final = "available"
UNAVAILABLE: Final = "unavailable"
RUNTIME_NOT_CONFIGURED: Final = "runtime_not_configured"
READY: Final = "ready"
NOT_READY: Final = "not_ready"
NO_ACTIVE_IMPORT: Final = "no_active_import"
SNAPSHOT_NOT_ACTIVE: Final = "snapshot_not_active"
NO_ADMITTED_SOURCE: Final = "no_admitted_source"
#: `CodeSnapshotFreshness` vocabulary. Core reads captures, never the live checkout.
CURRENT: Final = "current"
STALE: Final = "stale"
WORKING_TREE_NOT_OBSERVED: Final = "not_observed"
#: The most suffix groups `CodeSnapshotStatus.unsupported` lists.
MAX_UNSUPPORTED_GROUPS: Final = 64

_POLICY_KEYS: Final = frozenset(
    {
        "policy_id",
        "policy_version",
        "producers",
        "accepted_node_kinds",
        "accepted_relationships",
        "allow_reduced_coverage",
        "max_candidates",
    }
)
_IMPORT_KEYS: Final = frozenset(
    {"repository_id", "snapshot_id", "producer_id", "profile_digest", "policy_id", "policy_version"}
)
_STATUS_KEYS: Final = frozenset({"repository_id", "snapshot_id"})

_MESSAGE_INVALID: Final = "the request payload is not valid for this code-analysis operation"
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"
_MESSAGE_POLICY_INVALID: Final = (
    "the acceptance policy names a producer, node kind, relationship or strength this "
    "service does not analyse, or repeats an entry"
)
_MESSAGE_POLICY_CONFLICT: Final = "that policy id and version is already a different policy"
_MESSAGE_NOT_DECLARED: Final = "no producer profile this service declares matches the request"
_MESSAGE_RUNTIME: Final = "this service cannot run that producer: its runtime is not configured"
_MESSAGE_POLICY_NOT_FOUND: Final = "no acceptance policy of that id and version is registered"
_MESSAGE_POLICY_DOES_NOT_ADMIT: Final = (
    "that acceptance policy does not admit this producer build and profile"
)
_MESSAGE_SNAPSHOT_NOT_FOUND: Final = "no sealed snapshot of that repository by that id"
_MESSAGE_SNAPSHOT_NOT_ANALYSABLE: Final = (
    "that snapshot is not one complete sealed capture, so it cannot be analysed"
)
_MESSAGE_SNAPSHOT_UNVERIFIABLE: Final = (
    "that snapshot's captured source could not be re-verified against its capture"
)
_MESSAGE_NO_SOURCE: Final = "that snapshot holds no source file this producer analyses"
_MESSAGE_TOO_LARGE: Final = "that snapshot exceeds the declared profile's analysis bounds"
_MESSAGE_DEADLINE: Final = "the request deadline passed before the analysis finished"
_MESSAGE_IMPORT_REFUSED: Final = (
    "the import was refused by the activation rules: the applicable analysis moved, or the "
    "cutover drops coverage the policy does not allow"
)
_MESSAGE_PRECONDITION: Final = (
    "the scope's current activation is not the one this import expected to replace"
)
_MESSAGE_NOT_SEARCHABLE: Final = (
    "the import settled, but its file evidence is not yet in the search projection; "
    "replay the same request to finish it"
)


@dataclass(frozen=True, slots=True)
class DeclaredProducer:
    """One producer and profile this service declares, and whether it can run it here.

    `analyse` is None for a producer whose runtime this service does not have; such a
    producer is still declared, so its absence is a stated capability rather than a
    language that silently looks unanalysed.
    """

    producer_id: str
    producer_version: str
    profile_digest: str
    languages: tuple[str, ...]
    #: Whether the profile analyses one repository-relative path as source.
    admits: Callable[[str], bool]
    #: Every suffix the profile reads at all, source or configuration.
    suffixes: tuple[str, ...]
    analyse: Callable[..., VerifiedCodeAnalysisBatch | None] | None
    unavailable_reason: str | None = None

    @property
    def lock(self) -> tuple[str, str, str]:
        return (self.producer_id, self.producer_version, self.profile_digest)


def _analyse_python(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    repository_id: str,
    snapshot_id: str,
    blobs_root: Path,
    heartbeat: Callable[[], object],
) -> VerifiedCodeAnalysisBatch | None:
    profile = code_analysis_python.DEFAULT_PYTHON_PROFILE
    return code_analysis_python.analyse_python_snapshot(
        connection,
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        blobs_root=blobs_root,
        profile=profile,
        expected_profile_digest=profile.digest,
        heartbeat=heartbeat,
    ).verified


_PYTHON_PROFILE: Final = code_analysis_python.DEFAULT_PYTHON_PROFILE
_TYPESCRIPT_PROFILE: Final = code_analysis_typescript.DEFAULT_TYPESCRIPT_PROFILE

#: Every producer profile this service declares, in the order `code.status` reports them.
DECLARED_PRODUCERS: Final[tuple[DeclaredProducer, ...]] = (
    DeclaredProducer(
        producer_id=_PYTHON_PROFILE.producer_id,
        producer_version=_PYTHON_PROFILE.producer_version,
        profile_digest=_PYTHON_PROFILE.digest,
        languages=("python",),
        admits=_PYTHON_PROFILE.admits,
        suffixes=_PYTHON_PROFILE.include_suffixes,
        analyse=_analyse_python,
    ),
    DeclaredProducer(
        producer_id=_TYPESCRIPT_PROFILE.producer_id,
        producer_version=_TYPESCRIPT_PROFILE.producer_version,
        profile_digest=_TYPESCRIPT_PROFILE.digest,
        languages=("javascript", "typescript"),
        admits=lambda path: _TYPESCRIPT_PROFILE.role(path) == "source",
        suffixes=_TYPESCRIPT_PROFILE.include_suffixes + _TYPESCRIPT_PROFILE.config_suffixes,
        analyse=None,
        unavailable_reason=RUNTIME_NOT_CONFIGURED,
    ),
)


def declared_producer(producer_id: str, profile_digest: str) -> DeclaredProducer | None:
    return next(
        (
            declared
            for declared in DECLARED_PRODUCERS
            if (declared.producer_id, declared.profile_digest) == (producer_id, profile_digest)
        ),
        None,
    )


def import_idempotency_key(principal_id: str, idempotency_key: str) -> str:
    """The W12 activation key of one caller's idempotency key.

    W12 keys activations per workspace and the executor keys claims per principal, so the
    caller is folded in: two principals reusing one key are two imports, never one.
    """
    return "cik-" + hashlib.sha256(f"{principal_id}\x00{idempotency_key}".encode()).hexdigest()


class _Abandoned(Exception):
    """The request deadline passed while the analysis was still running."""


def _decoded(context: OperationContext, keys: frozenset[str], kind: Any, validate: Any) -> Any:
    """`context.request.input` as `kind`, validated, or `invalid_request`.

    The raw keys are held to the declared set first: the generated decoder drops unknown
    fields, so a `workspace_id`, a path or a profile body smuggled into the payload would
    otherwise be silently ignored rather than refused.
    """
    raw = context.request.input
    decoded = None
    if isinstance(raw, Mapping) and set(raw) <= keys:
        try:
            candidate = kind.from_wire(raw)
            validate(candidate)
            decoded = candidate
        except (ContractDecodeError, ContractSemanticError):
            decoded = None
    if decoded is None:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
    return decoded


def _grant(context: OperationContext) -> EvidenceLabelGrant:
    return local_owner_label_grant(
        principal_id=context.principal,
        workspace_id=context.workspace_id,
        granted_workspace=context.workspace_id,
    )


def _freshness(
    connection: Any,
    *,
    workspace_id: str,
    snapshot_id: str,
    grant: EvidenceLabelGrant,
    evidence_labels: Mapping[str, tuple[str, ...]],
) -> CodeSnapshotFreshness:
    newest = newest_visible_capture(
        connection,
        workspace_id=workspace_id,
        snapshot_id=snapshot_id,
        grant=grant,
        evidence_labels=evidence_labels,
    )
    return CodeSnapshotFreshness(
        state=CURRENT if newest is None else STALE,
        snapshot_id=snapshot_id,
        newest_snapshot_id=snapshot_id if newest is None else newest,
        working_tree=WORKING_TREE_NOT_OBSERVED,
    )


class CodeAnalysisHandlers:
    """`code.policy.register` and `code.import`, in the engineering family's session."""

    def __init__(
        self,
        service: Any,
        session: AuthenticatedSession | None = None,
        binding: ServiceBinding | None = None,
        clock: Clock | None = None,
        allocate_identifier: IdentifierAllocator = random_identifier,
    ) -> None:
        self.service = service
        self._issued_session = session
        self._issued_binding = binding
        self.clock = SystemClock() if clock is None else clock
        self.allocate_identifier = allocate_identifier

    def _storage(self) -> tuple[Any, Any, Any]:
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        if (
            connection is None
            or identity is None
            or self._issued_session is None
            or self._issued_binding is None
        ):
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        guard = read_guard(connection)
        if guard is None:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        return connection, identity, guard

    def _blobs_root(self) -> Path:
        blobs_root = getattr(getattr(self.service, "layout", None), "blobs_path", None)
        if not isinstance(blobs_root, Path):
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        return blobs_root

    def _execute(
        self,
        context: OperationContext,
        request: Any,
        storage: tuple[Any, Any, Any],
        mutate: Callable[[sqlite3.Connection, MutationSettlementContext], Mapping[str, Any]],
        result_type: Any,
        precondition: Callable[[sqlite3.Connection], str | None] | None = None,
    ) -> Any:
        connection, identity, guard = storage
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )
        assert self._issued_session is not None and self._issued_binding is not None
        grant = issue_mutation_grant(
            context.authorization,
            session=self._issued_session,
            binding=self._issued_binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )

        def valid(wire: Mapping[str, Any]) -> bool:
            try:
                result_type.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        try:
            return execute_mutation(
                connection,
                identity,
                grant=grant,
                context=context.authorization,
                equivalence=equivalence,
                precondition=precondition,
                mutate=mutate,
                validate_result=valid,
                clock=self.clock,
                allocate_identifier=self.allocate_identifier,
            )
        except MutationIdempotencyConflict as error:
            raise OperationError(error.code, error.message, retry_class=error.retry_class) from error
        except MutationPreconditionFailed as error:
            raise OperationError(
                ERROR_CODE_MUTATION_PRECONDITION_FAILED,
                _MESSAGE_PRECONDITION,
                retry_class=DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_MUTATION_PRECONDITION_FAILED],
            ) from error

    # --- code.policy.register ------------------------------------------------------

    def code_policy_register(self, context: OperationContext) -> AuditedOperationResult:
        """Record the caller's authorisation of one versioned acceptance policy.

        Every lock must name a producer build and profile this service declares: a policy
        locking anything else could never admit an import here, and an owner who wrote one
        should be told so now rather than by every import that cites it.
        """
        request: CodePolicyRegisterInput = _decoded(
            context, _POLICY_KEYS, CodePolicyRegisterInput, validate_code_policy_register_input
        )
        policy: CodeAnalysisImportPolicy | None
        try:
            policy = CodeAnalysisImportPolicy(
                policy_id=request.policy_id,
                policy_version=request.policy_version,
                producers=tuple(
                    (lock.producer_id, lock.producer_version, lock.profile_digest)
                    for lock in request.producers
                ),
                accepted_node_kinds=tuple(
                    (kind.producer_id, kind.node_kind) for kind in request.accepted_node_kinds
                ),
                accepted_relationships=tuple(
                    (item.producer_id, item.relationship, item.evidence_strength)
                    for item in request.accepted_relationships
                ),
                allow_reduced_coverage=request.allow_reduced_coverage,
                max_candidates=(
                    MAX_CANDIDATES if request.max_candidates is None else request.max_candidates
                ),
            )
        except CodeAnalysisImportError:
            policy = None
        declared = {producer.lock for producer in DECLARED_PRODUCERS}
        if policy is None or not set(policy.producers) <= declared:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_POLICY_INVALID)
        storage = self._storage()
        chosen = policy

        def mutate(
            fenced: sqlite3.Connection, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            existing = fenced.execute(
                "SELECT recorded_at_us FROM omnivia_code_analysis_import_policies "
                "WHERE workspace_id = ? AND policy_id = ? AND policy_version = ?",
                (context.workspace_id, chosen.policy_id, chosen.policy_version),
            ).fetchone()
            try:
                digest = register_import_policy(
                    fenced,
                    settlement,
                    workspace_id=context.workspace_id,
                    principal_id=context.principal,
                    policy=chosen,
                )
            except CodeAnalysisImportConflict as conflict:
                raise application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_POLICY_CONFLICT) from conflict
            return CodePolicyRegisterResult(
                policy=CodePolicyReference(
                    policy_id=chosen.policy_id,
                    policy_version=chosen.policy_version,
                    policy_digest=digest,
                ),
                disposition="registered" if existing is None else "already_registered",
                registered_at=_timestamp(
                    settlement.settled_at_us if existing is None else int(existing[0])
                ),
            ).to_wire()

        outcome = self._execute(context, request, storage, mutate, CodePolicyRegisterResult)
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    # --- code.import ---------------------------------------------------------------

    def code_import(self, context: OperationContext) -> AuditedOperationResult:
        """Analyse one sealed snapshot with a declared producer and activate what one
        registered policy accepts; or answer a replay from its stored receipt."""
        request: CodeImportInput = _decoded(
            context, _IMPORT_KEYS, CodeImportInput, validate_code_import_input
        )
        declared = declared_producer(request.producer_id, request.profile_digest)
        if declared is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_NOT_DECLARED)
        if declared.analyse is None:
            raise OperationError(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE,
                _MESSAGE_RUNTIME,
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
                details={"reason": declared.unavailable_reason},
            )
        storage = self._storage()
        connection, identity, _guard = storage
        blobs_root = self._blobs_root()
        workspace_id = context.workspace_id
        key = import_idempotency_key(context.principal, str(context.authorization.idempotency_key))
        policy: CodeAnalysisImportPolicy | None = None
        with authorised(connection, mutations=False, ddl=False) as reader:
            # A settled key is answered from the executor's claim alone -- the stored
            # receipt, or `idempotency_conflict` for a different request -- so nothing
            # below runs for it: not the policy lookup, whose answer may have moved since,
            # and not the producer, whose answer is already fixed.
            settled = import_receipt(reader, workspace_id=workspace_id, idempotency_key=key)
            if settled is None:
                policy = read_import_policy(
                    reader,
                    workspace_id=workspace_id,
                    policy_id=request.policy_id,
                    policy_version=request.policy_version,
                )
                if policy is None:
                    raise application_refusal(ERROR_CODE_NOT_FOUND, _MESSAGE_POLICY_NOT_FOUND)
                if declared.lock not in policy.producers:
                    raise application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_POLICY_DOES_NOT_ADMIT)
        verified = None if settled is not None else self._analysed(
            context, connection, declared, request, blobs_root
        )
        scope = (request.repository_id, declared.producer_id, declared.profile_digest)
        grant = _grant(context)
        chosen = policy

        def precondition(fenced: sqlite3.Connection) -> str:
            return str(
                current_activation_sequence(
                    fenced,
                    workspace_id=workspace_id,
                    repository_id=scope[0],
                    producer_id=scope[1],
                    profile_digest=scope[2],
                )
            )

        def mutate(
            fenced: sqlite3.Connection, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            if verified is None or chosen is None:
                # A settled W12 receipt with no executor claim: the two are written in one
                # transaction, so this is a store that no longer agrees with itself.
                raise MutationSeamFault(_MESSAGE_NO_STORAGE)
            try:
                receipt = import_code_analysis(
                    fenced,
                    workspace_id=workspace_id,
                    verified=verified,
                    policy=chosen,
                    blobs_root=blobs_root,
                    idempotency_key=key,
                    expected_active_import_id=active_import_id(
                        fenced,
                        workspace_id=workspace_id,
                        repository_id=scope[0],
                        producer_id=scope[1],
                        profile_digest=scope[2],
                    ),
                    now_us=settlement.settled_at_us,
                )
            except CodeAnalysisImportError as refused:
                raise application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_IMPORT_REFUSED) from refused
            return CodeImportResult(
                binding=CodeAnalysisBinding(
                    repository_id=receipt.repository_id,
                    snapshot_id=receipt.snapshot_id,
                    producer_id=receipt.producer_id,
                    producer_version=verified.batch.producer_version,
                    profile_digest=receipt.profile_digest,
                    import_id=receipt.import_id,
                    batch_digest=receipt.batch_digest,
                    policy_id=receipt.policy_id,
                    policy_version=receipt.policy_version,
                    policy_digest=receipt.policy_digest,
                    manifest_evidence_id=verified.manifest_evidence_id,
                    activated_at=_timestamp(receipt.activated_at_us),
                ),
                activation_sequence=receipt.activation_sequence,
                previous_import_id=receipt.previous_import_id,
                coverage_decision=receipt.coverage_decision,
                reduced_file_count=receipt.reduced_file_count,
                entity_count=receipt.entity_count,
                relation_count=receipt.relation_count,
                diagnostic_count=receipt.diagnostic_count,
                file_count=receipt.file_count,
                freshness=_freshness(
                    fenced,
                    workspace_id=workspace_id,
                    snapshot_id=receipt.snapshot_id,
                    grant=grant,
                    evidence_labels=read_evidence_labels(fenced, workspace_id=workspace_id),
                ),
            ).to_wire()

        outcome = self._execute(
            context, request, storage, mutate, CodeImportResult, precondition=precondition
        )
        self._require_findable(
            connection,
            identity,
            workspace_id=workspace_id,
            blobs_root=blobs_root,
            import_id=str(outcome.result["binding"]["import_id"]),
        )
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    def _analysed(
        self,
        context: OperationContext,
        connection: Any,
        declared: DeclaredProducer,
        request: CodeImportInput,
        blobs_root: Path,
    ) -> VerifiedCodeAnalysisBatch:
        """The declared producer's proven batch of one sealed, complete capture.

        Read-only and outside every write transaction. The heartbeat runs once per file:
        it keeps this instance's workspace lease current through a long analysis, and it
        abandons the analysis as soon as the request's own deadline has passed.
        """
        assert declared.analyse is not None
        deadline_ms = getattr(context.authorization, "deadline_ms", None)
        started = time.monotonic()
        renew = getattr(self.service, "renew_lease_if_due", None)

        def heartbeat() -> None:
            if deadline_ms is not None and (time.monotonic() - started) * 1000 >= deadline_ms:
                raise _Abandoned
            if callable(renew):
                renew()

        refusal: OperationError | None = None
        verified = None
        with authorised(connection, mutations=False, ddl=False) as reader:
            snapshot = covered_snapshot(
                reader,
                workspace_id=context.workspace_id,
                snapshot_id=request.snapshot_id,
                repository_id=request.repository_id,
            )
            if snapshot is None:
                refusal = application_refusal(ERROR_CODE_NOT_FOUND, _MESSAGE_SNAPSHOT_NOT_FOUND)
            elif snapshot.representation != "captured_v1" or snapshot.capture_status != "complete":
                refusal = application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_SNAPSHOT_NOT_ANALYSABLE)
            else:
                try:
                    verified = declared.analyse(
                        reader,
                        workspace_id=context.workspace_id,
                        repository_id=request.repository_id,
                        snapshot_id=request.snapshot_id,
                        blobs_root=blobs_root,
                        heartbeat=heartbeat,
                    )
                except _Abandoned:
                    refusal = OperationError(
                        ERROR_CODE_DEADLINE_EXCEEDED,
                        _MESSAGE_DEADLINE,
                        retry_class=DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_DEADLINE_EXCEEDED],
                    )
                except code_analysis_python.PythonAnalysisRefused:
                    refusal = application_refusal(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_TOO_LARGE)
                except CodeAnalysisBindingError:
                    refusal = application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_SNAPSHOT_UNVERIFIABLE)
                if refusal is None and verified is None:
                    refusal = OperationError(
                        ERROR_CODE_CONFLICT,
                        _MESSAGE_NO_SOURCE,
                        retry_class=DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_CONFLICT],
                        details={"reason": NO_ADMITTED_SOURCE},
                    )
        if refusal is not None:
            raise refusal
        assert verified is not None
        return verified

    def _require_findable(
        self,
        connection: Any,
        identity: Any,
        *,
        workspace_id: str,
        blobs_root: Path,
        import_id: str,
    ) -> None:
        """The post-commit barrier: every file artifact the import cites is searchable.

        `evidence.capture`'s own barrier and the same two calls. It runs on a replay too,
        which is what makes a barrier that failed once recoverable by replaying the key.
        """
        failed = False
        try:
            guard = read_guard(connection)
            if guard is None:
                failed = True
            else:
                build_search_projection(
                    connection,
                    identity,
                    workspace_id=workspace_id,
                    fencing_generation=guard.fencing_generation,
                    now_us=int(self.clock.wall_time().timestamp() * 1_000_000),
                )
                projection = open_search_projection(
                    connection, workspace_id=workspace_id, blobs_root=blobs_root
                )
                with authorised(connection, mutations=False, ddl=False) as reader:
                    cited = import_file_evidence_ids(
                        reader, workspace_id=workspace_id, import_id=import_id
                    )
                failed = not all(evidence_id in projection.material for evidence_id in cited)
        except (StorageError, OSError, sqlite3.Error):
            failed = True
        if failed:
            raise OperationError(
                ERROR_CODE_INTERNAL_RECOVERABLE,
                _MESSAGE_NOT_SEARCHABLE,
                retry_class=RETRY_CLASS_RETRYABLE,
            )


# --- code.status -----------------------------------------------------------------


def code_status(context: OperationContext) -> Mapping[str, Any]:
    """Every declared producer profile's analysis of one repository, for this caller."""
    request: CodeStatusInput = _decoded(
        context, _STATUS_KEYS, CodeStatusInput, validate_code_status_input
    )
    connection = getattr(getattr(context, "service", None), "connection", None)
    if connection is None:
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
    workspace_id = context.workspace_id
    instant_us = time.time_ns() // 1_000_000 * 1000
    grant = _grant(context)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        evidence_labels = read_evidence_labels(connection, workspace_id=workspace_id)
        snapshot_files: dict[str, str] | None = None
        snapshot_block = None
        if request.snapshot_id is not None:
            snapshot_files, snapshot_block = _snapshot_status(
                connection, workspace_id, request, grant, evidence_labels
            )
        with authorised(connection, mutations=False, ddl=False) as reader:
            policies = read_import_policies(reader, workspace_id=workspace_id)
        analyses = tuple(
            _analysis_status(
                connection,
                declared,
                workspace_id=workspace_id,
                request=request,
                snapshot_files=snapshot_files,
                policies=policies,
                grant=grant,
                evidence_labels=evidence_labels,
                instant_us=instant_us,
            )
            for declared in DECLARED_PRODUCERS
        )
    except BaseException:
        if owns_transaction:
            connection.execute("ROLLBACK")
        raise
    if owns_transaction:
        connection.execute("COMMIT")
    return CodeStatusResult(
        repository_id=request.repository_id,
        snapshot=snapshot_block,
        analyses=analyses,
        as_of=_timestamp(instant_us),
    ).to_wire()


def _snapshot_status(
    connection: Any,
    workspace_id: str,
    request: CodeStatusInput,
    grant: EvidenceLabelGrant,
    evidence_labels: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, str], CodeSnapshotStatus]:
    """The requested snapshot, or `not_found` -- also for one whose manifest is hidden."""
    assert request.snapshot_id is not None
    with authorised(connection, mutations=False, ddl=False) as reader:
        snapshot = covered_snapshot(
            reader,
            workspace_id=workspace_id,
            snapshot_id=request.snapshot_id,
            repository_id=request.repository_id,
        )
        manifest = reader.execute(
            "SELECT manifest_evidence_id FROM omnivia_engineering_snapshot_captures "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (workspace_id, request.snapshot_id),
        ).fetchone()
    if snapshot is None or (
        manifest is not None and not grant.permits(evidence_labels.get(str(manifest[0]), ()))
    ):
        raise application_refusal(ERROR_CODE_NOT_FOUND, _MESSAGE_SNAPSHOT_NOT_FOUND)
    files = (
        read_snapshot_files(connection, workspace_id=workspace_id, snapshot_id=request.snapshot_id)
        if snapshot.representation == "captured_v1"
        else dict(snapshot.manifest)
    )
    read = {suffix for declared in DECLARED_PRODUCERS for suffix in declared.suffixes}
    groups: dict[str, int] = {}
    for path in files:
        suffix = PurePosixPath(path).suffix
        if suffix not in read:
            groups[suffix] = groups.get(suffix, 0) + 1
    ordered = sorted(groups.items())
    return files, CodeSnapshotStatus(
        snapshot_id=request.snapshot_id,
        capture_status=snapshot.capture_status,
        freshness=_freshness(
            connection,
            workspace_id=workspace_id,
            snapshot_id=request.snapshot_id,
            grant=grant,
            evidence_labels=evidence_labels,
        ),
        unsupported=tuple(
            CodeUnsupportedFiles(suffix=suffix, file_count=count)
            for suffix, count in ordered[:MAX_UNSUPPORTED_GROUPS]
        ),
        unsupported_truncated=len(ordered) > MAX_UNSUPPORTED_GROUPS,
    )


def _analysis_status(
    connection: Any,
    declared: DeclaredProducer,
    *,
    workspace_id: str,
    request: CodeStatusInput,
    snapshot_files: Mapping[str, str] | None,
    policies: tuple[tuple[CodeAnalysisImportPolicy, int], ...],
    grant: EvidenceLabelGrant,
    evidence_labels: Mapping[str, tuple[str, ...]],
    instant_us: int,
) -> CodeAnalysisStatus:
    """One declared producer profile's availability, readiness, binding and coverage.

    An analysis whose snapshot manifest the caller may not see is reported exactly as one
    that does not exist -- no binding, sequence 0 -- and coverage counts only files the
    caller may see, so a hidden file moves neither count nor `partial`.
    """
    active = read_active_scope(
        connection,
        workspace_id=workspace_id,
        repository_id=request.repository_id,
        producer_id=declared.producer_id,
        profile_digest=declared.profile_digest,
        instant_us=instant_us,
    )
    if active is not None and not grant.permits(
        evidence_labels.get(active[0].manifest_evidence_id, ())
    ):
        active = None
    policy = next(
        (
            CodePolicyReference(
                policy_id=item.policy_id,
                policy_version=item.policy_version,
                policy_digest=item.digest,
            )
            for item, _recorded in policies
            if declared.lock in item.producers
        ),
        None,
    )
    reason: str | None
    if snapshot_files is not None and not any(declared.admits(path) for path in snapshot_files):
        reason = NO_ADMITTED_SOURCE
    elif active is None:
        reason = NO_ACTIVE_IMPORT
    elif request.snapshot_id is not None and active[0].snapshot_id != request.snapshot_id:
        reason = SNAPSHOT_NOT_ACTIVE
    else:
        reason = None
    status = CodeAnalysisStatus(
        producer_id=declared.producer_id,
        producer_version=declared.producer_version,
        profile_digest=declared.profile_digest,
        languages=declared.languages,
        availability=AVAILABLE if declared.analyse is not None else UNAVAILABLE,
        availability_reason=declared.unavailable_reason if declared.analyse is None else None,
        readiness=READY if reason is None else NOT_READY,
        readiness_reason=reason,
        activation_sequence=0 if active is None else active[1],
        policy=policy,
    )
    if active is None:
        return status
    scope = active[0]
    analysed = read_snapshot_files(connection, workspace_id=workspace_id, snapshot_id=scope.snapshot_id)
    covered = read_import_files(connection, workspace_id=workspace_id, import_id=scope.import_id)
    # A file no fact was read from has no artifact of its own, so the manifest -- already
    # visible here -- is all that governs it; a covered file is governed by its own too.
    visible = [
        path
        for path in sorted(analysed)
        if declared.admits(path)
        and (
            path not in covered
            or file_admitted(
                grant,
                evidence_labels,
                repository_id=scope.repository_id,
                manifest_evidence_id=scope.manifest_evidence_id,
                relative_path=path,
                content_digest=covered[path],
            )
        )
    ]
    analysed_count = sum(1 for path in visible if path in covered)
    return dataclasses.replace(
        status,
        binding=CodeAnalysisBinding(
            repository_id=scope.repository_id,
            snapshot_id=scope.snapshot_id,
            producer_id=scope.producer_id,
            producer_version=scope.producer_version,
            profile_digest=scope.profile_digest,
            import_id=scope.import_id,
            batch_digest=scope.batch_digest,
            policy_id=scope.policy_id,
            policy_version=scope.policy_version,
            policy_digest=scope.policy_digest,
            manifest_evidence_id=scope.manifest_evidence_id,
            activated_at=_timestamp(scope.activated_at_us),
        ),
        freshness=_freshness(
            connection,
            workspace_id=workspace_id,
            snapshot_id=scope.snapshot_id,
            grant=grant,
            evidence_labels=evidence_labels,
        ),
        coverage=CodeAnalysisCoverage(
            state="complete" if analysed_count == len(visible) else "partial",
            admitted_file_count=len(visible),
            analysed_file_count=analysed_count,
        ),
    )


__all__ = [
    "CODE_STATUS_OPERATION",
    "DECLARED_PRODUCERS",
    "CodeAnalysisHandlers",
    "DeclaredProducer",
    "code_status",
    "declared_producer",
    "import_idempotency_key",
]
