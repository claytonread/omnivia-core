"""The model-neutral semantic vector projection: its builder, and the narrowed read.

Migration 0069 declares the durable rows; migration 0011 owns the lifecycle they ride on.
This module builds a run under that lifecycle (`running -> validating -> succeeded`, then
one activation INSERT whose AFTER trigger moves the ledger pointer) exactly as
`projections/fts.py` does for the lexical lane, and it shares nothing else with it. It does
not import `fts`, and `repository.CONTRIBUTING_PROJECTIONS` still names only the lexical
projection, so a missing, stale or incompatible semantic generation cannot make
`evidence.search` refuse: the two lanes fail independently.

**No model ships here.** `SemanticEmbedder` is a Protocol. It names the three identities a
build must declare (profile, execution, representation) and one method that turns canonical
input bytes into float vectors. Nothing in this repository implements it.

**The provider runs outside every fenced transaction.** `_append_occurrences` reads a batch,
calls `embedder.embed` with no transaction open, validates and encodes the result with the
strict codec, and only then opens one fenced transaction to write vectors, occurrences and
the checkpoint together. A slow or failing provider therefore holds no lease-bearing
transaction and can leave nothing half-written.

**Vectors are deduplicated by exact canonical input; occurrences stay separate.** Two
artifacts with byte-identical model input share one `omnivia_semantic_vectors` row, and each
keeps its own `omnivia_semantic_vector_occurrences` row naming it. Coverage is therefore
checkable: validation refuses to close a run in which any in-scope evidence artifact has no
occurrence, or any vector has none.

**Reads narrow by address.** `project_semantic` looks up only the ids the supplied frozen
`AuthorizedFrontier` holds, never iterates the run, and returns the pure
`ProjectedSemanticFrontier` value. A frontier member with no vector is a refusal, never a
fallback to some other source, and freshness is re-proven on every call.

ponytail: the embedded text is the artifact's identity surface only (kind, native id,
locator), exactly what the lexical lane indexes before blob composition. Composing stored
blob text is a follow-up; it changes the canonical-input version, not this lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError, authorised
from omnivia_core_runtime.storage.repository import (
    authoritative_checkpoint,
    projection_readiness,
)
from omnivia_core_runtime.storage.retrieval import AuthorizedFrontier
from omnivia_core_runtime.storage.semantic_vectors import (
    CompatibilityDecision,
    ExecutionProvenance,
    GenerationIdentity,
    ProjectedSemanticCandidate,
    ProjectedSemanticFrontier,
    RepresentationIdentity,
    SemanticEmbeddingProfile,
    SemanticVectorError,
    canonical_model_input,
    encode_vector,
    identity_digest,
    input_digest,
)
from omnivia_core_runtime.storage.sql_in import execute_in_rows

PROJECTION_ID: Final = "evidence.semantic.vectors"
PROJECTION_KIND: Final = "evidence.semantic"
SCHEMA_VERSION: Final = "0069"

#: Artifacts embedded per transaction. Every batch ends in a checkpoint, so this bounds the
#: work an interruption can cost. ponytail: fixed batch, parametrize if it ever matters.
CHECKPOINT_BATCH: Final = 256

VECTORS_TABLE: Final = "omnivia_semantic_vectors"
OCCURRENCES_TABLE: Final = "omnivia_semantic_vector_occurrences"

_LEDGER_DECLARED_AT: Final = "0069-semantic-vector-projection"
_SUPERSEDED_SOURCE: Final = json.dumps(
    {"code": "projection.source_checkpoint_advanced"}, separators=(",", ":"), sort_keys=True
)


class SemanticProjectionError(StorageError):
    """The semantic projection cannot serve this read or finish this build."""


class SemanticProjectionUnavailable(SemanticProjectionError):
    """No semantic generation is activated, or it has no vector for an authorized id."""


class StaleSemanticProjection(SemanticProjectionError):
    """The activated generation is behind the authoritative checkpoint. Refuse, never build."""


class IncompatibleSemanticGeneration(SemanticProjectionError):
    """The generation, or the embedder asked to query it, is not comparable with it."""


class IncompleteSemanticCoverage(SemanticProjectionError):
    """A run's occurrences do not cover its in-scope evidence exactly."""


class SemanticEmbedder(Protocol):
    """What a build and a query need from a model. Ships without an implementation.

    The three identities are declared by the embedder, not inferred from its output.
    `embed` receives canonical model-input bytes (`canonical_model_input`) and returns one
    vector per input, in order, as float components. The adapter encodes each through the
    strict codec, so an embedder that does not honour its profile's normalization is
    refused, never repaired.
    """

    @property
    def profile(self) -> SemanticEmbeddingProfile: ...

    @property
    def execution(self) -> ExecutionProvenance: ...

    @property
    def representation(self) -> RepresentationIdentity: ...

    def embed(self, canonical_inputs: Sequence[bytes]) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True, slots=True)
class SemanticGeneration:
    """The activated run, with every identity reconstructed from its durable rows."""

    run_id: str
    epoch: int
    source_checkpoint: str
    build_digest: str
    profile: SemanticEmbeddingProfile
    execution: ExecutionProvenance
    representation: RepresentationIdentity
    identity: GenerationIdentity


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    """What one `build_semantic_projection` call left behind.

    `activated` is `False` when the projection was already level and the call did nothing.
    """

    run_id: str
    epoch: int
    source_checkpoint: str
    occurrence_count: int
    vector_count: int
    build_digest: str
    activated: bool


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def active_generation(
    connection: sqlite3.Connection, *, workspace_id: str
) -> SemanticGeneration | None:
    """The activated generation, rebuilt and re-digested from its rows, or `None`.

    Every stored digest is recomputed from the stored facts and compared. A row edited
    underneath the generation (the guards forbid it; this proves it) is a refusal.
    """
    with authorised(connection, mutations=False, ddl=False) as fenced:
        row = fenced.execute(
            "SELECT a.run_id, a.target_epoch, a.source_checkpoint, a.build_digest, "
            "       r.profile_version, "
            "       p.dimensions, p.weights_id, p.tokenizer_id, p.transform_id, "
            "       p.pooling_id, p.normalization_id, p.canonical_input_version, "
            "       e.provider_id, e.runtime_id, e.build_id, e.hardware_id, "
            "       e.inference_precision, e.compatibility_status, e.compatibility_reason, "
            "       e.scalar_encoding, e.byte_order, e.metric, e.codec_version, "
            "       e.index_version, e.execution_digest, e.representation_digest, "
            "       e.generation_digest "
            "FROM omnivia_projection_activations a "
            "JOIN omnivia_projection_runs r "
            "  ON r.workspace_id = a.workspace_id AND r.projection_id = a.projection_id "
            " AND r.run_id = a.run_id "
            "LEFT JOIN omnivia_semantic_run_executions e "
            "  ON e.workspace_id = a.workspace_id AND e.projection_id = a.projection_id "
            " AND e.run_id = a.run_id "
            "LEFT JOIN omnivia_semantic_profiles p "
            "  ON p.workspace_id = e.workspace_id AND p.profile_digest = e.profile_digest "
            "WHERE a.workspace_id = ? AND a.projection_id = ? "
            "ORDER BY a.activation_sequence DESC LIMIT 1",
            (workspace_id, PROJECTION_ID),
        ).fetchone()
    if row is None:
        return None
    if row[5] is None or row[12] is None:
        raise SemanticProjectionError(
            "the activated semantic run has no recorded profile or execution"
        )
    try:
        profile = SemanticEmbeddingProfile(
            dimensions=int(row[5]),
            weights_id=str(row[6]),
            tokenizer_id=str(row[7]),
            transform_id=str(row[8]),
            pooling_id=str(row[9]),
            normalization_id=str(row[10]),
            canonical_input_version=str(row[11]),
        )
        execution = ExecutionProvenance(
            provider_id=str(row[12]),
            runtime_id=str(row[13]),
            build_id=str(row[14]),
            hardware_id=str(row[15]),
            inference_precision=str(row[16]),
            compatibility=CompatibilityDecision(str(row[17]), str(row[18])),
        )
        representation = RepresentationIdentity(
            scalar_encoding=str(row[19]),
            byte_order=str(row[20]),
            metric=str(row[21]),
            codec_version=str(row[23 - 1]),
            index_version=str(row[23]),
        )
        identity = GenerationIdentity(
            generation_id=str(row[0]),
            profile_digest=profile.digest,
            representation_digest=representation.digest,
            execution_digest=execution.digest,
            compatibility_status=execution.compatibility.status,
        )
    except SemanticVectorError as exc:
        raise SemanticProjectionError("the stored semantic identity is malformed") from exc
    if (
        profile.digest != str(row[4])
        or execution.digest != str(row[24])
        or representation.digest != str(row[25])
        or identity.digest != str(row[26])
    ):
        raise SemanticProjectionError(
            "the stored semantic identity does not match its recorded digests"
        )
    return SemanticGeneration(
        run_id=str(row[0]),
        epoch=int(row[1]),
        source_checkpoint=str(row[2]),
        build_digest=str(row[3]),
        profile=profile,
        execution=execution,
        representation=representation,
        identity=identity,
    )


def current_generation(
    connection: sqlite3.Connection, *, workspace_id: str
) -> SemanticGeneration:
    """The activated generation, proven level, compatible and intact, or a refusal.

    Gated on `projection_readiness` for this projection alone, so the lexical lane's own
    readiness is neither consulted nor affected. Equality, no tolerance.
    """
    checkpoint = authoritative_checkpoint(connection, workspace_id=workspace_id)
    readiness = projection_readiness(
        connection,
        workspace_id=workspace_id,
        source_checkpoint=checkpoint,
        contributing=(PROJECTION_ID,),
    )
    if readiness.missing:
        raise SemanticProjectionUnavailable(
            f"{PROJECTION_ID} has no activated run; semantic search cannot be served"
        )
    generation = active_generation(connection, workspace_id=workspace_id)
    if readiness.stale or generation is None or generation.source_checkpoint != checkpoint:
        raise StaleSemanticProjection(
            f"{PROJECTION_ID} is behind the workspace checkpoint; there is no tolerance for lag"
        )
    if not generation.execution.compatibility.compatible:
        raise IncompatibleSemanticGeneration(
            f"{PROJECTION_ID} generation was recorded as incompatible: "
            f"{generation.execution.compatibility.reason}"
        )
    return generation


def require_current(
    connection: sqlite3.Connection, *, workspace_id: str, generation: SemanticGeneration
) -> None:
    """Re-prove per request that `generation` is still the activated, level one."""
    current = current_generation(connection, workspace_id=workspace_id)
    if current.run_id != generation.run_id or current.identity != generation.identity:
        raise StaleSemanticProjection(
            f"this generation was built from run {generation.run_id!r} and "
            f"{PROJECTION_ID} has since activated {current.run_id!r}"
        )


def project_semantic(
    connection: sqlite3.Connection,
    generation: SemanticGeneration,
    frontier: AuthorizedFrontier,
) -> ProjectedSemanticFrontier:
    """Narrow the activated generation to exactly the frozen frontier's ids.

    Reads by address (`evidence_id IN (...)`), never by scan, so an excluded artifact's
    vector is never touched. Freshness is proven first. A member with no occurrence is a
    refusal -- the frontier and the generation disagree about what exists.
    """
    require_current(connection, workspace_id=frontier.workspace_id, generation=generation)
    ids = tuple(candidate.evidence_id for candidate in frontier.candidates)
    with authorised(connection, mutations=False, ddl=False) as fenced:
        rows = execute_in_rows(
            fenced,
            select=(
                f"SELECT o.evidence_id, v.vector FROM {OCCURRENCES_TABLE} o "
                f"JOIN {VECTORS_TABLE} v "
                "  ON v.workspace_id = o.workspace_id AND v.projection_id = o.projection_id "
                " AND v.run_id = o.run_id AND v.input_digest = o.input_digest"
            ),
            where_before="o.workspace_id = ? AND o.projection_id = ? AND o.run_id = ?",
            in_column="o.evidence_id",
            leading=(frontier.workspace_id, PROJECTION_ID, generation.run_id),
            ids=ids,
            order_key=lambda row: row[0],
        )
    vectors = {str(evidence_id): bytes(vector) for evidence_id, vector in rows}
    members: list[ProjectedSemanticCandidate] = []
    for candidate in frontier.candidates:
        vector = vectors.get(candidate.evidence_id)
        if vector is None:
            raise SemanticProjectionUnavailable(
                "the activated semantic generation has no vector for an authorized candidate"
            )
        members.append(ProjectedSemanticCandidate(candidate=candidate, vector=vector))
    return ProjectedSemanticFrontier(
        workspace_id=frontier.workspace_id,
        checksum=frontier.checksum,
        dimensions=generation.profile.dimensions,
        profile_digest=generation.identity.profile_digest,
        generation_digest=generation.identity.digest,
        candidates=tuple(members),
    )


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def build_semantic_projection(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    embedder: SemanticEmbedder,
    now_us: int,
) -> BuildOutcome:
    """Bring the semantic projection level with the workspace, from wherever it is.

    Maintenance work and the only way this projection advances. Each phase reads its own
    position from the database and runs in its own fenced transaction, so a crash between
    or inside phases leaves a state the next call resumes -- the same contract as the
    lexical builder. The ledger pointer moves only as the activation INSERT's AFTER
    trigger's doing.
    """
    profile, execution, representation = (
        embedder.profile,
        embedder.execution,
        embedder.representation,
    )
    fence = (workspace_id, fencing_generation)
    _ensure_ledger_row(connection, identity, fence=fence)

    while True:
        checkpoint = authoritative_checkpoint(connection, workspace_id=workspace_id)
        plan = _plan_run(
            connection,
            identity,
            fence=fence,
            checkpoint=checkpoint,
            now_us=now_us,
            profile=profile,
            execution=execution,
            representation=representation,
        )
        if plan is None:
            current = active_generation(connection, workspace_id=workspace_id)
            if current is None:
                raise SemanticProjectionError("a level semantic ledger has no activation")
            _reclaim(connection, identity, fence=fence)
            occurrences, vectors = _counts(connection, workspace_id, current.run_id)
            return BuildOutcome(
                run_id=current.run_id,
                epoch=current.epoch,
                source_checkpoint=current.source_checkpoint,
                occurrence_count=occurrences,
                vector_count=vectors,
                build_digest=current.build_digest,
                activated=False,
            )

        run_id, epoch, state, run_checkpoint, compatible = plan
        if state == "running":
            _ensure_run_identity(
                connection,
                identity,
                fence=fence,
                run_id=run_id,
                profile=profile,
                execution=execution,
                representation=representation,
                now_us=now_us,
            )
            _append_occurrences(
                connection,
                identity,
                fence=fence,
                run_id=run_id,
                checkpoint=run_checkpoint,
                embedder=embedder,
            )
            digest = _validate_run(
                connection,
                identity,
                fence=fence,
                run_id=run_id,
                checkpoint=run_checkpoint,
                now_us=now_us,
            )
        else:
            digest = _completed_digest(connection, workspace_id, run_id)
        _activate_run(
            connection,
            identity,
            fence=fence,
            run_id=run_id,
            epoch=epoch,
            checkpoint=run_checkpoint,
            now_us=now_us,
        )
        _reclaim(connection, identity, fence=fence)
        if run_checkpoint == checkpoint and compatible:
            occurrences, vectors = _counts(connection, workspace_id, run_id)
            return BuildOutcome(
                run_id=run_id,
                epoch=epoch,
                source_checkpoint=run_checkpoint,
                occurrence_count=occurrences,
                vector_count=vectors,
                build_digest=digest,
                activated=True,
            )
        # A recovered run was complete but for an older checkpoint or another embedder:
        # activating it was right and is not the whole job. Loop at the current one.


def _ensure_ledger_row(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
) -> None:
    workspace_id, generation = fence
    with authorised(connection, mutations=False, ddl=False) as fenced:
        declared = fenced.execute(
            "SELECT 1 FROM omnivia_projection_ledger WHERE projection_id = ?",
            (PROJECTION_ID,),
        ).fetchone()
    if declared is not None:
        return
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        connection.execute(
            "INSERT INTO omnivia_projection_ledger "
            "(projection_id, version, rebuildable, state, updated_at, "
            "fencing_generation) VALUES (?, ?, 1, 'ready', ?, ?)",
            (PROJECTION_ID, SCHEMA_VERSION, _LEDGER_DECLARED_AT, generation),
        )


def _plan_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
    checkpoint: str,
    now_us: int,
    profile: SemanticEmbeddingProfile,
    execution: ExecutionProvenance,
    representation: RepresentationIdentity,
) -> tuple[str, int, str, str, bool] | None:
    """The run this build should advance, or `None` when the ledger is already level.

    "Compatible" means the run was declared under this embedder's profile and, once its
    execution row exists, this embedder's execution and representation. A run built by a
    different model or runtime is never resumed and never counts as level: the builder
    declares a fresh run instead, so an index never silently serves another model's vectors.
    """
    workspace_id, generation = fence
    with authorised(connection, mutations=False, ddl=False) as fenced:
        in_flight = fenced.execute(
            "SELECT r.run_id, r.target_epoch, r.source_checkpoint, r.state, "
            "       r.schema_version, r.profile_version, "
            "       e.execution_digest, e.representation_digest "
            "FROM omnivia_projection_runs r "
            "LEFT JOIN omnivia_semantic_run_executions e "
            "  ON e.workspace_id = r.workspace_id AND e.projection_id = r.projection_id "
            " AND e.run_id = r.run_id "
            "WHERE r.workspace_id = ? AND r.projection_id = ? "
            "  AND r.state IN ('running', 'succeeded') "
            "  AND NOT EXISTS (SELECT 1 FROM omnivia_projection_activations a "
            "                  WHERE a.workspace_id = r.workspace_id "
            "                    AND a.projection_id = r.projection_id "
            "                    AND a.run_id = r.run_id) "
            "ORDER BY r.target_epoch DESC LIMIT 1",
            (workspace_id, PROJECTION_ID),
        ).fetchone()
        ledger = fenced.execute(
            "SELECT l.active_epoch, l.active_source_checkpoint, "
            "       r.schema_version, r.profile_version, "
            "       e.execution_digest, e.representation_digest "
            "FROM omnivia_projection_ledger l "
            "LEFT JOIN omnivia_projection_runs r "
            "  ON r.workspace_id = ? AND r.projection_id = l.projection_id "
            " AND r.run_id = l.active_run_id "
            "LEFT JOIN omnivia_semantic_run_executions e "
            "  ON e.workspace_id = r.workspace_id AND e.projection_id = r.projection_id "
            " AND e.run_id = r.run_id "
            "WHERE l.projection_id = ?",
            (workspace_id, PROJECTION_ID),
        ).fetchone()
        declared_epoch_row = fenced.execute(
            "SELECT MAX(target_epoch) FROM omnivia_projection_runs "
            "WHERE workspace_id = ? AND projection_id = ?",
            (workspace_id, PROJECTION_ID),
        ).fetchone()

    def matches(schema: object, profile_version: object, run_execution: object, run_repr: object) -> bool:
        return (
            str(schema) == SCHEMA_VERSION
            and str(profile_version) == profile.digest
            and (run_execution is None or str(run_execution) == execution.digest)
            and (run_repr is None or str(run_repr) == representation.digest)
        )

    in_flight_compatible = in_flight is not None and matches(
        in_flight[4], in_flight[5], in_flight[6], in_flight[7]
    )
    if in_flight is not None:
        state = str(in_flight[3])
        if state == "succeeded" or (in_flight_compatible and str(in_flight[2]) == checkpoint):
            return (
                str(in_flight[0]),
                int(in_flight[1]),
                state,
                str(in_flight[2]),
                in_flight_compatible,
            )
        with fenced_transaction(
            connection, identity, workspace_id=workspace_id, fencing_generation=generation
        ):
            connection.execute(
                "UPDATE omnivia_projection_runs SET state = 'failed', "
                "finished_at_us = ?, error_json = ? "
                "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
                (
                    _next_us(connection, workspace_id, now_us),
                    _SUPERSEDED_SOURCE,
                    workspace_id,
                    PROJECTION_ID,
                    str(in_flight[0]),
                ),
            )

    active_epoch = int(ledger[0]) if ledger is not None and ledger[0] is not None else 0
    active_checkpoint = str(ledger[1]) if ledger is not None and ledger[1] is not None else None
    active_compatible = (
        ledger is not None
        and ledger[2] is not None
        and matches(ledger[2], ledger[3], ledger[4], ledger[5])
    )
    if in_flight is None and active_checkpoint == checkpoint and active_compatible:
        return None

    declared_epoch = (
        int(declared_epoch_row[0])
        if declared_epoch_row is not None and declared_epoch_row[0] is not None
        else 0
    )
    epoch = max(active_epoch, declared_epoch) + 1
    run_id = f"{PROJECTION_ID}-{epoch}-{checkpoint}"
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        connection.execute(
            "INSERT INTO omnivia_projection_runs "
            "(workspace_id, projection_id, run_id, projection_kind, schema_version, "
            "profile_version, source_checkpoint, target_epoch, started_at_us, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')",
            (
                workspace_id,
                PROJECTION_ID,
                run_id,
                PROJECTION_KIND,
                SCHEMA_VERSION,
                profile.digest,
                checkpoint,
                epoch,
                _next_us(connection, workspace_id, now_us),
            ),
        )
    return run_id, epoch, "running", checkpoint, True


def _ensure_run_identity(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
    run_id: str,
    profile: SemanticEmbeddingProfile,
    execution: ExecutionProvenance,
    representation: RepresentationIdentity,
    now_us: int,
) -> None:
    """Record the profile once and the run's execution once; verify them on resume."""
    del now_us
    workspace_id, generation = fence
    generation_identity = GenerationIdentity(
        generation_id=run_id,
        profile_digest=profile.digest,
        representation_digest=representation.digest,
        execution_digest=execution.digest,
        compatibility_status=execution.compatibility.status,
    )
    with authorised(connection, mutations=False, ddl=False) as fenced:
        profile_known = fenced.execute(
            "SELECT 1 FROM omnivia_semantic_profiles WHERE workspace_id = ? AND profile_digest = ?",
            (workspace_id, profile.digest),
        ).fetchone()
        recorded = fenced.execute(
            "SELECT generation_digest FROM omnivia_semantic_run_executions "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()
    if recorded is not None:
        if str(recorded[0]) != generation_identity.digest:
            raise SemanticProjectionError("a resumed run was declared under another generation")
        return
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        if profile_known is None:
            connection.execute(
                "INSERT INTO omnivia_semantic_profiles (workspace_id, profile_digest, "
                "dimensions, weights_id, tokenizer_id, transform_id, pooling_id, "
                "normalization_id, canonical_input_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    profile.digest,
                    profile.dimensions,
                    profile.weights_id,
                    profile.tokenizer_id,
                    profile.transform_id,
                    profile.pooling_id,
                    profile.normalization_id,
                    profile.canonical_input_version,
                ),
            )
        connection.execute(
            "INSERT INTO omnivia_semantic_run_executions (workspace_id, projection_id, "
            "run_id, profile_digest, provider_id, runtime_id, build_id, hardware_id, "
            "inference_precision, compatibility_status, compatibility_reason, "
            "scalar_encoding, byte_order, metric, codec_version, index_version, "
            "execution_digest, representation_digest, generation_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                PROJECTION_ID,
                run_id,
                profile.digest,
                execution.provider_id,
                execution.runtime_id,
                execution.build_id,
                execution.hardware_id,
                execution.inference_precision,
                execution.compatibility.status,
                execution.compatibility.reason,
                representation.scalar_encoding,
                representation.byte_order,
                representation.metric,
                representation.codec_version,
                representation.index_version,
                execution.digest,
                representation.digest,
                generation_identity.digest,
            ),
        )


def _append_occurrences(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
    run_id: str,
    checkpoint: str,
    embedder: SemanticEmbedder,
) -> None:
    """Embed and append every in-scope artifact this run does not already cover.

    Scope is the artifacts recorded at or before the run's source checkpoint, so the set
    a run must cover is fixed by its declaration and a later ingest cannot widen it.
    Resume position comes from the durable occurrences. The provider call sits between two
    reads and one write transaction, with no transaction open.
    """
    workspace_id, generation = fence
    dimensions = embedder.profile.dimensions
    ceiling = int(checkpoint)
    with authorised(connection, mutations=False, ddl=False) as fenced:
        last = fenced.execute(
            f"SELECT MAX(evidence_id) FROM {OCCURRENCES_TABLE} "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()
    after = str(last[0]) if last is not None and last[0] is not None else ""

    while True:
        with authorised(connection, mutations=False, ddl=False) as fenced:
            batch = fenced.execute(
                "SELECT evidence_id, source_kind, source_native_id, source_locator "
                "FROM omnivia_evidence_artifacts "
                "WHERE workspace_id = ? AND evidence_id > ? AND recorded_at_us <= ? "
                "ORDER BY evidence_id ASC LIMIT ?",
                (workspace_id, after, ceiling, CHECKPOINT_BATCH),
            ).fetchall()
        if not batch:
            return
        documents = [
            (
                str(evidence_id),
                canonical_model_input(" ".join(part for part in (kind, native_id, locator) if part)),
            )
            for evidence_id, kind, native_id, locator in batch
        ]
        by_digest: dict[str, bytes] = {}
        for _, canonical in documents:
            by_digest.setdefault(input_digest(canonical), canonical)
        with authorised(connection, mutations=False, ddl=False) as fenced:
            stored = {
                str(row[0])
                for row in execute_in_rows(
                    fenced,
                    select=f"SELECT input_digest FROM {VECTORS_TABLE}",
                    where_before="workspace_id = ? AND projection_id = ? AND run_id = ?",
                    in_column="input_digest",
                    leading=(workspace_id, PROJECTION_ID, run_id),
                    ids=tuple(by_digest),
                    order_key=lambda row: row[0],
                )
            }
        missing = [(digest, by_digest[digest]) for digest in by_digest if digest not in stored]

        payloads: list[tuple[str, bytes]] = []
        if missing:
            # Outside every fenced transaction: the provider is slow, untrusted and may fail.
            produced = embedder.embed([canonical for _, canonical in missing])
            if len(produced) != len(missing):
                raise SemanticProjectionError(
                    f"the embedder returned {len(produced)} vectors for {len(missing)} inputs"
                )
            payloads = [
                (digest, encode_vector(values, dimensions=dimensions))
                for (digest, _), values in zip(missing, produced, strict=True)
            ]

        with fenced_transaction(
            connection, identity, workspace_id=workspace_id, fencing_generation=generation
        ):
            representation = embedder.representation
            connection.executemany(
                f"INSERT INTO {VECTORS_TABLE} (workspace_id, projection_id, run_id, "
                "input_digest, dimensions, representation, codec_version, vector) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        workspace_id,
                        PROJECTION_ID,
                        run_id,
                        digest,
                        dimensions,
                        representation.scalar_encoding,
                        representation.codec_version,
                        payload,
                    )
                    for digest, payload in payloads
                ],
            )
            connection.executemany(
                f"INSERT INTO {OCCURRENCES_TABLE} (workspace_id, projection_id, run_id, "
                "evidence_id, input_digest) VALUES (?, ?, ?, ?, ?)",
                [
                    (workspace_id, PROJECTION_ID, run_id, evidence_id, input_digest(canonical))
                    for evidence_id, canonical in documents
                ],
            )
            written = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {OCCURRENCES_TABLE} "
                    "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
                    (workspace_id, PROJECTION_ID, run_id),
                ).fetchone()[0]
            )
            after = documents[-1][0]
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(checkpoint_sequence), -1) + 1 "
                    "FROM omnivia_projection_run_checkpoints "
                    "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
                    (workspace_id, PROJECTION_ID, run_id),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO omnivia_projection_run_checkpoints "
                "(workspace_id, projection_id, run_id, checkpoint_sequence, "
                "created_at_us, source_checkpoint, cursor_json, checkpoint_digest, "
                "input_record_count, output_record_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    PROJECTION_ID,
                    run_id,
                    sequence,
                    _next_us(connection, workspace_id, 0),
                    checkpoint,
                    json.dumps({"after_evidence_id": after}, separators=(",", ":"), sort_keys=True),
                    _occurrences_digest(connection, workspace_id, run_id),
                    written,
                    written,
                ),
            )


def _occurrences_digest(connection: sqlite3.Connection, workspace_id: str, run_id: str) -> str:
    hasher = hashlib.sha256()
    for evidence_id, digest in connection.execute(
        f"SELECT evidence_id, input_digest FROM {OCCURRENCES_TABLE} "
        "WHERE workspace_id = ? AND projection_id = ? AND run_id = ? "
        "ORDER BY evidence_id ASC",
        (workspace_id, PROJECTION_ID, run_id),
    ):
        hasher.update(f"{evidence_id}\x1f{digest}\x1e".encode())
    return f"sha256:{hasher.hexdigest()}"


def _validate_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
    run_id: str,
    checkpoint: str,
    now_us: int,
) -> str:
    """Prove complete coverage, then close the run. Raises before touching the lifecycle.

    Refuses, leaving the run `running` so a later call can finish it, when an in-scope
    artifact has no occurrence, a vector has none, or a stored vector fails the strict
    codec for the run's profile. The recorded digest is computed from the durable rows, so
    a resumed build and an uninterrupted one attest to the same value.
    """
    workspace_id, generation = fence
    with authorised(connection, mutations=False, ddl=False) as fenced:
        uncovered = int(
            fenced.execute(
                "SELECT COUNT(*) FROM omnivia_evidence_artifacts a "
                "WHERE a.workspace_id = ? AND a.recorded_at_us <= ? AND NOT EXISTS ("
                f"  SELECT 1 FROM {OCCURRENCES_TABLE} o WHERE o.workspace_id = a.workspace_id "
                "   AND o.projection_id = ? AND o.run_id = ? AND o.evidence_id = a.evidence_id)",
                (workspace_id, int(checkpoint), PROJECTION_ID, run_id),
            ).fetchone()[0]
        )
        orphans = int(
            fenced.execute(
                f"SELECT COUNT(*) FROM {VECTORS_TABLE} v "
                "WHERE v.workspace_id = ? AND v.projection_id = ? AND v.run_id = ? "
                f"AND NOT EXISTS (SELECT 1 FROM {OCCURRENCES_TABLE} o "
                " WHERE o.workspace_id = v.workspace_id AND o.projection_id = v.projection_id "
                "   AND o.run_id = v.run_id AND o.input_digest = v.input_digest)",
                (workspace_id, PROJECTION_ID, run_id),
            ).fetchone()[0]
        )
        run = fenced.execute(
            "SELECT e.profile_digest, e.representation_digest, e.execution_digest, p.dimensions "
            "FROM omnivia_semantic_run_executions e "
            "JOIN omnivia_semantic_profiles p "
            "  ON p.workspace_id = e.workspace_id AND p.profile_digest = e.profile_digest "
            "WHERE e.workspace_id = ? AND e.projection_id = ? AND e.run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()
        vectors = fenced.execute(
            f"SELECT input_digest, vector FROM {VECTORS_TABLE} "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ? "
            "ORDER BY input_digest ASC",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchall()
    if run is None:
        raise IncompleteSemanticCoverage("the run has no recorded profile or execution")
    if uncovered:
        raise IncompleteSemanticCoverage(
            f"{uncovered} in-scope evidence artifact(s) have no semantic occurrence"
        )
    if orphans:
        raise IncompleteSemanticCoverage(f"{orphans} vector(s) have no occurrence")
    vector_hasher = hashlib.sha256()
    from omnivia_core_runtime.storage.semantic_vectors import decode_vector

    for digest, vector in vectors:
        decode_vector(bytes(vector), dimensions=int(run[3]))
        vector_hasher.update(
            f"{digest}\x1f{hashlib.sha256(bytes(vector)).hexdigest()}\x1e".encode()
        )
    occurrences_digest = _occurrences_digest(connection, workspace_id, run_id)
    count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {OCCURRENCES_TABLE} "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()[0]
    )
    digest = identity_digest(
        "omnivia.semantic-build.v1",
        {
            "profile_digest": str(run[0]),
            "representation_digest": str(run[1]),
            "execution_digest": str(run[2]),
            "occurrences": occurrences_digest,
            "vectors": f"sha256:{vector_hasher.hexdigest()}",
        },
    )
    report = json.dumps(
        {"accepted": True, "occurrences": count, "vectors": len(vectors)},
        separators=(",", ":"),
        sort_keys=True,
    )
    validation_digest = "sha256:" + hashlib.sha256(f"{digest}\x1f{report}".encode()).hexdigest()
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        started = _next_us(connection, workspace_id, now_us)
        connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'validating', "
            "validation_started_at_us = ? "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (started, workspace_id, PROJECTION_ID, run_id),
        )
        connection.execute(
            "INSERT INTO omnivia_projection_validations "
            "(workspace_id, projection_id, run_id, validated_at_us, accepted, "
            "input_record_count, output_record_count, build_digest, report_json, "
            "validation_digest) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                PROJECTION_ID,
                run_id,
                started + 1,
                count,
                count,
                digest,
                report,
                validation_digest,
            ),
        )
        connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'succeeded', "
            "finished_at_us = ?, input_record_count = ?, output_record_count = ?, "
            "build_digest = ? "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (started + 2, count, count, digest, workspace_id, PROJECTION_ID, run_id),
        )
    return digest


def _completed_digest(connection: sqlite3.Connection, workspace_id: str, run_id: str) -> str:
    with authorised(connection, mutations=False, ddl=False) as fenced:
        row = fenced.execute(
            "SELECT build_digest FROM omnivia_projection_runs "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()
    return str(row[0])


def _activate_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
    run_id: str,
    epoch: int,
    checkpoint: str,
    now_us: int,
) -> None:
    """Insert the activation row; the ledger pointer moves as its AFTER trigger's doing."""
    workspace_id, generation = fence
    with authorised(connection, mutations=False, ddl=False) as fenced:
        row = fenced.execute(
            "SELECT build_digest, validation_digest FROM omnivia_projection_validations "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        previous = connection.execute(
            "SELECT active_run_id FROM omnivia_projection_ledger WHERE projection_id = ?",
            (PROJECTION_ID,),
        ).fetchone()
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(activation_sequence), -1) + 1 "
                "FROM omnivia_projection_activations "
                "WHERE workspace_id = ? AND projection_id = ?",
                (workspace_id, PROJECTION_ID),
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO omnivia_projection_activations "
            "(workspace_id, projection_id, activation_sequence, run_id, target_epoch, "
            "previous_run_id, activated_at_us, source_checkpoint, build_digest, "
            "validation_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                PROJECTION_ID,
                sequence,
                run_id,
                epoch,
                previous[0] if previous is not None else None,
                _next_us(connection, workspace_id, now_us),
                checkpoint,
                str(row[0]),
                str(row[1]),
            ),
        )


def _reclaim(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
) -> None:
    """Drop the vectors and occurrences of every failed or superseded run.

    Derived content only: runs, checkpoints, validations, activations, profiles and
    execution rows are append-preserved and stay. Occurrences go first (they reference
    vectors). 0069's DELETE guards refuse any other run, so the predicate is the intent
    and the trigger is the proof.
    """
    workspace_id, generation = fence
    spent = (
        "SELECT run_id FROM omnivia_projection_runs "
        "WHERE workspace_id = ? AND projection_id = ? AND state IN ('failed', 'superseded')"
    )
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        for table in (OCCURRENCES_TABLE, VECTORS_TABLE):
            connection.execute(
                f"DELETE FROM {table} WHERE workspace_id = ? AND projection_id = ? "
                f"AND run_id IN ({spent})",
                (workspace_id, PROJECTION_ID, workspace_id, PROJECTION_ID),
            )


def _counts(connection: sqlite3.Connection, workspace_id: str, run_id: str) -> tuple[int, int]:
    with authorised(connection, mutations=False, ddl=False) as fenced:
        occurrences = fenced.execute(
            f"SELECT COUNT(*) FROM {OCCURRENCES_TABLE} "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()
        vectors = fenced.execute(
            f"SELECT COUNT(*) FROM {VECTORS_TABLE} "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()
    return int(occurrences[0]), int(vectors[0])


def _next_us(connection: sqlite3.Connection, workspace_id: str, now_us: int) -> int:
    """A microsecond stamp at least one past everything this projection has recorded."""
    with authorised(connection, mutations=False, ddl=False) as fenced:
        row = fenced.execute(
            "SELECT MAX(latest) FROM ("
            "  SELECT MAX(started_at_us) AS latest FROM omnivia_projection_runs "
            "  WHERE workspace_id = ? AND projection_id = ?"
            "  UNION ALL"
            "  SELECT MAX(validation_started_at_us) FROM omnivia_projection_runs "
            "  WHERE workspace_id = ? AND projection_id = ?"
            "  UNION ALL"
            "  SELECT MAX(finished_at_us) FROM omnivia_projection_runs "
            "  WHERE workspace_id = ? AND projection_id = ?"
            "  UNION ALL"
            "  SELECT MAX(created_at_us) FROM omnivia_projection_run_checkpoints "
            "  WHERE workspace_id = ? AND projection_id = ?"
            "  UNION ALL"
            "  SELECT MAX(activated_at_us) FROM omnivia_projection_activations "
            "  WHERE workspace_id = ? AND projection_id = ?"
            ")",
            (workspace_id, PROJECTION_ID) * 5,
        ).fetchone()
    latest = int(row[0]) if row is not None and row[0] is not None else 0
    return max(now_us, latest + 1)


__all__ = [
    "CHECKPOINT_BATCH",
    "OCCURRENCES_TABLE",
    "PROJECTION_ID",
    "PROJECTION_KIND",
    "SCHEMA_VERSION",
    "VECTORS_TABLE",
    "BuildOutcome",
    "IncompatibleSemanticGeneration",
    "IncompleteSemanticCoverage",
    "SemanticEmbedder",
    "SemanticGeneration",
    "SemanticProjectionError",
    "SemanticProjectionUnavailable",
    "StaleSemanticProjection",
    "active_generation",
    "build_semantic_projection",
    "current_generation",
    "project_semantic",
    "require_current",
]
