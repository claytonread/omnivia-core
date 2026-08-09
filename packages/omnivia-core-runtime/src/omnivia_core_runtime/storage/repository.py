"""The first read layer over 0008's evidence tables, under a fenced read.

Before this module nothing in the tree read authoritative application storage at all:
there was a migration substrate, an ownership substrate and a probe path, and no
repository, no fenced-read helper and no row-to-contract-object mapping. Every later
V06-3 lane consumes what this establishes, so the properties are worth stating rather
than leaving to be inferred.

**Read-only, and structurally so.** Packet §20.2 Decision 1: *"Lane A remains read-only
against existing storage tables and owns no migration. Any requirement for Lane A to
mutate those tables returns for review."* Every query here runs inside
`authorised(connection)` with `mutations=False, ddl=False` -- the default, stated
explicitly at each call site so the fence is visible rather than inherited. That is not
a convention: the block installs SQLite's own authorizer, so a statement in it that
tried to write is refused by the engine before it reaches a row. A future edit that
smuggles an `INSERT` in here fails at runtime rather than in review.

**Every read carries an explicit total `ORDER BY`.** Packet §8.2 names this as one of
the two places determinism actually breaks: SQLite's unordered result order is not
stable across index changes, and a projection rebuild changes indexes. The frontier and
the ranking are both order-sensitive, so no query below relies on natural order.

**Tombstone and label state are folded from their event streams, not read off a flag.**
0008 stores neither on the artifact -- deliberately, so that "it was tombstoned" is
always an audited act with an actor and a time behind it. This module reduces each
stream in sequence order and reports the last observation, which is the only reading
that agrees with the schema's own design.

**Nothing here decides authority.** It returns candidates; `storage/retrieval.py`
filters them and freezes the frontier. The split is the point: this module holds the
store handle and the module that ranks does not import this one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    EvidenceArtifact,
    ProvenanceEntry,
    RecordTemporalMetadata,
    SourceReference,
)
from omnivia_core_runtime.storage.connection import authorised
from omnivia_core_runtime.storage.projections import EVIDENCE_SEARCH_PROJECTION_ID
from omnivia_core_runtime.storage.retrieval import EvidenceCandidate

#: The projections `evidence.search` reads through. Lane A had none and said so; Lane B
#: contributes exactly one, the FTS5 search projection 0012 declares and
#: `storage/projections/fts.py` builds. The gate below was built and wired before there
#: was anything to put here, because the clause it implements (§20.7) resolves to
#: *refuse* rather than report, and a refusal path first exercised on the day it becomes
#: load-bearing is a refusal path nobody has seen work.
#:
#: The id comes from `storage.projections` rather than being written out, so the gate,
#: the migration, the builder, the activation and the reader are one string. Naming it
#: twice is how a workspace ends up refusing on one projection while serving from
#: another.
CONTRIBUTING_PROJECTIONS: Final[tuple[str, ...]] = (EVIDENCE_SEARCH_PROJECTION_ID,)

#: The one `label_action` in 0008's vocabulary that releases a permission label. Named
#: rather than inlined because it is the single string standing between the label
#: stream and an artifact losing a restriction it still carries.
LABEL_ACTION_WITHDRAWN: Final = "withdrawn"


@dataclass(frozen=True, slots=True)
class ProjectionReadiness:
    """Which contributing projections are missing, and which lag the read point.

    Two categories rather than one because the contract has two error codes for them
    and they mean different things to a caller: `projection_unavailable` says this build
    has nothing to serve from, and `stale_projection` says it has something and that
    thing is behind. Both are refusals -- packet §20.7 removed the middle option
    (*"There is no configurable time tolerance in v0.6"*), so there is no threshold, no
    grace period and no tolerance setting anywhere in this module.

    This value states facts. The refusals are raised by the handler, which is where the
    contract's error codes and retry classes belong.
    """

    missing: tuple[str, ...]
    stale: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.missing and not self.stale


def authoritative_checkpoint(connection: sqlite3.Connection, *, workspace_id: str) -> str:
    """The source checkpoint this workspace's evidence is at, as of this read point.

    The high-water `recorded_at_us` over the workspace's own artifacts, rendered as
    text because that is the column type every 0011 row compares against. An empty
    workspace is checkpoint `"0"`, which is a real checkpoint a projection can be level
    with rather than a missing value that has to be special-cased downstream.
    """
    with authorised(connection, mutations=False, ddl=False) as fenced:
        row = fenced.execute(
            "SELECT COALESCE(MAX(recorded_at_us), 0) FROM omnivia_evidence_artifacts "
            "WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
    return str(int(row[0]))


def projection_readiness(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    source_checkpoint: str,
    contributing: tuple[str, ...] = CONTRIBUTING_PROJECTIONS,
) -> ProjectionReadiness:
    """Compare each contributing projection's active build against the read point.

    The active build is the highest `activation_sequence` for the projection, which is
    the only definition 0011 supports: its pointer trigger refuses any ledger pointer
    write without a matching activation row, so the activation history is authoritative
    and the pointer is downstream of it.

    A projection with no activation at all is *missing*, not fresh. That direction
    matters and it is the one an implementation drifts on: treating "no rows" as
    "nothing to compare, so proceed" is exactly the silent success §20.7 forbids.

    Equality, not tolerance. A projection whose activation names a checkpoint different
    from the read point is stale, full stop.
    """
    if not contributing:
        return ProjectionReadiness(missing=(), stale=())

    missing: list[str] = []
    stale: list[str] = []
    with authorised(connection, mutations=False, ddl=False) as fenced:
        for projection_id in sorted(contributing):
            row = fenced.execute(
                "SELECT source_checkpoint FROM omnivia_projection_activations "
                "WHERE workspace_id = ? AND projection_id = ? "
                "ORDER BY activation_sequence DESC LIMIT 1",
                (workspace_id, projection_id),
            ).fetchone()
            if row is None:
                missing.append(projection_id)
            elif str(row[0]) != source_checkpoint:
                stale.append(projection_id)
    return ProjectionReadiness(missing=tuple(missing), stale=tuple(stale))


def read_evidence_candidates(
    connection: sqlite3.Connection, *, workspace_id: str
) -> tuple[EvidenceCandidate, ...]:
    """Every evidence artifact in one workspace, as unfiltered candidates.

    Unfiltered is deliberate and is why this returns `EvidenceCandidate` rather than an
    authorized frontier: authority is decided in one place, by
    `retrieval.authorized_frontier`, over a set this module hands it whole. A read layer
    that pre-filtered by authority would put the ACL decision in two places, and two
    places is how one of them drifts.

    The workspace predicate below is *not* the authorization filter -- it is domain
    separation on the query, and `authorized_frontier` applies the workspace filter
    again over the returned set. That duplication is intentional in the same way the
    session grant and the `ServiceBinding` both name the workspace: the second check
    costs one comparison and does not depend on this query being written correctly.
    """
    with authorised(connection, mutations=False, ddl=False) as fenced:
        artifact_rows = fenced.execute(
            "SELECT evidence_id, workspace_id, source_kind, source_native_id, "
            "       source_locator, source_retrieved_at_us, event_at_us, observed_at_us, "
            "       ingested_at_us, recorded_at_us, content_checksum, media_type, "
            "       original_metadata_json, sensitivity, parser_status, ingestion_status, "
            "       import_run_id "
            "FROM omnivia_evidence_artifacts WHERE workspace_id = ? "
            "ORDER BY evidence_id ASC",
            (workspace_id,),
        ).fetchall()
        label_rows = fenced.execute(
            "SELECT evidence_id, label_sequence, label_action, permission_label "
            "FROM omnivia_evidence_permission_labels WHERE workspace_id = ? "
            "ORDER BY evidence_id ASC, label_sequence ASC",
            (workspace_id,),
        ).fetchall()
        provenance_rows = fenced.execute(
            "SELECT evidence_id, provenance_sequence, actor_id, actor_kind, action, "
            "       occurred_at_us, reason_code, reason_comment, tombstoned_observation "
            "FROM omnivia_evidence_provenance_events WHERE workspace_id = ? "
            "ORDER BY evidence_id ASC, provenance_sequence ASC",
            (workspace_id,),
        ).fetchall()

    labels = _fold_labels(label_rows)
    history, tombstoned = _fold_provenance(provenance_rows)
    return tuple(
        _candidate(
            row,
            permission_labels=labels.get(row[0], ()),
            provenance_history=history.get(row[0], ()),
            tombstoned=tombstoned.get(row[0], False),
        )
        for row in artifact_rows
    )


def _fold_labels(rows: list[Any]) -> dict[str, tuple[str, ...]]:
    """Reduce the label event stream to each artifact's effective label set.

    0008 fixes `label_action` to `attached`, `withdrawn` or `corrected`, applied in
    sequence order: a label attached and later withdrawn is absent, and one withdrawn
    and later re-attached is present. Reading the stream any other way -- taking the
    first event, or taking the set of every label ever named -- reports labels the
    workspace no longer asserts, and on an ACL input that direction of error admits
    material rather than withholding it.

    `withdrawn` is the only action that removes. `corrected` restates a label the
    artifact carries, so it adds for the same reason `attached` does; the default is to
    hold rather than to release, because an action this build does not recognise must
    not be able to strip a restriction.
    """
    effective: dict[str, set[str]] = {}
    for evidence_id, _sequence, action, label in rows:
        held = effective.setdefault(evidence_id, set())
        if action == LABEL_ACTION_WITHDRAWN:
            held.discard(label)
        else:
            held.add(label)
    return {key: tuple(sorted(value)) for key, value in effective.items()}


def _fold_provenance(
    rows: list[Any],
) -> tuple[dict[str, tuple[ProvenanceEntry, ...]], dict[str, bool]]:
    """Reduce the provenance stream to each artifact's history and tombstone status.

    The status is the *last non-null* `tombstoned_observation`, not the last row: most
    events observe nothing about tombstoning and record `NULL`, so taking the last row's
    value would let an unrelated later event silently un-tombstone an artifact.
    """
    history: dict[str, list[ProvenanceEntry]] = {}
    tombstoned: dict[str, bool] = {}
    for row in rows:
        (
            evidence_id,
            _sequence,
            actor_id,
            actor_kind,
            action,
            occurred_at_us,
            reason_code,
            reason_comment,
            observation,
        ) = row
        history.setdefault(evidence_id, []).append(
            ProvenanceEntry(
                actor_id=actor_id,
                actor_kind=actor_kind,
                action=action,
                occurred_at=_timestamp(occurred_at_us),
                reason_code=reason_code,
                reason_comment=reason_comment,
            )
        )
        if observation is not None:
            tombstoned[evidence_id] = bool(observation)
    return ({key: tuple(value) for key, value in history.items()}, tombstoned)


def _candidate(
    row: Any,
    *,
    permission_labels: tuple[str, ...],
    provenance_history: tuple[ProvenanceEntry, ...],
    tombstoned: bool,
) -> EvidenceCandidate:
    """One storage row as one candidate, with its contract DTO already built."""
    (
        evidence_id,
        workspace_id,
        source_kind,
        source_native_id,
        source_locator,
        source_retrieved_at_us,
        event_at_us,
        observed_at_us,
        ingested_at_us,
        recorded_at_us,
        content_checksum,
        media_type,
        original_metadata_json,
        sensitivity,
        parser_status,
        ingestion_status,
        import_run_id,
    ) = row
    artifact = EvidenceArtifact(
        evidence_id=evidence_id,
        workspace_id=workspace_id,
        source=SourceReference(
            kind=source_kind,
            source_id=source_native_id,
            locator=source_locator,
            retrieved_at=_optional_timestamp(source_retrieved_at_us),
        ),
        temporal=RecordTemporalMetadata(
            ingested_at=_timestamp(ingested_at_us),
            recorded_at=_timestamp(recorded_at_us),
            event_at=_optional_timestamp(event_at_us),
            observed_at=_optional_timestamp(observed_at_us),
        ),
        content_checksum=content_checksum,
        media_type=media_type,
        metadata=_metadata(original_metadata_json),
        permission_labels=permission_labels,
        sensitivity=sensitivity,
        tombstoned=tombstoned,
        parser_status=parser_status,
        ingestion_status=ingestion_status,
        provenance_history=provenance_history,
        import_run_id=import_run_id,
    )
    return EvidenceCandidate(
        evidence_id=evidence_id,
        workspace_id=workspace_id,
        content_checksum=content_checksum,
        sensitivity=sensitivity,
        permission_labels=permission_labels,
        tombstoned=tombstoned,
        recorded_at_us=int(recorded_at_us),
        # Identity, not content. `search_text` is what a Lane A query matches, and
        # every component of it is an identifier 0008 already stores on the artifact.
        search_text=" ".join(
            part for part in (source_kind, source_native_id, source_locator) if part
        ),
        artifact=artifact,
    )


def _metadata(document: str) -> dict[str, Any]:
    """The artifact's opaque metadata, or an empty object if it will not decode.

    0008 constrains the column to a JSON object, so a failure here is a corrupt row
    rather than a caller's doing. It degrades to `{}` instead of raising because the
    metadata is opaque passthrough and refusing a whole search over one unreadable
    blob's decoration would be a worse answer than returning the artifact without it.
    """
    decoded: object
    try:
        decoded = json.loads(document)
    except ValueError:
        decoded = None
    return decoded if isinstance(decoded, dict) else {}


def _timestamp(microseconds: int) -> str:
    """One microsecond instant as the contract's `Timestamp`.

    Millisecond precision with a `Z` suffix, which is what the contract's pattern
    accepts and what every other timestamp this service emits already looks like.
    """
    moment = datetime.fromtimestamp(int(microseconds) / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _optional_timestamp(microseconds: int | None) -> str | None:
    return None if microseconds is None else _timestamp(microseconds)


__all__ = [
    "CONTRIBUTING_PROJECTIONS",
    "ProjectionReadiness",
    "authoritative_checkpoint",
    "projection_readiness",
    "read_evidence_candidates",
]
