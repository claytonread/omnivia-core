"""V06-1 M2 acceptance: blobs, staged sources and L0 evidence (M2-01 … M2-43b).

`0008_blobs_staged_sources_and_evidence.sql` adds nine append-preserved tables and
nothing else. This file is the durable proof of what that migration is, and just as
importantly of what it is not.

What it is. A unique consecutive successor to the accepted `0007`, pinned by content
checksum, whose objects are exactly nine tables, nineteen named indexes and
twenty-seven statement triggers; a slice that applies to a pristine and to an
exactly-adopted Phase 0 workspace, converging on the same M2 schema without
disturbing one legacy row, column or value; and a schema whose immutability is
enforced rather than asserted -- every UPDATE and DELETE aborts unconditionally, for
the current fenced owner as much as for anyone else, and every INSERT must satisfy
the complete connection-authority, guard, workspace-state and lease predicate plus
the singleton workspace binding.

What it is not. There is no repository, filesystem blob store, importer, handler,
dispatcher, ACL evaluator, grant resolver, garbage collector, governed record, job
expansion or projection here, and the tests below hold the migration to that: it
contains no DML at all, no deferred foreign key, no cascade, no physical blob-store
column, and no mutable truth/current/deleted flag that a later writer could be
tempted to rewrite. Its triggers do nothing but refuse.

**Residual obligations, carried forward deliberately.** Two, stated rather than
quietly implied. First, nothing here proves a byte was ever fsynced, atomically
published or is still on disk: `omnivia_blob_objects` records that a verification
happened, and M2-27 proves a later "it is gone" is *recordable*, not that anything
detects it. Second, the atomicity proved here (M2-14, M2-38) is atomicity of the M2
slice itself; a real ingestion vertical committing evidence alongside its own domain
mutation is that vertical's obligation and is not discharged by anything in this file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from omnivia_core_runtime.ownership.fencing import (
    SchemaDrift,
    StaleGeneration,
    assert_guards_intact,
    close_guard,
    fenced_transaction,
    guarded_tables,
    open_guard,
    verify_fingerprint,
)
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    ProcessEvidence,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.storage import migrations as migrations_module
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    create_verified_backup,
    new_attempt_id,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    SchemaCreationRefused,
    StorageError,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
    table_names,
)
from omnivia_core_runtime.storage.inventory import (
    capture_inventory,
    compare_inventories,
)
from omnivia_core_runtime.storage.legacy import LegacyMigrationError, rollback_migration
from omnivia_core_runtime.storage.migrations import (
    BASELINE_ADOPTED,
    BASELINE_PRISTINE,
    GENERATION_ONE,
    PHASE0_BASELINE_FILE,
    Migration,
    applied_migrations,
    apply_pending_migrations,
    bootstrap_generation_one,
    canonical_schema_fingerprint,
    canonical_schema_tables,
    load_migrations,
    materialise_phase0_baseline,
    phase0_baseline_sql,
    read_workspace_state,
)

# --- the migration under test -------------------------------------------------

MIGRATION_VERSION = 8
MIGRATION_NAME = "0008_blobs_staged_sources_and_evidence.sql"
PREDECESSOR_NAME = "0007_application_audit_and_idempotency.sql"

#: The accepted base's migration corpus, pinned by content. Every existing migration
#: is immutable once applied, so an edit to any of them is a defect this file must
#: report rather than something a later reader has to notice in a diff.
ACCEPTED_MIGRATION_CHECKSUMS = {
    "0000_phase0_baseline.sql": (
        "28b2e553cf461aeae6ec93ce9fd2170c59a7efd5d1b0b956ced5cdaec0090de1"
    ),
    "0001_ownership_substrate.sql": (
        "2ca7c190c8678994a2229a8ee9b2d1c12b259406de6dada108202cf51a8aa87c"
    ),
    "0002_mutation_guard.sql": (
        "10807d2ff70ba1e1c4c9b407b9cf1efd7fdb11767726ae10bcb0831de40b1d06"
    ),
    "0003_complete_mutation_guard.sql": (
        "bf1a837c16d3e012db0c4fa78071d7198c116f27b75dfd0308fce3f4ec5d3f0f"
    ),
    "0004_bind_guard_to_lease.sql": (
        "67048d8b1d9cf5dd565f7dff0c2bbd07529a3c18f3b8e05ae4add7d1521c1cde"
    ),
    "0005_require_connection_authority.sql": (
        "e6cc0d7f6d0ce59c60a35eeaed77780b47af93175bbcb3eea933cc98c7832e89"
    ),
    "0006_protect_ownership_substrate.sql": (
        "c3c1501b0cf22ccaa3af16d1ea2c77e7c62dd3b94d4e26cf5cd0041caf1a471e"
    ),
    PREDECESSOR_NAME: (
        "1a315bb097092c1981bf9c3175d8a8c69fb5436006f7a4e6145f9ef9ca42891a"
    ),
}

BLOBS = "omnivia_blob_objects"
INTEGRITY = "omnivia_blob_integrity_events"
STAGED = "omnivia_staged_sources"
EVIDENCE = "omnivia_evidence_artifacts"
LABELS = "omnivia_evidence_permission_labels"
PROVENANCE = "omnivia_evidence_provenance_events"
REFERENCES = "omnivia_evidence_event_references"
RECORDS = "omnivia_normalized_source_records"
SPANS = "omnivia_normalized_source_spans"

#: The nine tables, in the dependency order a writer must respect.
M2_TABLES = (
    BLOBS,
    INTEGRITY,
    STAGED,
    EVIDENCE,
    LABELS,
    PROVENANCE,
    REFERENCES,
    RECORDS,
    SPANS,
)

#: Parent keys for the composite foreign keys, declared by name so they reach the
#: canonical fingerprint, which filters implicit `sqlite_autoindex_*` out.
M2_PARENT_KEY_INDEXES = (
    "omnivia_idx_blob_objects_workspace_digest",
    "omnivia_idx_blob_objects_identity",
    "omnivia_idx_staged_sources_ref_workspace",
    "omnivia_idx_evidence_artifacts_id_workspace",
    "omnivia_idx_evidence_artifacts_identity_source",
    "omnivia_idx_evidence_artifacts_identity_blob",
    "omnivia_idx_evidence_provenance_events_identity_source",
    "omnivia_idx_normalized_source_records_id_evidence",
)

#: The durable half of "monotonic": one sequence value per parent stream.
M2_SEQUENCE_INDEXES = (
    "omnivia_idx_blob_integrity_events_digest_sequence",
    "omnivia_idx_evidence_permission_labels_sequence",
    "omnivia_idx_evidence_provenance_events_sequence",
    "omnivia_idx_evidence_event_references_ordinal",
    "omnivia_idx_normalized_source_records_sequence",
    "omnivia_idx_normalized_source_spans_sequence",
)

#: Material read paths over tables that only ever grow.
M2_READ_PATH_INDEXES = (
    "omnivia_idx_blob_integrity_events_workspace_outcome_time",
    "omnivia_idx_staged_sources_workspace_outcome",
    "omnivia_idx_evidence_artifacts_workspace_source",
    "omnivia_idx_evidence_artifacts_workspace_blob",
    "omnivia_idx_evidence_artifacts_import_run",
)

M2_INDEXES = M2_PARENT_KEY_INDEXES + M2_SEQUENCE_INDEXES + M2_READ_PATH_INDEXES

M2_TRIGGERS = tuple(
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in M2_TABLES
    for statement in ("insert", "update", "delete")
)

#: The exact declared columns of every named index, so a silently reshaped index is a
#: failure rather than a name that still resolves.
M2_INDEX_COLUMNS: dict[str, tuple[str, ...]] = {
    "omnivia_idx_blob_objects_workspace_digest": ("workspace_id", "content_digest"),
    "omnivia_idx_blob_objects_identity": (
        "workspace_id",
        "content_digest",
        "content_length_bytes",
    ),
    "omnivia_idx_staged_sources_ref_workspace": (
        "staged_source_ref",
        "workspace_id",
    ),
    "omnivia_idx_evidence_artifacts_id_workspace": ("evidence_id", "workspace_id"),
    "omnivia_idx_evidence_artifacts_identity_source": (
        "evidence_id",
        "workspace_id",
        "source_kind",
        "source_native_id",
    ),
    "omnivia_idx_evidence_artifacts_identity_blob": (
        "evidence_id",
        "workspace_id",
        "blob_content_digest",
    ),
    "omnivia_idx_evidence_provenance_events_identity_source": (
        "provenance_event_id",
        "evidence_id",
        "workspace_id",
        "source_kind",
        "source_native_id",
    ),
    "omnivia_idx_normalized_source_records_id_evidence": (
        "normalized_record_id",
        "evidence_id",
        "workspace_id",
    ),
    "omnivia_idx_blob_integrity_events_digest_sequence": (
        "workspace_id",
        "content_digest",
        "integrity_sequence",
    ),
    "omnivia_idx_evidence_permission_labels_sequence": (
        "evidence_id",
        "label_sequence",
    ),
    "omnivia_idx_evidence_provenance_events_sequence": (
        "evidence_id",
        "provenance_sequence",
    ),
    "omnivia_idx_evidence_event_references_ordinal": (
        "provenance_event_id",
        "reference_ordinal",
    ),
    "omnivia_idx_normalized_source_records_sequence": (
        "evidence_id",
        "record_sequence",
    ),
    "omnivia_idx_normalized_source_spans_sequence": (
        "normalized_record_id",
        "span_sequence",
    ),
    "omnivia_idx_blob_integrity_events_workspace_outcome_time": (
        "workspace_id",
        "outcome",
        "checked_at_us",
    ),
    "omnivia_idx_staged_sources_workspace_outcome": (
        "workspace_id",
        "staging_outcome",
        "recorded_at_us",
    ),
    "omnivia_idx_evidence_artifacts_workspace_source": (
        "workspace_id",
        "source_kind",
        "source_native_id",
    ),
    "omnivia_idx_evidence_artifacts_workspace_blob": (
        "workspace_id",
        "blob_content_digest",
    ),
    "omnivia_idx_evidence_artifacts_import_run": ("import_run_id",),
}

#: The exact foreign key graph: child table -> {(parent, child columns, parent
#: columns)}. Acyclic by construction -- blob, then staged source, then artifact, then
#: that artifact's labels, provenance, references and normalized records -- which is
#: why nothing here needs a commit-time cycle and therefore a deferred key.
M2_FOREIGN_KEYS: dict[str, set[tuple[str, tuple[str, ...], tuple[str, ...]]]] = {
    BLOBS: set(),
    # Deliberately none: the observations most worth keeping are about a digest that
    # has no accepted blob row, and a foreign key would make those unrecordable.
    INTEGRITY: set(),
    STAGED: {
        (
            BLOBS,
            ("blob_workspace_id", "blob_content_digest", "content_length_bytes"),
            ("workspace_id", "content_digest", "content_length_bytes"),
        )
    },
    EVIDENCE: {
        (
            BLOBS,
            ("workspace_id", "blob_content_digest"),
            ("workspace_id", "content_digest"),
        ),
        (
            STAGED,
            ("staged_source_ref", "workspace_id"),
            ("staged_source_ref", "workspace_id"),
        ),
        ("omnivia_durable_jobs", ("import_run_id",), ("job_id",)),
    },
    LABELS: {
        (
            EVIDENCE,
            ("evidence_id", "workspace_id"),
            ("evidence_id", "workspace_id"),
        )
    },
    PROVENANCE: {
        (
            EVIDENCE,
            ("evidence_id", "workspace_id", "source_kind", "source_native_id"),
            ("evidence_id", "workspace_id", "source_kind", "source_native_id"),
        ),
        (
            "omnivia_application_audit_events",
            ("audit_ref", "workspace_id"),
            ("audit_ref", "workspace_id"),
        ),
    },
    REFERENCES: {
        (
            PROVENANCE,
            (
                "provenance_event_id",
                "evidence_id",
                "workspace_id",
                "source_kind",
                "source_native_id",
            ),
            (
                "provenance_event_id",
                "evidence_id",
                "workspace_id",
                "source_kind",
                "source_native_id",
            ),
        )
    },
    RECORDS: {
        (
            EVIDENCE,
            ("evidence_id", "workspace_id", "evidence_blob_digest"),
            ("evidence_id", "workspace_id", "blob_content_digest"),
        )
    },
    SPANS: {
        (
            RECORDS,
            ("normalized_record_id", "evidence_id", "workspace_id"),
            ("normalized_record_id", "evidence_id", "workspace_id"),
        )
    },
}

WORKSPACE_ID = "ws-m2-blobs-0001"
OTHER_WORKSPACE_ID = "ws-m2-blobs-0002"
SERVICE_INSTANCE = "svc-m2-one"

#: The internal blob address domain: `sha256:` plus 64 lowercase hex, one algorithm,
#: one length, one letter case, because both sides must recompute and compare it.
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
DIGEST_E = "sha256:" + "e" * 64

#: The separate public `EvidenceChecksum` domain: provider-neutral `algorithm:digest`.
#: A provider that published this instead of a SHA-256 must still be storable.
PROVIDER_CHECKSUM = "md5:9e107d9d372bb6826bd81d3542a419d6"

IMPORT_JOB_ID = "job-import-0001"
OTHER_JOB_ID = "job-compaction-0001"

BASE_US = 1_700_000_000_000_000

#: The largest signed 64-bit microsecond value, carried without narrowing.
MAX_SIGNED_64 = 9_223_372_036_854_775_807

#: A document that predates 1970. `event_at_us` and `observed_at_us` carry the full
#: signed domain on purpose; every time the system itself stamps is positive.
PRE_EPOCH_US = -1_234_567_890_123

#: Every way a write without current authority is refused, across all the layers that
#: can refuse one. Which layer answers depends on where the writer is standing, and no
#: single message is the right expectation for all of them: the connection authorizer
#: says `not authorized` and is reached first on a governed connection whose guard is
#: closed; the persisted triggers say `unguarded` or `append-only` and are what a
#: writer that never went through the authorizer meets; a stock SQLite process finds
#: `no such function` because the connection-authority function is connection-local;
#: and a read-only open is refused `from outside the runtime`.
REFUSED_EXTERNAL_WRITE = (
    "unguarded|no such function|from outside the runtime|append-only|not authorized"
)


def migration_under_test() -> Migration:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


#: The only statement kinds that build storage rather than guard it.
DDL_ONLY_PREFIXES = ("CREATE TABLE", "CREATE UNIQUE INDEX", "CREATE INDEX")


def ddl_only_statements() -> tuple[str, ...]:
    """The migration's table and index statements, without its triggers.

    Replaying only these gives a database where the declared constraints are the sole
    defence, which is how M2-29b, M2-31 and M2-35b prove the foreign keys, the UNIQUE
    stream indexes and the sequence CHECKs refuse on their own rather than being
    carried by the trigger layer standing in front of them.

    A whitelist, not "everything that is not a CREATE TRIGGER". Excluding by that one
    prefix silently kept the file's `DROP TRIGGER IF EXISTS
    omnivia_guard_durable_jobs_update`, so the harness meant to remove this slice's
    guards was deleting an *inherited* one instead -- and every claim made against it
    would have been made against a schema missing a trigger no test had asked to lose.
    Every test above that calls `replay_without_m2_triggers` depends on this whitelist
    staying exact.
    """
    return tuple(
        statement
        for statement in MIGRATION_STATEMENTS
        if " ".join(statement.split()).upper().startswith(DDL_ONLY_PREFIXES)
    )


# --- fixtures and helpers -----------------------------------------------------


def make_identity(
    instance: str = SERVICE_INSTANCE, pid: int = 4242
) -> ServiceInstanceIdentity:
    return ServiceInstanceIdentity(
        service_instance_id=instance,
        installation_id="inst-m2",
        process=ProcessEvidence(
            pid=pid, start_time="100", boot_id="boot-m2", os_principal="me"
        ),
    )


def bootstrap_and_migrate(
    path: Path,
    *,
    workspace_id: str = WORKSPACE_ID,
    adopt_phase0: bool = True,
    service_instance_id: str = SERVICE_INSTANCE,
) -> None:
    """Take a database to the full canonical schema through the real migrator."""
    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = bootstrap_generation_one(
            maintenance,
            workspace_id=workspace_id,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=adopt_phase0,
            service_instance_id=service_instance_id,
        )
        apply_pending_migrations(
            maintenance,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=service_instance_id,
            fencing_generation=state.fencing_generation,
            workspace_id=workspace_id,
        )
    finally:
        maintenance.close()


@dataclass(frozen=True)
class Owned:
    """A migrated workspace this service instance holds the lease and guard for."""

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    generation: int
    path: Path


def take_ownership(path: Path, *, workspace_id: str = WORKSPACE_ID) -> Owned:
    identity = make_identity()
    connection = open_database(path, OpenMode.SERVICE_OWNED)
    lease = acquire_lease(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=workspace_id,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=workspace_id,
        fencing_generation=lease.fencing_generation,
    )
    return Owned(
        connection=connection,
        identity=identity,
        generation=lease.fencing_generation,
        path=path,
    )


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    """A workspace adopted from the frozen Phase 0 artifact and fully migrated."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    bootstrap_and_migrate(path)
    return path


@pytest.fixture
def owned(migrated: Path) -> Iterator[Owned]:
    """A migrated workspace under current, valid write authority."""
    holder = take_ownership(migrated)
    yield holder
    holder.connection.close()


BLOB_DEFAULTS: dict[str, object] = {
    "workspace_id": WORKSPACE_ID,
    "content_digest": DIGEST_A,
    "content_length_bytes": 1024,
    "created_at_us": BASE_US,
    "verified_at_us": BASE_US + 1,
}

INTEGRITY_DEFAULTS: dict[str, object] = {
    "integrity_event_id": "bie-0001",
    "workspace_id": WORKSPACE_ID,
    "content_digest": DIGEST_A,
    "integrity_sequence": 1,
    "outcome": "verified",
    "observed_digest": None,
    "observed_length_bytes": None,
    "expected_length_bytes": None,
    "inventory_id": None,
    "checked_at_us": BASE_US + 2,
}

STAGED_DEFAULTS: dict[str, object] = {
    "staged_source_ref": "stg-0001",
    "workspace_id": WORKSPACE_ID,
    "source_kind": "filesystem.archive",
    "declared_checksum": DIGEST_A,
    "content_length_bytes": 1024,
    "media_type": "application/zip",
    "source_version": None,
    "computed_checksum": DIGEST_A,
    "original_metadata_json": '{"kind":"archive"}',
    "original_metadata_digest": DIGEST_C,
    "staging_outcome": "verified",
    "blob_workspace_id": WORKSPACE_ID,
    "blob_content_digest": DIGEST_A,
    "recorded_at_us": BASE_US + 3,
}

EVIDENCE_DEFAULTS: dict[str, object] = {
    "evidence_id": "evd-0001",
    "workspace_id": WORKSPACE_ID,
    "source_kind": "filesystem.archive",
    "source_native_id": "doc-1",
    "source_locator": "archive://doc.md",
    "source_retrieved_at_us": BASE_US,
    "event_at_us": BASE_US - 10,
    "observed_at_us": BASE_US - 5,
    "ingested_at_us": BASE_US + 4,
    "recorded_at_us": BASE_US + 5,
    "content_checksum": DIGEST_A,
    "blob_content_digest": DIGEST_A,
    "media_type": "text/markdown",
    "original_metadata_json": '{"title":"doc"}',
    "original_metadata_digest": DIGEST_C,
    "sensitivity": "internal",
    "parser_status": "parsed",
    "ingestion_status": "ingested",
    "staged_source_ref": "stg-0001",
    "import_run_id": None,
}

LABEL_DEFAULTS: dict[str, object] = {
    "label_event_id": "lbl-0001",
    "evidence_id": "evd-0001",
    "workspace_id": WORKSPACE_ID,
    "label_sequence": 1,
    "label_action": "attached",
    # An `OpenCode`, so the separator is a dot. `group:engineering` reads naturally
    # and is not in the domain: a colon is outside `[a-z0-9_.]`.
    "permission_label": "group.engineering",
    "recorded_at_us": BASE_US + 6,
}

PROVENANCE_DEFAULTS: dict[str, object] = {
    "provenance_event_id": "prv-0001",
    "evidence_id": "evd-0001",
    "workspace_id": WORKSPACE_ID,
    "provenance_sequence": 1,
    "actor_id": "actor-1",
    "actor_kind": "service",
    "action": "captured",
    "occurred_at_us": BASE_US + 7,
    "reason_code": None,
    "reason_comment": None,
    "parser_status": None,
    "ingestion_status": None,
    "tombstoned_observation": None,
    "source_kind": "filesystem.archive",
    "source_native_id": "doc-1",
    "audit_ref": None,
}

REFERENCE_DEFAULTS: dict[str, object] = {
    "event_reference_id": "ref-0001",
    "provenance_event_id": "prv-0001",
    "evidence_id": "evd-0001",
    "workspace_id": WORKSPACE_ID,
    "reference_ordinal": 1,
    "source_kind": "filesystem.archive",
    "source_native_id": "doc-1",
    "source_locator": None,
    "source_retrieved_at_us": None,
    "span_pointer": "/body/0",
    "span_start_offset": 0,
    "span_end_offset": 10,
    "excerpt": None,
}

RECORD_DEFAULTS: dict[str, object] = {
    "normalized_record_id": "nrc-0001",
    "evidence_id": "evd-0001",
    "workspace_id": WORKSPACE_ID,
    "evidence_blob_digest": DIGEST_A,
    "record_sequence": 1,
    "record_type": "message",
    "schema_version": "1",
    "content_json": '{"body":"hello"}',
    "content_digest": DIGEST_D,
    "parser_id": "parser.markdown",
    "parser_version": "1.0.0",
    "recorded_at_us": BASE_US + 8,
}

SPAN_DEFAULTS: dict[str, object] = {
    "normalized_span_id": "nsp-0001",
    "normalized_record_id": "nrc-0001",
    "evidence_id": "evd-0001",
    "workspace_id": WORKSPACE_ID,
    "span_sequence": 1,
    "span_kind": "byte_range",
    "span_pointer": "/body/0",
    "span_start_offset": 0,
    "span_end_offset": 10,
    "recorded_at_us": BASE_US + 9,
}

DEFAULTS_BY_TABLE: dict[str, dict[str, object]] = {
    BLOBS: BLOB_DEFAULTS,
    INTEGRITY: INTEGRITY_DEFAULTS,
    STAGED: STAGED_DEFAULTS,
    EVIDENCE: EVIDENCE_DEFAULTS,
    LABELS: LABEL_DEFAULTS,
    PROVENANCE: PROVENANCE_DEFAULTS,
    REFERENCES: REFERENCE_DEFAULTS,
    RECORDS: RECORD_DEFAULTS,
    SPANS: SPAN_DEFAULTS,
}

#: A second row for each table that collides with nothing already seeded: distinct
#: primary key, and for a stream a sequence that strictly advances. Without this a
#: negative would be refused by UNIQUE or by the sequence trigger before the
#: constraint actually under test was ever consulted.
UNIQUE_IDS: dict[str, dict[str, object]] = {
    BLOBS: {"content_digest": DIGEST_B},
    INTEGRITY: {"integrity_event_id": "bie-second", "integrity_sequence": 2},
    STAGED: {"staged_source_ref": "stg-second"},
    EVIDENCE: {"evidence_id": "evd-second", "source_native_id": "doc-2"},
    LABELS: {"label_event_id": "lbl-second", "label_sequence": 2},
    PROVENANCE: {"provenance_event_id": "prv-second", "provenance_sequence": 2},
    REFERENCES: {"event_reference_id": "ref-second", "reference_ordinal": 2},
    RECORDS: {"normalized_record_id": "nrc-second", "record_sequence": 2},
    SPANS: {"normalized_span_id": "nsp-second", "span_sequence": 2},
}

#: Each causally ordered append stream: its table, its sequence column and the parent
#: the sequence is scoped to.
SEQUENCE_STREAMS: tuple[tuple[str, str, str], ...] = (
    (INTEGRITY, "integrity_sequence", "content_digest"),
    (LABELS, "label_sequence", "evidence_id"),
    (PROVENANCE, "provenance_sequence", "evidence_id"),
    (REFERENCES, "reference_ordinal", "provenance_event_id"),
    (RECORDS, "record_sequence", "evidence_id"),
    (SPANS, "span_sequence", "normalized_record_id"),
)


def row_for(table: str, **overrides: object) -> dict[str, object]:
    values = dict(DEFAULTS_BY_TABLE[table])
    values.update(overrides)
    return values


def unique_row_for(table: str, **overrides: object) -> dict[str, object]:
    return row_for(table, **{**UNIQUE_IDS[table], **overrides})


def insert(
    connection: sqlite3.Connection, table: str, values: dict[str, object]
) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def write(holder: Owned, table: str, **overrides: object) -> None:
    """Insert one row under current authority, in its own fenced transaction."""
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(holder.connection, table, row_for(table, **overrides))


DURABLE_JOBS = "omnivia_durable_jobs"

#: The one inherited guard `0008` reopens. It is dropped and recreated under this
#: existing name, so the migration gains a CREATE TRIGGER statement and the database
#: gains no trigger object.
INHERITED_JOB_GUARD = "omnivia_guard_durable_jobs_update"

#: The statements as the migrator runs them: comments stripped, which is what makes a
#: claim about "this file contains no DML" a claim about what executes rather than
#: about the prose around it.
MIGRATION_EXECUTABLE_SQL = "\n".join(MIGRATION_STATEMENTS)

#: The two durable jobs the evidence tests need: one that is an `ingestion.import`
#: and one that exists but is not, because a foreign key can prove a job exists and
#: cannot prove what kind of work it was.
JOB_ROWS: tuple[dict[str, object], ...] = (
    {
        "job_id": IMPORT_JOB_ID,
        "job_type": "ingestion.import",
        "state": "queued",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    },
    {
        "job_id": OTHER_JOB_ID,
        "job_type": "maintenance.compaction",
        "state": "queued",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    },
)


def seed_chain(holder: Owned) -> None:
    """One coherent row in each of the nine tables, in one fenced transaction.

    A blob, an integrity observation of it, the staged source that verified against
    it, the evidence artifact captured from that staging, that artifact's label,
    provenance event and reference, and the normalized record and span parsed out of
    it. Written as one unit because a repository must insert an artifact and its
    initial capture event together; that is a transaction shape, not a schema
    constraint, and nothing here is deferred to pretend otherwise.
    """
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        for job in JOB_ROWS:
            insert(holder.connection, DURABLE_JOBS, dict(job))
        for table in M2_TABLES:
            insert(holder.connection, table, row_for(table))


def count(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def row_snapshot(
    connection: sqlite3.Connection, table: str, identity: dict[str, object]
) -> tuple[object, ...]:
    """Return one complete row for a fixed test-owned table identity."""
    predicates = " AND ".join(f"{column} = ?" for column in identity)
    row = connection.execute(
        f"SELECT * FROM {table} WHERE {predicates}", tuple(identity.values())
    ).fetchone()
    assert row is not None
    return tuple(row)


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {table: count(connection, table) for table in M2_TABLES}


def object_names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'",
            (kind,),
        ).fetchall()
    }


def object_sql(connection: sqlite3.Connection, name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None, name
    return str(row[0])


def read_user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    assert row is not None
    return int(row[0])


def migration_attempts(
    connection: sqlite3.Connection, *, version: int = MIGRATION_VERSION
) -> list[tuple[str, str, str | None, str | None]]:
    """`(outcome, started_at, finished_at, detail)` rows recorded for `version`."""
    return [
        (str(row[0]), str(row[1]), row[2] and str(row[2]), row[3] and str(row[3]))
        for row in connection.execute(
            "SELECT outcome, started_at, finished_at, detail "
            "FROM omnivia_migration_attempts WHERE version = ? ORDER BY started_at",
            (version,),
        ).fetchall()
    ]


def columns_of(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def foreign_keys_of(
    connection: sqlite3.Connection, table: str
) -> set[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """The declared foreign keys of one table, reassembled from `foreign_key_list`.

    The pragma emits one row per column with a per-key `id` and a `seq` within it, so
    a composite key only exists once its columns are grouped back together in `seq`
    order -- which is also the only way "this key names the workspace" can be asked.
    """
    grouped: dict[int, list[tuple[int, str, str, str]]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
        grouped.setdefault(int(row[0]), []).append(
            (int(row[1]), str(row[2]), str(row[3]), str(row[4]))
        )
    keys = set()
    for parts in grouped.values():
        ordered = sorted(parts)
        keys.add(
            (
                ordered[0][1],
                tuple(child for _seq, _parent, child, _target in ordered),
                tuple(target for _seq, _parent, _child, target in ordered),
            )
        )
    return keys


def legacy_table_names() -> frozenset[str]:
    """The fourteen frozen Phase 0 tables, derived from the artifact itself."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(phase0_baseline_sql())
        return frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )
    finally:
        connection.close()


def populate_legacy_corpus(path: Path) -> None:
    """A small deterministic legacy corpus covering the value classes a copy loses.

    Deliberately independent of any other phase's fixture: this file must be able to
    state on its own what "legacy values are unchanged" means, and importing another
    phase's conftest to say it would make the claim depend on a fixture this slice
    does not own.
    """
    awkward = (
        "",
        "plain ascii",
        "unicode ☃ \U0001f30d émoji",
        "quote ' double \" semi ; backslash \\",
        "x" * 4096,
    )
    now = "2026-08-01T00:00:00+00:00"
    connection = sqlite3.connect(str(path))
    try:
        with connection:
            connection.execute(
                "INSERT INTO workspaces (id, name, root_path, storage_path, "
                "description, index_status, settings_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, 'indexed', '{\"b\":2,\"a\":1}', ?, ?)",
                (WORKSPACE_ID, "M2 corpus", "/root", "/storage", now, now),
            )
            for index, content in enumerate(awkward):
                connection.execute(
                    "INSERT INTO memories (id, workspace_id, content, source_type, "
                    "source_reference, source_description, lifecycle_state, "
                    "memory_type, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'note', 'ref', ?, 'approved', 'general', "
                    "'me', ?, ?)",
                    (
                        f"mem-{index:03d}",
                        WORKSPACE_ID,
                        content,
                        None if index % 2 else "described",
                        now,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO sources (id, workspace_id, file_path, extension, "
                "size_bytes, modified_time, file_type, content_hash, parse_status, "
                "error_message, created_at, updated_at) "
                "VALUES ('src-0', ?, '/doc.md', '.md', 1024, ?, 'markdown', "
                "'0000', 'parsed', NULL, ?, ?)",
                (WORKSPACE_ID, now, now, now),
            )
            for index, content in enumerate(awkward[:3]):
                connection.execute(
                    "INSERT INTO chunks (id, source_id, chunk_index, content, "
                    "start_offset, end_offset, content_hash) "
                    "VALUES (?, 'src-0', ?, ?, ?, ?, NULL)",
                    (f"chk-{index}", index, content, index * 100, index * 100 + 99),
                )
            connection.execute(
                "INSERT INTO graph_entities (id, name, entity_type, source_id, "
                "approval_status, created_at, updated_at) "
                "VALUES ('ent-0', ?, 'concept', 'src-0', 'approved', ?, ?)",
                (awkward[2], now, now),
            )
    finally:
        connection.close()


def legacy_inventory(path: Path) -> dict[str, tuple[tuple[str, ...], int, str]]:
    """Columns, row count and content digest for every legacy table."""
    legacy = legacy_table_names()
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        inventory = capture_inventory(connection)
    finally:
        connection.close()
    return {
        entry.name: (entry.columns, entry.row_count, entry.content_checksum)
        for entry in inventory.tables
        if entry.name in legacy
    }


def replay_without_m2_triggers() -> sqlite3.Connection:
    """The whole canonical schema minus `0008`'s twenty-seven triggers.

    In a real workspace the INSERT trigger pins every row to the singleton workspace
    and refuses a sequence that does not advance, so those refusals arrive before the
    declared constraints are ever consulted -- which would leave the claim that the
    foreign keys, the UNIQUE stream indexes and the sequence CHECKs forbid the same
    things untested. Here they are the only thing standing. Everything before `0008`
    is replayed in full so the schema stays coherent; those tables are never written.
    """
    connection = sqlite3.connect(":memory:")
    connection.executescript(phase0_baseline_sql())
    for migration in load_migrations():
        if migration.version < MIGRATION_VERSION:
            connection.executescript(migration.sql)
    # The inherited guards go too, and for a reason that is not about this slice:
    # every one of them calls `omnivia_service_writer()`, which is connection-local
    # and does not exist on a plain SQLite connection, so leaving them standing would
    # make this harness refuse the seed for the single reason it is not here to prove.
    for name in sorted(object_names(connection, "trigger")):
        connection.execute(f"DROP TRIGGER {name}")
    for statement in ddl_only_statements():
        connection.execute(statement)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def seed_chain_without_triggers(connection: sqlite3.Connection) -> None:
    for job in JOB_ROWS:
        insert(connection, DURABLE_JOBS, dict(job))
    for table in M2_TABLES:
        insert(connection, table, row_for(table))


def sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def insert_sql(table: str, values: dict[str, object]) -> str:
    columns = ", ".join(values)
    literals = ", ".join(sql_literal(value) for value in values.values())
    return f"INSERT INTO {table} ({columns}) VALUES ({literals})"


def run_child(source: str, *args: str, drop_pythonpath: bool = False) -> dict[str, Any]:
    """Run a script in a real OS process and return its single structured line."""
    environment = dict(os.environ)
    if drop_pythonpath:
        # A child that must import no OmniVia code is given no way to find it.
        environment.pop("PYTHONPATH", None)
    else:
        repo = Path(__file__).resolve().parents[5]
        environment["PYTHONPATH"] = os.pathsep.join(
            str(repo / part)
            for part in (
                "src",
                "services/omnivia-memory/src",
                "packages/omnivia-core-runtime/src",
            )
        )
    completed = subprocess.run(
        [sys.executable, "-c", source, *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
        check=False,
    )
    for line in reversed(completed.stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            decoded = json.loads(stripped)
            assert isinstance(decoded, dict)
            return decoded
    raise AssertionError(
        f"child emitted no result: {completed.stdout}{completed.stderr}"
    )


#: A stock SQLite client. It imports no OmniVia code and reports which OmniVia
#: modules it ended up with, so "imports none" is verified rather than assumed.
STOCK_SQLITE_CHILD = """
import json, sqlite3, sys

path, statement = sys.argv[1], sys.argv[2]
result = {"statement": statement}
try:
    connection = sqlite3.connect(path, timeout=0.5)
    try:
        connection.execute(statement)
        connection.commit()
        result["succeeded"] = True
    finally:
        connection.close()
except Exception as exc:
    result["succeeded"] = False
    result["error"] = str(exc)
result["omnivia_modules"] = sorted(n for n in sys.modules if n.startswith("omnivia"))
sys.stdout.write(json.dumps(result, sort_keys=True) + chr(10))
"""

#: A genuinely fresh process that resumes migration on a workspace an interrupted
#: run left behind.
RETRY_CHILD = """
import json, sys
from omnivia_core_runtime.storage import migrations as migrations_module
from omnivia_core_runtime.storage.connection import (
    OpenMode, foreign_key_check, integrity_check, open_database,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations, apply_pending_migrations, read_workspace_state,
)

path, workspace_id, service = sys.argv[1], sys.argv[2], sys.argv[3]
result = {}
original_load_migrations = migrations_module.load_migrations
accepted_through_m2 = tuple(
    migration for migration in original_load_migrations() if migration.version <= 8
)
migrations_module.load_migrations = lambda: accepted_through_m2
connection = open_database(
    __import__("pathlib").Path(path), OpenMode.EXCLUSIVE_MAINTENANCE
)
try:
    state = read_workspace_state(connection)
    applied = apply_pending_migrations(
        connection,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=service,
        fencing_generation=state.fencing_generation,
        workspace_id=workspace_id,
    )
    result["applied"] = [migration.version for migration in applied]
    result["ledger"] = sorted(applied_migrations(connection))
    result["ledger_rows_for_8"] = connection.execute(
        "SELECT COUNT(*) FROM omnivia_schema_migrations WHERE version = 8"
    ).fetchone()[0]
    result["tables"] = sorted(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    )
    result["objects"] = sorted(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    )
    result["integrity"] = integrity_check(connection)
    result["foreign_keys"] = foreign_key_check(connection)
    result["ok"] = True
except Exception as exc:
    result["ok"] = False
    result["error"] = str(exc)
finally:
    connection.close()
    migrations_module.load_migrations = original_load_migrations
sys.stdout.write(json.dumps(result, sort_keys=True) + chr(10))
"""


class MigrationInterrupted(RuntimeError):
    """Injected failure, distinguishable from any refusal the runtime itself raises."""


class FailAfterStatement:
    """Forwarding proxy that raises once the Nth statement of `0008` has executed.

    A proxy rather than a monkeypatch because `sqlite3.Connection.execute` is a
    read-only attribute, and everything except `execute` is delegated so the code
    under test runs against genuine SQLite semantics -- which is the whole point when
    the property being proved is transactional rollback.

    The injection is matched on statement *text* rather than on a call count. A count
    would be pinned to however many statements the migrator happens to run around the
    migration, so it would drift the moment that changed; matching text keeps
    "after the Nth statement of 0008" meaning exactly that.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        statements: tuple[str, ...],
        stop_after: int,
    ) -> None:
        self._connection = connection
        self._targets = {" ".join(statement.split()) for statement in statements}
        self._stop_after = stop_after
        self.executed = 0

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        cursor = self._connection.execute(sql, *args)
        if " ".join(sql.split()) in self._targets:
            self.executed += 1
            if self.executed == self._stop_after:
                raise MigrationInterrupted(
                    f"interrupted after 0008 statement {self._stop_after}"
                )
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _apply_through_predecessor(path: Path) -> int:
    """Bootstrap and migrate up to the accepted predecessor only.

    `apply_pending_migrations` applies everything outstanding, so reaching the state
    that existed *before* this slice means narrowing what `load_migrations` offers.
    Narrowed at the module attribute the migrator actually reads, so both halves of
    the test still run through the real migrator rather than a hand-rolled stand-in.
    """
    original = migrations_module.load_migrations
    trimmed = tuple(m for m in original() if m.version < MIGRATION_VERSION)

    migrations_module.load_migrations = lambda: trimmed  # type: ignore[assignment]
    try:
        connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = bootstrap_generation_one(
                connection,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=True,
                service_instance_id=SERVICE_INSTANCE,
            )
            apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
            assert max(applied_migrations(connection)) == MIGRATION_VERSION - 1
            return state.fencing_generation
        finally:
            connection.close()
    finally:
        migrations_module.load_migrations = original  # type: ignore[assignment]
        # The canonical oracles memoise what `load_migrations` returned; a value
        # computed while it was narrowed would describe a schema without 0008, and
        # `guarded_tables` derives from those, so a stale entry there would quietly
        # stop requiring the M2 guards.
        canonical_schema_tables.cache_clear()
        canonical_schema_fingerprint.cache_clear()
        guarded_tables.cache_clear()


@contextmanager
def migration_catalogue_through(version: int) -> Iterator[None]:
    """Expose the real migrator to one accepted historical prefix only."""
    original = migrations_module.load_migrations
    trimmed = tuple(m for m in original() if m.version <= version)
    migrations_module.load_migrations = lambda: trimmed  # type: ignore[assignment]
    try:
        yield
    finally:
        migrations_module.load_migrations = original  # type: ignore[assignment]
        canonical_schema_tables.cache_clear()
        canonical_schema_fingerprint.cache_clear()
        guarded_tables.cache_clear()


# --- M2-01 … M2-08: migration identity, ledger and fingerprint ----------------


def test_m2_01_0008_is_the_unique_consecutive_successor_to_0007() -> None:
    """M2-01: one file, one version, immediately after the accepted predecessor."""
    ordered = load_migrations()
    versions = [migration.version for migration in ordered]
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1)), (
        versions
    )

    eights = [m for m in ordered if m.version == MIGRATION_VERSION]
    assert len(eights) == 1, [m.name for m in eights]
    assert eights[0].name == MIGRATION_NAME
    assert ordered[MIGRATION_VERSION - 1] is eights[0]
    assert ordered[MIGRATION_VERSION - 2].name == PREDECESSOR_NAME
    assert ordered[MIGRATION_VERSION - 2].version == MIGRATION_VERSION - 1


def test_m2_02_content_checksum_and_ledger_metadata_agree(migrated: Path) -> None:
    """M2-02: the ledger records *this* 0008, under the authority that applied it."""
    expected = hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest()
    assert MIGRATION.checksum == expected
    assert len(expected) == 64

    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        row = connection.execute(
            "SELECT name, checksum, applied_by_service_instance, fencing_generation, "
            "applied_at FROM omnivia_schema_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        ).fetchone()
        assert row is not None
        assert row[0] == MIGRATION_NAME
        assert row[1] == expected
        assert row[2] == SERVICE_INSTANCE
        assert int(row[3]) == GENERATION_ONE
        assert row[4]

        attempts = migration_attempts(connection)
        assert [row[0] for row in attempts] == ["succeeded"], attempts
        _outcome, started_at, finished_at, _detail = attempts[0]
        assert started_at
        assert finished_at
    finally:
        connection.close()


def test_m2_03_unchanged_reapply_is_a_no_op(migrated: Path) -> None:
    """M2-03: applying an already-applied 0008 does nothing at all."""
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        before_ledger = applied_migrations(connection)
        before_fingerprint = fingerprint_schema(connection)
        before_attempts = count(connection, "omnivia_migration_attempts")

        applied = apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=SERVICE_INSTANCE,
            fencing_generation=GENERATION_ONE,
            workspace_id=WORKSPACE_ID,
        )

        assert applied == []
        assert applied_migrations(connection) == before_ledger
        assert fingerprint_schema(connection) == before_fingerprint
        assert count(connection, "omnivia_migration_attempts") == before_attempts
    finally:
        connection.close()


def test_m2_04_ledger_replay_refuses_drift_in_the_recorded_checksum(
    migrated: Path,
) -> None:
    """M2-04: "version 8 is applied" has to mean *this* version 8."""
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        connection.execute(
            "UPDATE omnivia_schema_migrations SET checksum = ? WHERE version = ?",
            ("0" * 64, MIGRATION_VERSION),
        )
        connection.commit()
        with pytest.raises(StorageError, match="has changed since it was applied"):
            apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=SERVICE_INSTANCE,
                fencing_generation=GENERATION_ONE,
                workspace_id=WORKSPACE_ID,
            )
    finally:
        connection.close()


def test_m2_05_every_accepted_migration_is_byte_for_byte_unchanged() -> None:
    """M2-05: 0008 is additive; every accepted 0000-0007 SHA-256 is unchanged."""
    package = migrations_module.resources.files(migrations_module.MIGRATION_PACKAGE)
    for name, expected in ACCEPTED_MIGRATION_CHECKSUMS.items():
        text = package.joinpath(name).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == expected, name

    on_disk = {
        entry.name
        for entry in package.iterdir()
        if entry.name.endswith(".sql")  # type: ignore[attr-defined]
    }
    accepted_through_m2 = set(ACCEPTED_MIGRATION_CHECKSUMS) | {MIGRATION_NAME}
    assert accepted_through_m2 <= on_disk
    assert PHASE0_BASELINE_FILE in ACCEPTED_MIGRATION_CHECKSUMS
    # The accepted corpus is exactly 0000 … 0007, so "consecutive successor" is a
    # claim about a numbered series and not about whichever files happen to be here.
    assert sorted(ACCEPTED_MIGRATION_CHECKSUMS) == [
        name for name in sorted(on_disk) if name < MIGRATION_NAME
    ]


def test_m2_06_canonical_schema_carries_every_m2_object_and_no_other(
    migrated: Path,
) -> None:
    """M2-06: nine tables, nineteen named indexes, twenty-seven triggers, exactly.

    The deltas are computed by replaying the artifacts with and without `0008` rather
    than by hard-coding totals, so this stays a statement about what the migration
    adds instead of about how large the schema happens to be. The names are then
    matched against what the file itself creates, so the constants above cannot drift
    away from the migration while still describing a schema that exists.
    """
    assert set(M2_TABLES) <= canonical_schema_tables()
    assert len(M2_TABLES) == 9
    assert len(M2_INDEXES) == 19
    assert len(M2_TRIGGERS) == 27
    assert len(set(M2_INDEXES)) == len(M2_INDEXES)

    declared = re.findall(
        r"CREATE (?:UNIQUE )?(TABLE|INDEX|TRIGGER) IF NOT EXISTS (\w+)", MIGRATION.sql
    )
    by_kind: dict[str, set[str]] = {}
    for kind, name in declared:
        by_kind.setdefault(kind, set()).add(name)
    assert by_kind == {
        "TABLE": set(M2_TABLES),
        "INDEX": set(M2_INDEXES),
        "TRIGGER": set(M2_TRIGGERS),
    }

    without = sqlite3.connect(":memory:")
    with_eight = sqlite3.connect(":memory:")
    try:
        for connection in (without, with_eight):
            connection.executescript(phase0_baseline_sql())
        for migration in load_migrations():
            if migration.version < MIGRATION_VERSION:
                without.executescript(migration.sql)
            if migration.version <= MIGRATION_VERSION:
                with_eight.executescript(migration.sql)

        before = fingerprint_schema(without)
        after = fingerprint_schema(with_eight)
        assert after.tables - before.tables == len(M2_TABLES)
        assert after.indexes - before.indexes == len(M2_INDEXES)
        assert after.triggers - before.triggers == len(M2_TRIGGERS)
        assert object_names(with_eight, "view") == object_names(without, "view")
        assert after.digest != before.digest
    finally:
        without.close()
        with_eight.close()

    canonical = canonical_schema_fingerprint()
    live = open_database(migrated, OpenMode.READ_ONLY)
    try:
        assert fingerprint_schema(live).matches(canonical)
        assert set(M2_TABLES) <= object_names(live, "table")
        assert set(M2_INDEXES) <= object_names(live, "index")
        assert set(M2_TRIGGERS) <= object_names(live, "trigger")
        # No virtual table: the slice adds ordinary storage, not virtual storage.
        assert not [
            name for name in object_names(live, "table") if name.startswith("sqlite_")
        ]
    finally:
        live.close()


@pytest.mark.parametrize(
    ("kind", "name"),
    [("table", RECORDS), ("index", M2_INDEXES[1]), ("trigger", M2_TRIGGERS[0])],
)
def test_m2_07_live_drift_in_any_m2_object_fails(
    migrated: Path, kind: str, name: str
) -> None:
    """M2-07: dropping any one of them is detected before writable readiness."""
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        expected = canonical_schema_fingerprint()
        assert verify_fingerprint(connection, expected).matches(expected)
        assert_guards_intact(connection)

        connection.execute(f"DROP {kind.upper()} {name}")
        connection.commit()

        with pytest.raises(SchemaDrift, match="fingerprint differs"):
            verify_fingerprint(connection, expected)
        if kind == "trigger":
            with pytest.raises(SchemaDrift, match="guard triggers are missing"):
                assert_guards_intact(connection)
    finally:
        connection.close()


def test_m2_08_user_version_mirrors_eight_but_is_not_authority(
    tmp_path: Path,
) -> None:
    """M2-08: the ledger decides; the pragma is a diagnostic mirror of it."""
    path = tmp_path / "m2-only.sqlite"
    materialise_phase0_baseline(path)
    generation = _apply_through_predecessor(path)
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        with migration_catalogue_through(MIGRATION_VERSION):
            applied = apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=SERVICE_INSTANCE,
                fencing_generation=generation,
                workspace_id=WORKSPACE_ID,
            )
        assert [migration.version for migration in applied] == [MIGRATION_VERSION]
        ledger = applied_migrations(connection)
        assert max(ledger) == MIGRATION_VERSION
        version = connection.execute("PRAGMA user_version").fetchone()
        assert version is not None and int(version[0]) == MIGRATION_VERSION

        connection.execute("PRAGMA user_version = 999")
        assert applied_migrations(connection) == ledger
        connection.execute("PRAGMA user_version = 0")
        assert applied_migrations(connection) == ledger
        assert max(applied_migrations(connection)) == MIGRATION_VERSION
    finally:
        connection.close()


# --- M2-09 … M2-14: bootstrap, adoption, convergence and legacy preservation ---


def test_m2_09_pristine_bootstrap_reaches_0008(tmp_path: Path) -> None:
    """M2-09: a workspace that never had a legacy database migrates cleanly."""
    path = tmp_path / "pristine.sqlite"
    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        state = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
            service_instance_id=SERVICE_INSTANCE,
        )
        assert state.baseline_state == BASELINE_PRISTINE
        applied = apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
        assert MIGRATION_VERSION in [migration.version for migration in applied]
        assert set(M2_TABLES) <= object_names(connection, "table")
        assert set(M2_INDEXES) <= object_names(connection, "index")
        assert set(M2_TRIGGERS) <= object_names(connection, "trigger")
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_m2_10_pristine_and_exact_phase0_adoption_converge_on_one_m2_schema(
    tmp_path: Path,
) -> None:
    """M2-10: two different histories, byte-identical M2 objects afterwards.

    What must converge is this slice: the same nine tables, nineteen indexes and
    twenty-seven triggers, with the same stored SQL text, reached from either starting
    point. The two workspaces differ in their *rows*, not in their shape -- a pristine
    bootstrap materialises the fourteen frozen Phase 0 tables as empty compatibility
    scaffolding, which is what lets every migration from `0002` onward run against
    either -- so the legacy corpus is the thing that tells them apart.
    """
    pristine = tmp_path / "pristine.sqlite"
    connection = open_database(pristine, OpenMode.EPHEMERAL)
    try:
        state = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
            service_instance_id=SERVICE_INSTANCE,
        )
        apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
    finally:
        connection.close()

    adopted = tmp_path / "adopted.sqlite"
    materialise_phase0_baseline(adopted)
    populate_legacy_corpus(adopted)
    bootstrap_and_migrate(adopted)

    m2_objects = set(M2_TABLES) | set(M2_INDEXES) | set(M2_TRIGGERS)
    schemas: list[dict[str, str]] = []
    for path, baseline in ((pristine, BASELINE_PRISTINE), (adopted, BASELINE_ADOPTED)):
        published = open_database(path, OpenMode.READ_ONLY)
        try:
            state_row = read_workspace_state(published)
            assert state_row is not None
            assert state_row.baseline_state == baseline
            assert state_row.workspace_id == WORKSPACE_ID
            assert MIGRATION_VERSION in applied_migrations(published)
            assert integrity_check(published) == []
            assert foreign_key_check(published) == []
            assert m2_objects <= (
                object_names(published, "table")
                | object_names(published, "index")
                | object_names(published, "trigger")
            )
            schemas.append(
                {name: object_sql(published, name) for name in sorted(m2_objects)}
            )
        finally:
            published.close()

    assert schemas[0] == schemas[1]
    # Both carry the frozen legacy *shape*; only the adopted one carries its rows.
    assert legacy_table_names() <= set(table_names_of(adopted))
    assert legacy_table_names() <= set(table_names_of(pristine))
    assert sum(entry[1] for entry in legacy_inventory(adopted).values()) > 0
    assert sum(entry[1] for entry in legacy_inventory(pristine).values()) == 0


def table_names_of(path: Path) -> tuple[str, ...]:
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        return table_names(connection)
    finally:
        connection.close()


def test_m2_11_the_full_legacy_inventory_is_unchanged_across_0008(
    tmp_path: Path,
) -> None:
    """M2-11: every legacy table, column, row count and value survives verbatim.

    Measured either side of `0008` alone rather than either side of the whole
    migration series, so this is evidence about *this* migration.
    """
    path = tmp_path / "adopted.sqlite"
    materialise_phase0_baseline(path)
    populate_legacy_corpus(path)

    generation = _apply_through_predecessor(path)
    before = legacy_inventory(path)
    assert len(before) == 14, sorted(before)
    assert sum(entry[1] for entry in before.values()) > 8

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        with migration_catalogue_through(MIGRATION_VERSION):
            applied = apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=SERVICE_INSTANCE,
                fencing_generation=generation,
                workspace_id=WORKSPACE_ID,
            )
    finally:
        connection.close()
    assert [migration.version for migration in applied] == [MIGRATION_VERSION]

    after = legacy_inventory(path)
    assert after == before
    # And the empty M2 tables did not smuggle a row in alongside them.
    published = open_database(path, OpenMode.READ_ONLY)
    try:
        assert counts(published) == dict.fromkeys(M2_TABLES, 0)
    finally:
        published.close()


def test_m2_12_populated_non_phase0_adoption_is_refused_with_no_m2_objects(
    tmp_path: Path,
) -> None:
    """M2-12: an unrecognised database is not adopted, and gains nothing."""
    path = tmp_path / "unknown.sqlite"
    materialise_phase0_baseline(path)
    stray = sqlite3.connect(str(path))
    try:
        stray.execute("CREATE TABLE interloper (id TEXT PRIMARY KEY)")
        stray.commit()
    finally:
        stray.close()

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        with pytest.raises(StorageError, match="not the frozen"):
            bootstrap_generation_one(
                connection,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=True,
                service_instance_id=SERVICE_INSTANCE,
            )
        with pytest.raises(SchemaCreationRefused, match="requires an empty database"):
            bootstrap_generation_one(
                connection,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=False,
                service_instance_id=SERVICE_INSTANCE,
            )
        assert object_names(connection, "table").isdisjoint(M2_TABLES)
        assert object_names(connection, "index").isdisjoint(M2_INDEXES)
        assert object_names(connection, "trigger").isdisjoint(M2_TRIGGERS)
    finally:
        connection.close()


#: Column names a mutable current-state flag would arrive under. `0008` has none:
#: the current interpretation is derived from an append stream, so there is nothing
#: for a later writer to rewrite. `tombstoned_observation` is deliberately not here --
#: it is what one provenance event *observed*, not a flag that event flips.
REWRITABLE_FLAG_COLUMNS = frozenset(
    {
        "current",
        "deleted",
        "is_current",
        "is_deleted",
        "is_truth",
        "superseded",
        "tombstoned",
        "truth",
    }
)

#: Column names a physical blob store would arrive under. A path is not part of a
#: content identity, and a schema carrying one would be quietly asserting the bytes
#: are still there.
PHYSICAL_STORE_COLUMNS = frozenset(
    {"blob_path", "file_path", "location", "path", "storage_path", "uri", "url"}
)


def test_m2_13_the_migration_is_additive_ddl_in_its_own_reserved_scope(
    migrated: Path,
) -> None:
    """M2-13: DDL over `omnivia_` objects, one named inherited guard, nothing else.

    Proved structurally rather than by observation, and against the statements the
    migrator actually executes rather than against the file's prose. With no INSERT,
    UPDATE, DELETE, ALTER, view, virtual table or PRAGMA in any of them, `0008`
    *cannot* bump a workspace format version, rewrite a legacy row or promote legacy
    knowledge, whatever a later reader assumes about it. The single DROP it carries is
    held to exactly what the file documents: one `DROP TRIGGER` naming the inherited
    durable-jobs guard that the very next statement recreates.

    The accepted `0000`-`0007` checksums are then re-read from the live ledger, not
    from disk as M2-05 does: what must be true after this slice applies is that the
    workspace still records the same accepted history, byte for byte.
    """
    for statement in MIGRATION_STATEMENTS:
        normalised = " ".join(statement.split()).upper()
        assert normalised.startswith(
            (*DDL_ONLY_PREFIXES, "CREATE TRIGGER", "DROP TRIGGER IF EXISTS")
        ), normalised[:80]

    assert re.findall(
        r"\bDROP\s+\w+\s+IF EXISTS\s+(\w+)", MIGRATION_EXECUTABLE_SQL
    ) == [INHERITED_JOB_GUARD]
    forbidden = (
        r"\bINSERT\s+INTO\b",
        r"\bUPDATE\s+\w+\s+SET\b",
        r"\bDELETE\s+FROM\b",
        r"\bALTER\s+TABLE\b",
        r"\bDROP\s+(TABLE|INDEX|VIEW)\b",
        r"\bCREATE\s+VIEW\b",
        r"\bCREATE\s+VIRTUAL\s+TABLE\b",
        r"\bPRAGMA\b",
        r"\bDEFERRABLE\b",
        r"\bON\s+(DELETE|UPDATE)\s+(CASCADE|SET\s+NULL|SET\s+DEFAULT)\b",
    )
    for pattern in forbidden:
        assert re.search(pattern, MIGRATION_EXECUTABLE_SQL, re.IGNORECASE) is None, (
            pattern
        )

    # It never names a legacy table, so it cannot alter or promote legacy knowledge.
    for legacy in legacy_table_names():
        assert (
            re.search(rf"\b{re.escape(legacy)}\b", MIGRATION_EXECUTABLE_SQL) is None
        ), legacy

    created = re.findall(
        r"CREATE (?:UNIQUE )?(?:TABLE|INDEX|TRIGGER) IF NOT EXISTS (\w+)",
        MIGRATION_EXECUTABLE_SQL,
    )
    assert len(created) == len(MIGRATION_STATEMENTS) - 2
    assert all(name.startswith("omnivia_") for name in created), created

    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        assert state.workspace_format_version == "1"
        assert state.fencing_generation == GENERATION_ONE

        # No column a physical blob store or a rewritable current-state flag could be
        # written into, by having no column of that name at all.
        for table in M2_TABLES:
            columns = set(columns_of(connection, table))
            assert columns.isdisjoint(REWRITABLE_FLAG_COLUMNS), table
            assert columns.isdisjoint(PHYSICAL_STORE_COLUMNS), table

        recorded = {
            int(row[0]): (str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT version, name, checksum FROM omnivia_schema_migrations "
                "WHERE version < ?",
                (MIGRATION_VERSION,),
            )
        }
    finally:
        connection.close()

    assert sorted(recorded) == list(range(1, MIGRATION_VERSION))
    for name, checksum in recorded.values():
        assert ACCEPTED_MIGRATION_CHECKSUMS[name] == checksum, name


def test_m2_14_integrity_and_foreign_keys_are_clean_after_migration(
    owned: Owned,
) -> None:
    """M2-14: a migrated workspace is healthy, and stays so once M2 rows exist."""
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []
    seed_chain(owned)
    assert counts(owned.connection) == dict.fromkeys(M2_TABLES, 1)
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


# --- M2-15: exact staged ancestry ------------------------------------------------


def test_m2_15_the_ancestry_predicate_checks_exact_source_kind_and_blob_digest(
    owned: Owned,
) -> None:
    """M2-15: naming the ref is not enough -- the source_kind and blob digest an
    evidence row claims must both be the ones the staging actually verified.

    `stg-0001` verified `filesystem.archive` against `DIGEST_A`. A row naming that
    ref while claiming a different, independently valid blob, or the right blob
    under a different source_kind, is refused by the same trigger clause; only the
    exact triple -- ref, source_kind and blob digest all agreeing with what staging
    recorded -- is accepted.
    """
    seed_chain(owned)
    write(owned, BLOBS, content_digest=DIGEST_B)
    before = counts(owned.connection)
    ancestry_refusal = (
        "must name a verified staged source in this workspace with the same "
        "source_kind and blob content digest"
    )

    with pytest.raises(sqlite3.DatabaseError, match=ancestry_refusal):
        write(
            owned,
            EVIDENCE,
            evidence_id="evd-wrong-blob",
            source_native_id="doc-wrong-blob",
            blob_content_digest=DIGEST_B,
        )
    assert counts(owned.connection) == before

    with pytest.raises(sqlite3.DatabaseError, match=ancestry_refusal):
        write(
            owned,
            EVIDENCE,
            evidence_id="evd-wrong-kind",
            source_native_id="doc-wrong-kind",
            source_kind="email.message",
        )
    assert counts(owned.connection) == before

    write(
        owned,
        EVIDENCE,
        evidence_id="evd-exact-match",
        source_native_id="doc-exact-match",
    )
    assert count(owned.connection, EVIDENCE) == before[EVIDENCE] + 1


# --- M2-16: the reopened durable-jobs guard is exact -----------------------------


def test_m2_16_the_reopened_durable_jobs_guard_forbids_retagging_a_referenced_job_and_nothing_else(
    owned: Owned,
) -> None:
    """M2-16: the one thing `0008` narrows about `omnivia_durable_jobs` is a job's
    *type* going stale under evidence that already named it. State, payload and an
    unreferenced job's type are all exactly as writable as before, and a writer with
    no authority at all is still refused for the reason it always was.
    """
    seed_chain(owned)
    write(owned, EVIDENCE, **UNIQUE_IDS[EVIDENCE], import_run_id=IMPORT_JOB_ID)
    assert count(owned.connection, EVIDENCE) == 2

    def job_row(job_id: str) -> tuple[str, str]:
        row = owned.connection.execute(
            "SELECT job_type, state FROM omnivia_durable_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert row is not None
        return str(row[0]), str(row[1])

    with (
        pytest.raises(
            sqlite3.DatabaseError,
            match="job_type may not change while evidence references this job",
        ),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET job_type = 'maintenance.compaction' "
            "WHERE job_id = ?",
            (IMPORT_JOB_ID,),
        )
    assert job_row(IMPORT_JOB_ID) == ("ingestion.import", "queued")

    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET state = 'running', "
            "updated_at = '2026-08-01T00:05:00+00:00' WHERE job_id = ?",
            (IMPORT_JOB_ID,),
        )
    assert job_row(IMPORT_JOB_ID) == ("ingestion.import", "running")

    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET job_type = 'maintenance.archival' "
            "WHERE job_id = ?",
            (OTHER_JOB_ID,),
        )
    assert job_row(OTHER_JOB_ID)[0] == "maintenance.archival"

    close_guard(owned.connection)
    with (
        pytest.raises(sqlite3.DatabaseError, match=REFUSED_EXTERNAL_WRITE),
        owned.connection,
    ):
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET state = 'running' WHERE job_id = ?",
            (OTHER_JOB_ID,),
        )


# --- M2-17: the four frozen public domains, exactly ------------------------------


#: (case id, table, overrides, expected error substring). Every case runs under a
#: current lease and an open guard, via `seed_chain`, so the insert clears every
#: trigger and it is the declared CHECK -- not the fencing layer -- that says no.
#: Cases against a table whose INSERT trigger also enforces a strictly-advancing
#: sequence (`omnivia_evidence_permission_labels`, `omnivia_evidence_provenance_events`)
#: carry `UNIQUE_IDS` so that trigger clears too, and the CHECK under test is the one
#: actually reached.
DOMAIN_NEGATIVE_CASES: list[tuple[str, str, dict[str, object], str]] = [
    # EvidenceChecksum: `[a-z][a-z0-9_]*` ':' `[A-Za-z0-9+/=_-]+`, <= 256, exactly
    # one colon -- omnivia_evidence_artifacts.content_checksum.
    (
        "checksum_uppercase_algorithm",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "content_checksum": "sHA:abc"},
        "CHECK constraint failed",
    ),
    (
        "checksum_two_colons",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "content_checksum": "sha256:abc:def"},
        "CHECK constraint failed",
    ),
    (
        "checksum_algorithm_hyphen",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "content_checksum": "sha-256:abc"},
        "CHECK constraint failed",
    ),
    (
        "checksum_empty_digest",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "content_checksum": "sha256:"},
        "CHECK constraint failed",
    ),
    (
        "checksum_bang_digest",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "content_checksum": "sha256:abc!"},
        "CHECK constraint failed",
    ),
    # MediaType: type '/' subtype, exactly one slash, each half well-formed --
    # omnivia_evidence_artifacts.media_type.
    (
        "media_type_two_slashes",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "media_type": "text/plain/x"},
        "CHECK constraint failed",
    ),
    (
        "media_type_empty_type",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "media_type": "/plain"},
        "CHECK constraint failed",
    ),
    (
        "media_type_empty_subtype",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "media_type": "text/"},
        "CHECK constraint failed",
    ),
    (
        "media_type_double_slash",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "media_type": "text//plain"},
        "CHECK constraint failed",
    ),
    (
        "media_type_bad_char",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "media_type": "text/pl@in"},
        "CHECK constraint failed",
    ),
    # Identifier: `^[A-Za-z0-9][A-Za-z0-9._:-]*$`, 1..128 -- the provenance actor_id.
    (
        "identifier_leading_punctuation",
        PROVENANCE,
        {**UNIQUE_IDS[PROVENANCE], "actor_id": "-actor"},
        "CHECK constraint failed",
    ),
    (
        "identifier_whitespace",
        PROVENANCE,
        {**UNIQUE_IDS[PROVENANCE], "actor_id": "act or"},
        "CHECK constraint failed",
    ),
    (
        "identifier_unicode",
        PROVENANCE,
        {**UNIQUE_IDS[PROVENANCE], "actor_id": "actör"},
        "CHECK constraint failed",
    ),
    (
        "identifier_overlength",
        PROVENANCE,
        {**UNIQUE_IDS[PROVENANCE], "actor_id": "a" * 129},
        "CHECK constraint failed",
    ),
    # OpenCode: `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`, 1..128 -- the permission
    # label. `group.engineering` is the well-formed shape these all deviate from.
    (
        "opencode_leading_punctuation",
        LABELS,
        {**UNIQUE_IDS[LABELS], "permission_label": ".group.engineering"},
        "CHECK constraint failed",
    ),
    (
        "opencode_whitespace",
        LABELS,
        {**UNIQUE_IDS[LABELS], "permission_label": "group engineering"},
        "CHECK constraint failed",
    ),
    (
        "opencode_unicode",
        LABELS,
        {**UNIQUE_IDS[LABELS], "permission_label": "gröup.engineering"},
        "CHECK constraint failed",
    ),
    (
        "opencode_uppercase",
        LABELS,
        {**UNIQUE_IDS[LABELS], "permission_label": "Group.engineering"},
        "CHECK constraint failed",
    ),
    (
        "opencode_consecutive_dot",
        LABELS,
        {**UNIQUE_IDS[LABELS], "permission_label": "group..engineering"},
        "CHECK constraint failed",
    ),
    (
        "opencode_dot_digit",
        LABELS,
        {**UNIQUE_IDS[LABELS], "permission_label": "group.1ngineering"},
        "CHECK constraint failed",
    ),
    (
        "opencode_trailing_dot",
        LABELS,
        {**UNIQUE_IDS[LABELS], "permission_label": "group.engineering."},
        "CHECK constraint failed",
    ),
    (
        "opencode_overlength",
        LABELS,
        {**UNIQUE_IDS[LABELS], "permission_label": "group." + "e" * 128},
        "CHECK constraint failed",
    ),
    # OpaqueToken: printable ASCII, 1..512 -- staged_source_ref, also its own PK.
    (
        "opaque_whitespace",
        STAGED,
        {"staged_source_ref": "stg with space"},
        "CHECK constraint failed",
    ),
    (
        "opaque_unicode",
        STAGED,
        {"staged_source_ref": "stg-☃"},
        "CHECK constraint failed",
    ),
    (
        "opaque_overlength",
        STAGED,
        {"staged_source_ref": "s" * 513},
        "CHECK constraint failed",
    ),
]


@pytest.mark.parametrize(
    ("table", "overrides", "expected"),
    [
        (table, overrides, expected)
        for _case, table, overrides, expected in DOMAIN_NEGATIVE_CASES
    ],
    ids=[case for case, _table, _overrides, _expected in DOMAIN_NEGATIVE_CASES],
)
def test_m2_17_evidencechecksum_mediatype_identifier_opencode_and_opaquetoken_refuse_every_malformed_shape(
    owned: Owned, table: str, overrides: dict[str, object], expected: str
) -> None:
    """M2-17: every column typed by a frozen public domain refuses the shapes that
    domain's pattern excludes -- for the legitimate owner, which is the strong form.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError), match=expected):
        write(owned, table, **overrides)
    assert counts(owned.connection) == before


def test_m2_17b_evidencechecksum_mediatype_opencode_and_opaquetoken_accept_their_full_domain(
    owned: Owned,
) -> None:
    """M2-17: acceptance is not narrower than the pattern -- a provider-neutral
    checksum, a vendor media type, an unrecognised-but-well-formed open code and a
    printable opaque token drawn from outside the alphanumeric range are all still
    storable, exactly as the frozen public contract admits them.
    """
    seed_chain(owned)
    write(
        owned,
        EVIDENCE,
        **UNIQUE_IDS[EVIDENCE],
        content_checksum=PROVIDER_CHECKSUM,
        media_type="application/vnd.custom+json",
    )
    write(owned, LABELS, **UNIQUE_IDS[LABELS], permission_label="group.engineering")
    write(owned, STAGED, staged_source_ref="tok!$%&'()*+,-./:;<=>?@[]^_`{|}~")

    stored = owned.connection.execute(
        "SELECT content_checksum, media_type FROM omnivia_evidence_artifacts "
        "WHERE evidence_id = ?",
        (UNIQUE_IDS[EVIDENCE]["evidence_id"],),
    ).fetchone()
    assert stored == (PROVIDER_CHECKSUM, "application/vnd.custom+json")
    assert count(owned.connection, LABELS) == 2
    assert count(owned.connection, STAGED) == 2


# --- M2-18: the statement and trigger inventory, exactly -------------------------


def test_m2_18_the_statement_inventory_is_exactly_fifty_seven(migrated: Path) -> None:
    """M2-18: 9 CREATE TABLE + 19 CREATE INDEX + 28 CREATE TRIGGER + 1 DROP TRIGGER.

    The twenty-eighth CREATE TRIGGER is the reopened
    `omnivia_guard_durable_jobs_update`: a statement the migrator executes, and --
    per M2-06 -- not a trigger *name* that enters the schema, which is why it is
    excluded from `M2_TRIGGERS` (27) even though it is counted here.
    """
    normalised = [
        " ".join(statement.split()).upper() for statement in MIGRATION_STATEMENTS
    ]
    assert len(MIGRATION_STATEMENTS) == 57
    assert sum(s.startswith("CREATE TABLE") for s in normalised) == len(M2_TABLES) == 9
    assert (
        sum(s.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")) for s in normalised)
        == len(M2_INDEXES)
        == 19
    )
    assert (
        sum(s.startswith("CREATE TRIGGER") for s in normalised)
        == len(M2_TRIGGERS) + 1
        == 28
    )
    assert sum(s.startswith("DROP TRIGGER") for s in normalised) == 1

    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        live_triggers = object_names(connection, "trigger")
    finally:
        connection.close()
    assert set(M2_TRIGGERS) <= live_triggers
    assert INHERITED_JOB_GUARD in live_triggers
    assert INHERITED_JOB_GUARD not in M2_TRIGGERS


def test_m2_18b_the_reopened_durable_jobs_guard_changes_and_keeps_the_old_predicate() -> (
    None
):
    """M2-18: dropped and recreated, not merely re-declared -- the stored SQL text
    differs from what `0007` left behind, and the difference is additive: the
    complete old authority predicate is reproduced byte for byte inside the new one.

    The comparison is of the schema immediately before `0008` against the schema
    immediately after it, so both catalogues stop there. Running on past `0008`
    would apply later migrations -- including the rebuilds that copy a table
    forward -- against a schema deliberately missing one of their predecessors,
    which validates an incomplete schema rather than this migration's own change.
    """
    without = sqlite3.connect(":memory:")
    with_eight = sqlite3.connect(":memory:")
    try:
        for connection in (without, with_eight):
            connection.executescript(phase0_baseline_sql())
        for migration in load_migrations():
            if migration.version > MIGRATION_VERSION:
                break
            if migration.version != MIGRATION_VERSION:
                without.executescript(migration.sql)
            with_eight.executescript(migration.sql)

        before = object_sql(without, INHERITED_JOB_GUARD)
        after = object_sql(with_eight, INHERITED_JOB_GUARD)
        assert before != after

        start = MIGRATION.sql.index(f"CREATE TRIGGER {INHERITED_JOB_GUARD}")
        when = MIGRATION.sql.index("WHEN omnivia_service_writer()", start)
        end = MIGRATION.sql.index("OR (NEW.job_type", when)
        old_predicate = MIGRATION.sql[when:end].rstrip()

        assert old_predicate in before
        assert old_predicate in after
    finally:
        without.close()
        with_eight.close()


# --- M2-19: UPDATE and DELETE refuse unconditionally, even for the owner --------


@pytest.mark.parametrize("table", M2_TABLES)
@pytest.mark.parametrize("statement", ["UPDATE", "DELETE"])
def test_m2_19_updates_and_deletes_always_fail_including_for_the_owner(
    owned: Owned, table: str, statement: str
) -> None:
    """M2-19: append-only means the fenced owner cannot rewrite history either.

    Both statements are attempted inside the same current, valid `fenced_transaction`
    that a real write would use, so the refusal is the persisted trigger's alone --
    not a missing guard or a stale generation -- and the row it targets is exactly
    the one `seed_chain` just wrote.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    sql = {
        "UPDATE": f"UPDATE {table} SET workspace_id = workspace_id",
        "DELETE": f"DELETE FROM {table}",
    }[statement]

    with (
        pytest.raises(sqlite3.DatabaseError, match="append-only"),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        owned.connection.execute(sql)
    assert counts(owned.connection) == before


# --- M2-20: INSERT needs the exact current authority tuple, per table ----------


#: Every dimension of the tuple `take_ownership` established as current that a
#: table-driven test can flip, one at a time, away from what it actually holds.
AUTHORITY_NEGATIVE_CASES: tuple[str, ...] = (
    "closed_guard",
    "wrong_owner",
    "stale_generation",
    "wrong_workspace",
)

#: The substring each case's refusal carries. A closed guard is refused before
#: authority is re-examined at all; the other three surface from
#: `assert_current_authority` by way of `StaleGeneration`.
AUTHORITY_REFUSAL_MESSAGE: dict[str, str] = {
    "closed_guard": "no mutation guard is open",
    "wrong_owner": "lease belongs to",
    "stale_generation": "stale authority",
    "wrong_workspace": "stale authority",
}


def _wrong_authority(
    case: str, holder: Owned
) -> tuple[ServiceInstanceIdentity, str, int]:
    """The `(identity, workspace_id, fencing_generation)` `case` names, made wrong.

    Everything not named by `case` is left exactly as `take_ownership` established
    it, so each case is wrong in one dimension only.
    """
    if case == "closed_guard":
        close_guard(holder.connection)
        return holder.identity, WORKSPACE_ID, holder.generation
    if case == "wrong_owner":
        return (
            make_identity(instance="svc-m2-impostor"),
            WORKSPACE_ID,
            holder.generation,
        )
    if case == "stale_generation":
        return holder.identity, WORKSPACE_ID, holder.generation + 1
    if case == "wrong_workspace":
        return holder.identity, OTHER_WORKSPACE_ID, holder.generation
    raise AssertionError(case)


@pytest.mark.parametrize("table", M2_TABLES)
@pytest.mark.parametrize("case", AUTHORITY_NEGATIVE_CASES)
def test_m2_20_insert_needs_the_exact_current_authority_tuple(
    owned: Owned, case: str, table: str
) -> None:
    """M2-20: the same row a real write inserts cleanly (M2-14 et al.) is refused
    here -- a closed guard, a different owner, a stale generation and a different
    workspace each refuse it, one dimension away from the tuple `owned` actually
    holds, reusing `seed_chain` and `unique_row_for` rather than a fresh setup.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    identity, workspace_id, generation = _wrong_authority(case, owned)

    with (
        pytest.raises(StaleGeneration, match=AUTHORITY_REFUSAL_MESSAGE[case]),
        fenced_transaction(
            owned.connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=generation,
        ),
    ):
        insert(owned.connection, table, unique_row_for(table))

    assert counts(owned.connection) == before


# --- M2-21: every parent-scoped sequence strictly advances ----------------------


#: Zero and every negative integer are refused before a stream's own maximum is
#: ever consulted: the INSERT trigger's own `COALESCE(MAX(...), 0)` floor means
#: even an empty stream refuses zero, so this needs no history of its own.
NON_POSITIVE_SEQUENCE_VALUES = (0, -1)

SEQUENCE_ADVANCE_REFUSAL = "must advance within its parent stream"

#: The wall-clock column each stream stamps, where one exists. Forcing it equal to
#: the row a duplicate or backwards attempt collides with is what proves the
#: ordering trigger reads the sequence column and never relaxes for a matching
#: timestamp. `omnivia_evidence_event_references` stamps no write-time column of
#: its own, so `reference_ordinal` is its only ordering signal.
SEQUENCE_STREAM_TIMESTAMP_COLUMNS: dict[str, str] = {
    INTEGRITY: "checked_at_us",
    LABELS: "recorded_at_us",
    PROVENANCE: "occurred_at_us",
    RECORDS: "recorded_at_us",
    SPANS: "recorded_at_us",
}


@pytest.mark.parametrize(
    ("table", "sequence_column", "_parent_column"), SEQUENCE_STREAMS
)
def test_m2_21_every_parent_scoped_sequence_strictly_advances(
    owned: Owned, table: str, sequence_column: str, _parent_column: str
) -> None:
    """M2-21: a stream's sequence must exceed its own current maximum -- not
    merely be positive, and not merely newer by the clock. Zero and negative are
    refused outright; a duplicate and a backwards value are refused even when
    forced to share the exact timestamp of the row they collide with, which is
    what proves the ordering trigger reads the sequence column, never the clock.
    """
    seed_chain(owned)
    before = count(owned.connection, table)

    for value in NON_POSITIVE_SEQUENCE_VALUES:
        with pytest.raises(
            (sqlite3.IntegrityError, sqlite3.DatabaseError),
            match=f"CHECK constraint failed|{SEQUENCE_ADVANCE_REFUSAL}",
        ):
            write(owned, table, **{**UNIQUE_IDS[table], sequence_column: value})
        assert count(owned.connection, table) == before

    timestamp_column = SEQUENCE_STREAM_TIMESTAMP_COLUMNS.get(table)
    advanced_timestamp = {timestamp_column: BASE_US + 1_000} if timestamp_column else {}
    write(owned, table, **UNIQUE_IDS[table], **advanced_timestamp)
    assert count(owned.connection, table) == before + 1

    colliding_timestamp = (
        {timestamp_column: advanced_timestamp[timestamp_column]}
        if timestamp_column
        else {}
    )
    for value in (2, 1):  # a duplicate of, then a value behind, the new maximum
        with pytest.raises(sqlite3.DatabaseError, match=SEQUENCE_ADVANCE_REFUSAL):
            write(
                owned,
                table,
                **{**UNIQUE_IDS[table], sequence_column: value},
                **colliding_timestamp,
            )
        assert count(owned.connection, table) == before + 1


# --- M2-22: a failed staged source is durable, and never adoptable ancestry -----

#: The four staging outcomes that are not `verified`. Every one of them satisfies the
#: table's own CHECK -- a failed staging is exactly as durable and inspectable as a
#: successful one -- and every one of them names no blob at all, which is what the
#: evidence ancestry predicate (M2-15) turns into a permanent refusal.
STAGED_FAILURE_OUTCOMES = ("digest_mismatch", "missing_blob", "unsupported", "unsafe")

ANCESTRY_REFUSAL = (
    "must name a verified staged source in this workspace with the same "
    "source_kind and blob content digest"
)


@pytest.mark.parametrize("outcome", STAGED_FAILURE_OUTCOMES)
def test_m2_22_a_failed_staged_source_stays_queryable_and_never_seeds_evidence(
    owned: Owned, outcome: str
) -> None:
    """M2-22: `digest_mismatch`, `missing_blob`, `unsupported` and `unsafe` are all
    valid, durable, inspectable rows -- and none of them can ever become the ancestry
    of an accepted evidence artifact, because none of them names a blob.
    """
    seed_chain(owned)
    ref = f"stg-{outcome}"
    write(
        owned,
        STAGED,
        staged_source_ref=ref,
        staging_outcome=outcome,
        blob_workspace_id=None,
        blob_content_digest=None,
    )
    stored = owned.connection.execute(
        "SELECT staging_outcome, blob_workspace_id, blob_content_digest "
        "FROM omnivia_staged_sources WHERE staged_source_ref = ?",
        (ref,),
    ).fetchone()
    assert stored == (outcome, None, None)

    before = counts(owned.connection)
    with pytest.raises(sqlite3.DatabaseError, match=ANCESTRY_REFUSAL):
        write(
            owned,
            EVIDENCE,
            evidence_id=f"evd-{outcome}",
            source_native_id=f"doc-{outcome}",
            staged_source_ref=ref,
        )
    assert counts(owned.connection) == before
    refused = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_evidence_artifacts WHERE staged_source_ref = ?",
        (ref,),
    ).fetchone()[0]
    assert refused == 0


# --- M2-23 … M2-24: normalized record/span coherence -----------------------------


def _seed_two_evidence_chains(owned: Owned) -> None:
    """`evd-0001`/`nrc-0001` from `seed_chain`, addressing `DIGEST_A`, plus a second,
    independently blob-coherent chain -- `evd-second`/`nrc-second`, addressing the
    distinct `DIGEST_B`. A negative case that substitutes one chain's identity into
    the other is then provably crossing a real fence rather than two values that
    already happened to agree.
    """
    seed_chain(owned)
    write(owned, BLOBS, content_digest=DIGEST_B)
    write(
        owned,
        EVIDENCE,
        evidence_id="evd-second",
        source_native_id="doc-2",
        blob_content_digest=DIGEST_B,
        staged_source_ref=None,
    )
    write(
        owned,
        RECORDS,
        normalized_record_id="nrc-second",
        evidence_id="evd-second",
        evidence_blob_digest=DIGEST_B,
    )


def test_m2_23_a_same_evidence_record_span_chain_with_coherent_parent_and_offsets_succeeds(
    owned: Owned,
) -> None:
    """M2-23: a second record on the same evidence, and a span naming that record as
    its own exact parent with a well-formed, non-negative, non-backwards offset
    pair, both land cleanly.
    """
    seed_chain(owned)
    write(owned, RECORDS, **UNIQUE_IDS[RECORDS])
    write(
        owned,
        SPANS,
        normalized_span_id="nsp-second",
        span_sequence=2,
        normalized_record_id=UNIQUE_IDS[RECORDS]["normalized_record_id"],
        span_start_offset=5,
        span_end_offset=15,
    )
    assert count(owned.connection, RECORDS) == 2
    assert count(owned.connection, SPANS) == 2
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


#: (case id, table, overrides, expected error substring), run against
#: `_seed_two_evidence_chains`. A record or span that mixes fields drawn from the two
#: chains, or a span whose own offset pair runs backwards, is refused -- and refused
#: without inserting anything, since the table and CHECK constraints below are
#: declared, not advisory.
NORMALIZED_COHERENCE_NEGATIVE_CASES: tuple[
    tuple[str, str, dict[str, object], str], ...
] = (
    (
        "cross_evidence_record_substitution",
        RECORDS,
        {
            "normalized_record_id": "nrc-cross",
            "record_sequence": 2,
            "evidence_id": "evd-second",
            "evidence_blob_digest": DIGEST_A,
        },
        "FOREIGN KEY constraint failed",
    ),
    (
        "cross_parent_span_substitution",
        SPANS,
        {
            "normalized_span_id": "nsp-cross",
            "span_sequence": 2,
            "normalized_record_id": "nrc-0001",
            "evidence_id": "evd-second",
        },
        "FOREIGN KEY constraint failed",
    ),
    (
        "incoherent_offset_pair",
        SPANS,
        {
            "normalized_span_id": "nsp-incoherent",
            "span_sequence": 2,
            "span_start_offset": 10,
            "span_end_offset": 0,
        },
        "CHECK constraint failed",
    ),
)


@pytest.mark.parametrize(
    ("table", "overrides", "expected"),
    [
        (table, overrides, expected)
        for _case, table, overrides, expected in NORMALIZED_COHERENCE_NEGATIVE_CASES
    ],
    ids=[
        case
        for case, _table, _overrides, _expected in NORMALIZED_COHERENCE_NEGATIVE_CASES
    ],
)
def test_m2_24_record_and_span_coherence_is_refused_without_partial_rows(
    owned: Owned, table: str, overrides: dict[str, object], expected: str
) -> None:
    """M2-24: a record naming another chain's evidence identity, a span naming
    another chain's evidence as its own record's parent, and a span whose offset
    pair runs backwards are each refused -- and each refusal leaves every table
    exactly as populated as it was before the attempt.
    """
    _seed_two_evidence_chains(owned)
    before = counts(owned.connection)

    with pytest.raises(sqlite3.DatabaseError, match=expected):
        write(owned, table, **overrides)

    assert counts(owned.connection) == before


# --- M2-25: every TEXT column carries an explicit typeof('text') guard ----------


#: The exhaustive, hand-authored map of every TEXT column across all nine tables,
#: paired with whether the column is NOT NULL (required) or nullable (optional).
#: `test_m2_25_...` cross-checks this map against the live schema so it cannot drift
#: silently, and `test_m2_25b_...` reads the migration's own stored SQL to prove each
#: one carries `typeof(<column>) = 'text'` -- the guard that keeps a BLOB out even
#: though TEXT affinity never converts one (only NUMERIC storage classes are ever
#: coerced; BLOB and NULL are always stored as-is), so a column that relied on
#: affinity alone would silently accept one.
TEXT_COLUMNS_BY_TABLE: dict[str, tuple[tuple[str, bool], ...]] = {
    BLOBS: (
        ("workspace_id", True),
        ("content_digest", True),
    ),
    INTEGRITY: (
        ("integrity_event_id", True),
        ("workspace_id", True),
        ("content_digest", True),
        ("outcome", True),
        ("observed_digest", False),
        ("inventory_id", False),
    ),
    STAGED: (
        ("staged_source_ref", True),
        ("workspace_id", True),
        ("source_kind", True),
        ("declared_checksum", True),
        ("media_type", True),
        ("source_version", False),
        ("computed_checksum", False),
        ("original_metadata_json", True),
        ("original_metadata_digest", True),
        ("staging_outcome", True),
        ("blob_workspace_id", False),
        ("blob_content_digest", False),
    ),
    EVIDENCE: (
        ("evidence_id", True),
        ("workspace_id", True),
        ("source_kind", True),
        ("source_native_id", True),
        ("source_locator", False),
        ("content_checksum", True),
        ("blob_content_digest", True),
        ("media_type", True),
        ("original_metadata_json", True),
        ("original_metadata_digest", True),
        ("sensitivity", True),
        ("parser_status", True),
        ("ingestion_status", True),
        ("staged_source_ref", False),
        ("import_run_id", False),
    ),
    LABELS: (
        ("label_event_id", True),
        ("evidence_id", True),
        ("workspace_id", True),
        ("label_action", True),
        ("permission_label", True),
    ),
    PROVENANCE: (
        ("provenance_event_id", True),
        ("evidence_id", True),
        ("workspace_id", True),
        ("actor_id", True),
        ("actor_kind", True),
        ("action", True),
        ("reason_code", False),
        ("reason_comment", False),
        ("parser_status", False),
        ("ingestion_status", False),
        ("source_kind", True),
        ("source_native_id", True),
        ("audit_ref", False),
    ),
    REFERENCES: (
        ("event_reference_id", True),
        ("provenance_event_id", True),
        ("evidence_id", True),
        ("workspace_id", True),
        ("source_kind", True),
        ("source_native_id", True),
        ("source_locator", False),
        ("span_pointer", False),
        ("excerpt", False),
    ),
    RECORDS: (
        ("normalized_record_id", True),
        ("evidence_id", True),
        ("workspace_id", True),
        ("evidence_blob_digest", True),
        ("record_type", True),
        ("schema_version", True),
        ("content_json", True),
        ("content_digest", True),
        ("parser_id", True),
        ("parser_version", True),
    ),
    SPANS: (
        ("normalized_span_id", True),
        ("normalized_record_id", True),
        ("evidence_id", True),
        ("workspace_id", True),
        ("span_kind", True),
        ("span_pointer", True),
    ),
}

#: The total column count this map declares, pinned so an edit that silently drops or
#: duplicates an entry is a failure rather than a map that quietly shrinks.
TEXT_COLUMN_COUNT = 78


def column_types_of(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    return {
        str(row[1]): str(row[2]).upper()
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def column_notnull_of(connection: sqlite3.Connection, table: str) -> dict[str, bool]:
    return {
        str(row[1]): bool(row[3])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def test_m2_25_the_hand_authored_text_column_map_is_exhaustive_and_exact(
    migrated: Path,
) -> None:
    """M2-25: the fixed map above names every TEXT column of all nine tables, no
    fewer and no more, with the exact NOT NULL/nullable split the live schema
    declares -- so an added, removed or renarrowed TEXT column is a failure here
    rather than a gap the rest of this section silently stops covering.
    """
    total = sum(len(columns) for columns in TEXT_COLUMNS_BY_TABLE.values())
    assert total == TEXT_COLUMN_COUNT
    assert set(TEXT_COLUMNS_BY_TABLE) == set(M2_TABLES)

    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        for table in M2_TABLES:
            declared_types = column_types_of(connection, table)
            declared_notnull = column_notnull_of(connection, table)
            live_text_columns = {
                name for name, kind in declared_types.items() if kind == "TEXT"
            }
            mapped = dict(TEXT_COLUMNS_BY_TABLE[table])
            assert set(mapped) == live_text_columns, table
            for column, required in mapped.items():
                assert declared_notnull[column] is required, (table, column)
    finally:
        connection.close()


def test_m2_25b_every_declared_text_column_carries_an_explicit_typeof_guard(
    migrated: Path,
) -> None:
    """M2-25: static proof, read straight from `sqlite_master`, that every TEXT
    column of all nine tables carries `typeof(<column>) = 'text'` in its own CHECK --
    not merely a pattern, a bound, an enum or a foreign key that a well-shaped BLOB
    could otherwise slip past, exactly as `blob_opaque_token_staged_source_ref` and
    `blob_structured_json_normalized_content_json` below prove for two columns whose
    other constraints alone never inspect storage class at all -- and that every one
    of those same 78 columns also carries an explicit `instr(<column>, char(0)) = 0`
    NUL-exclusion guard, so an embedded NUL can never hide inside an otherwise
    well-formed TEXT value. Optional columns keep their explicit NULL allowance: the
    guard reads only from the declared CHECK text, so a column's own `NOT NULL`
    split from M2-25 is untouched by this assertion.
    """
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        for table, columns in TEXT_COLUMNS_BY_TABLE.items():
            sql = object_sql(connection, table)
            declared_notnull = column_notnull_of(connection, table)
            for column, required in columns:
                assert f"typeof({column}) = 'text'" in sql, (table, column)
                assert f"instr({column}, char(0)) = 0" in sql, (table, column)
                assert declared_notnull[column] is required, (table, column)
    finally:
        connection.close()


# --- M2-25c: BLOB storage is refused for every text-value class, for the owner --


#: Byte strings shaped to pass every *other* constraint on their target column --
#: right length, right domain where one applies -- so each case below is refused for
#: exactly one reason: the value's storage class is BLOB, not TEXT.
BLOB_OPAQUE_TOKEN = b"stg-blob-ref-0001"
BLOB_JSON_METADATA = b'{"kind":"blob-metadata"}'
BLOB_JSON_CONTENT = b'{"body":"blob-content"}'
BLOB_LOCATOR = b"archive://blob-doc.md"
BLOB_COMMENT = b"a blob is not text, even printable text bytes"
BLOB_OPAQUE_GENERIC = b"\x00omnivia-blob-not-text\x00"

#: (case id, table, overrides, expected error substring). Every case runs under a
#: current lease and an open guard via `seed_chain`, exactly as M2-17 does, so it is
#: the declared CHECK -- not the fencing layer -- that refuses. Cases against a
#: stream table carry `UNIQUE_IDS` so the sequence trigger clears and the CHECK
#: under test is the one actually reached.
BLOB_STORAGE_NEGATIVE_CASES: tuple[tuple[str, str, dict[str, object], str], ...] = (
    # OpaqueToken, required and its own primary key -- neither of its two other
    # CHECKs (a length bound, `NOT GLOB '*[^!-~]*'`) touches storage class: GLOB
    # never matches a BLOB, so `NOT GLOB` is trivially true for one. Only the
    # explicit typeof guard refuses this.
    (
        "blob_opaque_token_staged_source_ref",
        STAGED,
        {"staged_source_ref": BLOB_OPAQUE_TOKEN},
        "CHECK constraint failed",
    ),
    # `import_run_id` is also the INSERT trigger's own ancestry key: it looks up
    # `omnivia_durable_jobs.job_id = NEW.import_run_id`, and a BLOB never equals a
    # TEXT `job_id` either, so the trigger's "no such job" refusal is reached before
    # the table's own CHECK is -- a second, independent layer that agrees.
    (
        "blob_opaque_token_import_run_id_optional",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "import_run_id": BLOB_OPAQUE_TOKEN},
        "must name an existing ingestion.import durable job",
    ),
    # Structured canonical JSON, required, bound only by length -- no pattern to
    # incidentally reject a BLOB shaped to fit inside it.
    (
        "blob_structured_json_staged_original_metadata_json",
        STAGED,
        {**UNIQUE_IDS[STAGED], "original_metadata_json": BLOB_JSON_METADATA},
        "CHECK constraint failed",
    ),
    (
        "blob_structured_json_normalized_content_json",
        RECORDS,
        {**UNIQUE_IDS[RECORDS], "content_json": BLOB_JSON_CONTENT},
        "CHECK constraint failed",
    ),
    # Canonical JSON digest -- the `sha256:` address of the record's own JSON bytes.
    (
        "blob_canonical_json_digest_normalized_record",
        RECORDS,
        {**UNIQUE_IDS[RECORDS], "content_digest": BLOB_OPAQUE_GENERIC},
        "CHECK constraint failed",
    ),
    # The internal blob-address checksum -- omnivia_blob_objects.content_digest,
    # the exact column this repair adds a guard to.
    (
        "blob_internal_checksum_blob_objects_content_digest",
        BLOBS,
        {"content_digest": BLOB_OPAQUE_GENERIC},
        "CHECK constraint failed",
    ),
    # The separate public, provider-neutral EvidenceChecksum domain.
    (
        "blob_public_checksum_evidence_content_checksum",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "content_checksum": BLOB_OPAQUE_GENERIC},
        "CHECK constraint failed",
    ),
    # MediaType.
    (
        "blob_media_type_evidence",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "media_type": BLOB_OPAQUE_GENERIC},
        "CHECK constraint failed",
    ),
    # Identifier, required, primary key.
    (
        "blob_identifier_evidence_id",
        EVIDENCE,
        {"evidence_id": BLOB_OPAQUE_GENERIC},
        "CHECK constraint failed",
    ),
    # OpenCode, deliberately left open rather than narrowed to a closed set.
    # `staged_source_ref` is cleared so the INSERT trigger's own ancestry lookup --
    # which also compares `source_kind` and would otherwise refuse first -- is not
    # reached, and the CHECK under test is the one actually exercised.
    (
        "blob_open_code_evidence_source_kind",
        EVIDENCE,
        {
            **UNIQUE_IDS[EVIDENCE],
            "source_kind": BLOB_OPAQUE_GENERIC,
            "staged_source_ref": None,
        },
        "CHECK constraint failed",
    ),
    # A genuinely closed `IN (...)` enum.
    (
        "blob_closed_enum_staged_staging_outcome",
        STAGED,
        {**UNIQUE_IDS[STAGED], "staging_outcome": BLOB_OPAQUE_GENERIC},
        "CHECK constraint failed",
    ),
    # Optional, length-only fields -- the same unprotected shape as the structured
    # JSON cases above, just nullable.
    (
        "blob_optional_length_only_source_locator",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "source_locator": BLOB_LOCATOR},
        "CHECK constraint failed",
    ),
    (
        "blob_optional_length_only_reason_comment",
        PROVENANCE,
        {**UNIQUE_IDS[PROVENANCE], "reason_comment": BLOB_COMMENT},
        "CHECK constraint failed",
    ),
    # Relationship text fields -- the ancestry columns a foreign key also
    # constrains. `staged_source_ref` is also the INSERT trigger's own ancestry key
    # (it looks up `omnivia_staged_sources.staged_source_ref = NEW.staged_source_ref`,
    # and a BLOB never equals that TEXT column either), so the trigger's ancestry
    # refusal is reached before the table's own CHECK is -- a second, independent
    # layer that agrees with it.
    (
        "blob_relationship_text_evidence_staged_source_ref",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "staged_source_ref": BLOB_OPAQUE_TOKEN},
        (
            "must name a verified staged source in this workspace with the same "
            "source_kind and blob content digest"
        ),
    ),
    (
        "blob_relationship_text_record_evidence_blob_digest",
        RECORDS,
        {**UNIQUE_IDS[RECORDS], "evidence_blob_digest": BLOB_OPAQUE_GENERIC},
        "CHECK constraint failed",
    ),
)


@pytest.mark.parametrize(
    ("table", "overrides", "expected"),
    [
        (table, overrides, expected)
        for _case, table, overrides, expected in BLOB_STORAGE_NEGATIVE_CASES
    ],
    ids=[case for case, _table, _overrides, _expected in BLOB_STORAGE_NEGATIVE_CASES],
)
def test_m2_25c_blob_storage_is_refused_for_every_text_value_class_with_no_partial_rows(
    owned: Owned, table: str, overrides: dict[str, object], expected: str
) -> None:
    """M2-25: a real BLOB, correctly shaped by every measure that is not storage
    class, is refused for the legitimate fenced owner across every text-value class
    this migration stores -- structured JSON, a canonical JSON digest, an opaque
    token, an identifier, an open code, both checksum domains, a media type, a
    closed enum, an optional length-only field and a relationship text field -- and
    each refusal leaves every table exactly as populated as it was before the
    attempt.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError), match=expected):
        write(owned, table, **overrides)
    assert counts(owned.connection) == before


# --- Repair regression: INSERT OR REPLACE never overwrites accepted history -----


#: The primary-key columns of each table, in declaration order -- the identity the
#: duplicate-insert guard refuses to see collide, whether the colliding statement
#: says `INSERT` or `INSERT OR REPLACE`.
PRIMARY_KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    BLOBS: ("workspace_id", "content_digest"),
    INTEGRITY: ("integrity_event_id",),
    STAGED: ("staged_source_ref",),
    EVIDENCE: ("evidence_id",),
    LABELS: ("label_event_id",),
    PROVENANCE: ("provenance_event_id",),
    REFERENCES: ("event_reference_id",),
    RECORDS: ("normalized_record_id",),
    SPANS: ("normalized_span_id",),
}

#: One non-key fact per table, changed from what `seed_chain` originally wrote. If
#: the duplicate-identity guard did not fire on `INSERT OR REPLACE` -- SQLite's own
#: conflict resolution deletes the old row and inserts this one -- this is the value
#: that would have visibly overwritten the accepted row's history. Stream tables
#: also carry an advanced sequence number: their guard checks "does the sequence
#: advance" before "is the identity a duplicate", so a stale sequence would raise
#: that earlier, unrelated refusal instead of the one this test targets.
_SEQUENCE_COLUMN_BY_TABLE: dict[str, str] = {
    table: sequence_column for table, sequence_column, _parent in SEQUENCE_STREAMS
}
REPLACE_FACT_OVERRIDES: dict[str, dict[str, object]] = {
    BLOBS: {"verified_at_us": BASE_US + 999},
    INTEGRITY: {
        "checked_at_us": BASE_US + 999,
        _SEQUENCE_COLUMN_BY_TABLE[INTEGRITY]: 2,
    },
    STAGED: {"recorded_at_us": BASE_US + 999},
    EVIDENCE: {"recorded_at_us": BASE_US + 999},
    LABELS: {
        "recorded_at_us": BASE_US + 999,
        _SEQUENCE_COLUMN_BY_TABLE[LABELS]: 2,
    },
    PROVENANCE: {
        "occurred_at_us": BASE_US + 999,
        _SEQUENCE_COLUMN_BY_TABLE[PROVENANCE]: 2,
    },
    REFERENCES: {
        "excerpt": "forged retroactive excerpt",
        _SEQUENCE_COLUMN_BY_TABLE[REFERENCES]: 2,
    },
    RECORDS: {
        "recorded_at_us": BASE_US + 999,
        _SEQUENCE_COLUMN_BY_TABLE[RECORDS]: 2,
    },
    SPANS: {
        "recorded_at_us": BASE_US + 999,
        _SEQUENCE_COLUMN_BY_TABLE[SPANS]: 2,
    },
}

#: The exact "identity is immutable; duplicate insert refused" substring each
#: table's own BEFORE INSERT guard raises, read straight from the migration text.
#: `BLOBS`' composite key wraps the column list in parentheses, so it alone needs
#: `re.escape` before it can serve as a `pytest.raises(match=...)` pattern.
REPLACE_REFUSAL_MESSAGES: dict[str, str] = {
    BLOBS: re.escape(
        "omnivia_blob_objects (workspace_id, content_digest) identity is "
        "immutable; duplicate insert refused"
    ),
    INTEGRITY: (
        "omnivia_blob_integrity_events.integrity_event_id identity is immutable; "
        "duplicate insert refused"
    ),
    STAGED: (
        "omnivia_staged_sources.staged_source_ref identity is immutable; "
        "duplicate insert refused"
    ),
    EVIDENCE: (
        "omnivia_evidence_artifacts.evidence_id identity is immutable; duplicate "
        "insert refused"
    ),
    LABELS: (
        "omnivia_evidence_permission_labels.label_event_id identity is immutable; "
        "duplicate insert refused"
    ),
    PROVENANCE: (
        "omnivia_evidence_provenance_events.provenance_event_id identity is "
        "immutable; duplicate insert refused"
    ),
    REFERENCES: (
        "omnivia_evidence_event_references.event_reference_id identity is "
        "immutable; duplicate insert refused"
    ),
    RECORDS: (
        "omnivia_normalized_source_records.normalized_record_id identity is "
        "immutable; duplicate insert refused"
    ),
    SPANS: (
        "omnivia_normalized_source_spans.normalized_span_id identity is "
        "immutable; duplicate insert refused"
    ),
}


def insert_or_replace(
    connection: sqlite3.Connection, table: str, values: dict[str, object]
) -> None:
    """Issue a real `INSERT OR REPLACE`, never `write`'s plain `INSERT`."""
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({marks})",
        tuple(values.values()),
    )


def replace(holder: Owned, table: str, **overrides: object) -> None:
    """`INSERT OR REPLACE` one row under current authority, fenced like `write`."""
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert_or_replace(holder.connection, table, row_for(table, **overrides))


def fetch_row(
    connection: sqlite3.Connection, table: str, identity: dict[str, object]
) -> dict[str, object]:
    columns = list(DEFAULTS_BY_TABLE[table])
    pk_columns = PRIMARY_KEY_COLUMNS[table]
    where = " AND ".join(f"{column} = ?" for column in pk_columns)
    params = tuple(identity[column] for column in pk_columns)
    row = connection.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {where}", params
    ).fetchone()
    assert row is not None, table
    return dict(zip(columns, row, strict=True))


@pytest.mark.parametrize("table", M2_TABLES)
def test_repair_insert_or_replace_never_overwrites_an_accepted_row(
    owned: Owned, table: str
) -> None:
    """Repair regression: for the legitimate fenced owner, a real `INSERT OR
    REPLACE` bound to the same primary identity as an already-accepted row is
    refused by that table's own duplicate-identity guard -- not silently accepted
    by SQLite's default conflict resolution, which would delete the old row and
    replace it with the new one. The original row, byte for byte, and every
    table's count are exactly what `seed_chain` left them.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    original = fetch_row(owned.connection, table, DEFAULTS_BY_TABLE[table])

    with pytest.raises(
        (sqlite3.IntegrityError, sqlite3.DatabaseError),
        match=REPLACE_REFUSAL_MESSAGES[table],
    ):
        replace(owned, table, **REPLACE_FACT_OVERRIDES[table])

    assert counts(owned.connection) == before
    assert fetch_row(owned.connection, table, DEFAULTS_BY_TABLE[table]) == original


@pytest.mark.parametrize("table", M2_TABLES)
def test_repair_insert_or_replace_has_no_hidden_rowid_collision_target(
    owned: Owned, table: str
) -> None:
    """Repair regression: SQLite rowid tables expose a second replacement target.

    With the default `recursive_triggers = 0`, `INSERT OR REPLACE` aimed at an
    existing hidden rowid deletes accepted history without running its DELETE
    trigger, even when the new row carries a fresh valid logical primary key. Every
    M2 history table is therefore `WITHOUT ROWID`: all three hidden-rowid aliases
    are absent, and a real fenced-owner replacement that names the first inserted
    row's former rowid position plus a fresh logical identity is refused before it
    can replace anything.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    original = fetch_row(owned.connection, table, DEFAULTS_BY_TABLE[table])

    for hidden_alias in ("rowid", "_rowid_", "oid"):
        replacement = {hidden_alias: 1, **unique_row_for(table)}
        with (
            fenced_transaction(
                owned.connection,
                owned.identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=owned.generation,
            ),
            pytest.raises(sqlite3.DatabaseError, match="has no column named"),
        ):
            insert_or_replace(owned.connection, table, replacement)

        assert counts(owned.connection) == before
        assert fetch_row(owned.connection, table, DEFAULTS_BY_TABLE[table]) == original

    with pytest.raises(sqlite3.DatabaseError, match="no such column"):
        owned.connection.execute(f"SELECT rowid FROM {table}").fetchone()


# --- Repair regression: an embedded NUL is refused for every text domain -------


NUL = "\x00"

#: A `content_checksum` at exactly the domain's 256-character bound, followed by a
#: hidden NUL and an overlength suffix -- proving the guard still fires even when
#: everything visible before the NUL is itself already valid at the domain's own
#: length limit.
MAX_LENGTH_CHECKSUM_PREFIX = "sha256:" + "a" * (256 - len("sha256:"))
assert len(MAX_LENGTH_CHECKSUM_PREFIX) == 256

#: (case id, table, overrides, expected error substring). Every case runs under a
#: current lease and an open guard via `seed_chain`, with a fresh primary identity
#: (via `UNIQUE_IDS`, or -- for `STAGED`, whose primary key is the value under
#: test -- for free) so the CHECK reached is the column's own NUL-exclusion guard,
#: not the duplicate-identity guard.
NUL_NEGATIVE_CASES: tuple[tuple[str, str, dict[str, object], str], ...] = (
    # Identifier -- omnivia_evidence_provenance_events.actor_id.
    (
        "nul_identifier_actor_id",
        PROVENANCE,
        {**UNIQUE_IDS[PROVENANCE], "actor_id": "a" + NUL + "INVALID"},
        "CHECK constraint failed",
    ),
    # OpenCode -- omnivia_evidence_permission_labels.permission_label.
    (
        "nul_opencode_permission_label",
        LABELS,
        {**UNIQUE_IDS[LABELS], "permission_label": "group" + NUL + ".INVALID"},
        "CHECK constraint failed",
    ),
    # OpaqueToken, also its own primary key -- omnivia_staged_sources.
    # staged_source_ref. The value itself is a fresh identity, so no `UNIQUE_IDS`
    # spread is needed to keep it clear of `seed_chain`'s row.
    (
        "nul_opaquetoken_staged_source_ref",
        STAGED,
        {"staged_source_ref": "stg" + NUL + " hidden"},
        "CHECK constraint failed",
    ),
    # OpaqueToken again, at its own 512-character bound, to prove the guard still
    # fires when the visible prefix is already maximally long and the hidden tail
    # is what pushes the true length past the domain's own bound.
    (
        "nul_opaquetoken_staged_source_ref_max_length_prefix",
        STAGED,
        {"staged_source_ref": "s" * 512 + NUL + "h" * 20},
        "CHECK constraint failed",
    ),
    # EvidenceChecksum -- omnivia_evidence_artifacts.content_checksum.
    (
        "nul_evidencechecksum_content_checksum",
        EVIDENCE,
        {
            **UNIQUE_IDS[EVIDENCE],
            "content_checksum": "sha256:" + "a" * 10 + NUL + "hiddendigest",
        },
        "CHECK constraint failed",
    ),
    # EvidenceChecksum again, at its own 256-character bound.
    (
        "nul_evidencechecksum_content_checksum_max_length_prefix",
        EVIDENCE,
        {
            **UNIQUE_IDS[EVIDENCE],
            "content_checksum": MAX_LENGTH_CHECKSUM_PREFIX + NUL + "b" * 50,
        },
        "CHECK constraint failed",
    ),
    # MediaType -- omnivia_evidence_artifacts.media_type.
    (
        "nul_mediatype_media_type",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "media_type": "text/pl" + NUL + "ain"},
        "CHECK constraint failed",
    ),
    # A genuinely closed `IN (...)` enum -- omnivia_staged_sources.staging_outcome.
    (
        "nul_closed_enum_staging_outcome",
        STAGED,
        {**UNIQUE_IDS[STAGED], "staging_outcome": "verified" + NUL + "hidden"},
        "CHECK constraint failed",
    ),
    # Structured canonical JSON -- omnivia_normalized_source_records.content_json.
    (
        "nul_structured_json_content_json",
        RECORDS,
        {
            **UNIQUE_IDS[RECORDS],
            "content_json": '{"body":"ok"}' + NUL + '{"hidden":true}',
        },
        "CHECK constraint failed",
    ),
    # Optional, length-only field -- omnivia_evidence_artifacts.source_locator.
    (
        "nul_optional_length_only_source_locator",
        EVIDENCE,
        {**UNIQUE_IDS[EVIDENCE], "source_locator": "evd" + NUL + "/hidden"},
        "CHECK constraint failed",
    ),
)


@pytest.mark.parametrize(
    ("table", "overrides", "expected"),
    [
        (table, overrides, expected)
        for _case, table, overrides, expected in NUL_NEGATIVE_CASES
    ],
    ids=[case for case, _table, _overrides, _expected in NUL_NEGATIVE_CASES],
)
def test_repair_an_embedded_nul_is_refused_for_every_text_domain(
    owned: Owned, table: str, overrides: dict[str, object], expected: str
) -> None:
    """Repair regression: a value that is otherwise well-formed for its domain --
    right shape, right length -- but carries a hidden NUL byte is refused for the
    legitimate fenced owner across Identifier, OpenCode, OpaqueToken,
    EvidenceChecksum, MediaType, a closed enum, structured JSON and an optional
    length-only field, and each refusal leaves every table exactly as populated as
    it was before the attempt.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError), match=expected):
        write(owned, table, **overrides)
    assert counts(owned.connection) == before


# --- M2-25d: every public-domain column and internal digest is exercised --------

# Fixed contract map, intentionally not derived from the migration SQL. Relationship
# columns are included: being an FK does not stop the public wire-domain constraint
# from applying to the stored value itself.
PUBLIC_DOMAIN_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "identifier": (
        (BLOBS, "workspace_id"),
        (INTEGRITY, "integrity_event_id"),
        (INTEGRITY, "workspace_id"),
        (INTEGRITY, "inventory_id"),
        (STAGED, "workspace_id"),
        (STAGED, "source_version"),
        (EVIDENCE, "evidence_id"),
        (EVIDENCE, "workspace_id"),
        (EVIDENCE, "source_native_id"),
        (LABELS, "label_event_id"),
        (LABELS, "evidence_id"),
        (LABELS, "workspace_id"),
        (PROVENANCE, "provenance_event_id"),
        (PROVENANCE, "evidence_id"),
        (PROVENANCE, "workspace_id"),
        (PROVENANCE, "actor_id"),
        (PROVENANCE, "source_native_id"),
        (PROVENANCE, "audit_ref"),
        (REFERENCES, "event_reference_id"),
        (REFERENCES, "provenance_event_id"),
        (REFERENCES, "evidence_id"),
        (REFERENCES, "workspace_id"),
        (REFERENCES, "source_native_id"),
        (RECORDS, "normalized_record_id"),
        (RECORDS, "evidence_id"),
        (RECORDS, "workspace_id"),
        (RECORDS, "schema_version"),
        (RECORDS, "parser_id"),
        (RECORDS, "parser_version"),
        (SPANS, "normalized_span_id"),
        (SPANS, "normalized_record_id"),
        (SPANS, "evidence_id"),
        (SPANS, "workspace_id"),
    ),
    "open_code": (
        (STAGED, "source_kind"),
        (EVIDENCE, "source_kind"),
        (EVIDENCE, "sensitivity"),
        (EVIDENCE, "parser_status"),
        (EVIDENCE, "ingestion_status"),
        (LABELS, "permission_label"),
        (PROVENANCE, "actor_kind"),
        (PROVENANCE, "action"),
        (PROVENANCE, "reason_code"),
        (PROVENANCE, "parser_status"),
        (PROVENANCE, "ingestion_status"),
        (PROVENANCE, "source_kind"),
        (REFERENCES, "source_kind"),
        (RECORDS, "record_type"),
        (SPANS, "span_kind"),
    ),
    "opaque_token": (
        (STAGED, "staged_source_ref"),
        (EVIDENCE, "staged_source_ref"),
        (EVIDENCE, "import_run_id"),
    ),
    "evidence_checksum": ((EVIDENCE, "content_checksum"),),
    "media_type": ((STAGED, "media_type"), (EVIDENCE, "media_type")),
}

PUBLIC_DOMAIN_INVALID_VALUES: dict[str, tuple[str, ...]] = {
    "identifier": ("", "-bad", "bad value", "é", "x" * 129),
    "open_code": ("", "Bad", "a..b", "a.1", "a.", "a" * 129),
    "opaque_token": ("", "contains space", "é", "x" * 513),
    "evidence_checksum": ("", "sHA:abc", "sha256:", "sha256:abc:def", "x" * 257),
    "media_type": ("", "/plain", "text/", "text/plain/x", "x" * 256),
}

CLOSED_TEXT_COLUMNS: tuple[tuple[str, str], ...] = (
    (INTEGRITY, "outcome"),
    (STAGED, "staging_outcome"),
    (LABELS, "label_action"),
)


@pytest.mark.parametrize(
    ("domain", "table", "column"),
    [
        (domain, table, column)
        for domain, columns in PUBLIC_DOMAIN_COLUMNS.items()
        for table, column in columns
    ],
    ids=lambda value: str(value),
)
def test_m2_25d_every_public_domain_column_refuses_its_full_malformed_boundary_set(
    domain: str, table: str, column: str
) -> None:
    """Every stored occurrence of a frozen public domain gets direct CHECK evidence.

    Triggers are removed only to keep malformed relationship/workspace values from
    being refused first by authority or ancestry; table CHECKs and FKs remain live.
    """
    for value in PUBLIC_DOMAIN_INVALID_VALUES[domain]:
        connection = replay_without_m2_triggers()
        try:
            seed_chain_without_triggers(connection)
            overrides = {**UNIQUE_IDS[table], column: value}
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                insert(connection, table, row_for(table, **overrides))
        finally:
            connection.close()


@pytest.mark.parametrize(("table", "column"), CLOSED_TEXT_COLUMNS)
def test_m2_25e_every_closed_text_column_refuses_unknown_empty_and_nul_values(
    table: str, column: str
) -> None:
    for value in ("", "unknown_value", "valid\x00hidden"):
        connection = replay_without_m2_triggers()
        try:
            seed_chain_without_triggers(connection)
            overrides = {**UNIQUE_IDS[table], column: value}
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                insert(connection, table, row_for(table, **overrides))
        finally:
            connection.close()


INTERNAL_DIGEST_COLUMNS_EXHAUSTIVE: tuple[tuple[str, str], ...] = (
    (BLOBS, "content_digest"),
    (INTEGRITY, "content_digest"),
    (INTEGRITY, "observed_digest"),
    (STAGED, "declared_checksum"),
    (STAGED, "computed_checksum"),
    (STAGED, "original_metadata_digest"),
    (STAGED, "blob_content_digest"),
    (EVIDENCE, "blob_content_digest"),
    (EVIDENCE, "original_metadata_digest"),
    (RECORDS, "evidence_blob_digest"),
    (RECORDS, "content_digest"),
)


@pytest.mark.parametrize(("table", "column"), INTERNAL_DIGEST_COLUMNS_EXHAUSTIVE)
def test_m2_25f_every_internal_digest_column_refuses_noncanonical_sha256_spelling(
    table: str, column: str
) -> None:
    for value in (
        "SHA256:" + "a" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "g" * 64,
    ):
        connection = replay_without_m2_triggers()
        try:
            seed_chain_without_triggers(connection)
            overrides = {**UNIQUE_IDS[table], column: value}
            if table == INTEGRITY and column == "observed_digest":
                overrides["outcome"] = "digest_mismatch"
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                insert(connection, table, row_for(table, **overrides))
        finally:
            connection.close()


# --- M2-26: the index and foreign-key inventory, exactly -----------------------


def index_table(connection: sqlite3.Connection, index: str) -> str:
    row = connection.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index,),
    ).fetchone()
    assert row is not None, index
    return str(row[0])


def actual_index_columns(connection: sqlite3.Connection, index: str) -> tuple[str, ...]:
    """The index's own columns, in position order, straight from `index_info`."""
    return tuple(
        str(row[2])
        for row in sorted(
            connection.execute(f'PRAGMA index_info("{index}")'), key=lambda row: row[0]
        )
    )


def actual_index_is_unique(connection: sqlite3.Connection, index: str) -> bool:
    table = index_table(connection, index)
    for row in connection.execute(f'PRAGMA index_list("{table}")'):
        if str(row[1]) == index:
            return bool(row[2])
    raise AssertionError(index)


#: Every declared index that must be UNIQUE: the composite-parent-key indexes a
#: foreign key resolves against, and the sequence indexes that are the durable half
#: of "monotonic". The read-path indexes are the only ones that are not.
M2_UNIQUE_INDEXES = frozenset(M2_PARENT_KEY_INDEXES) | frozenset(M2_SEQUENCE_INDEXES)


@pytest.mark.parametrize("index", M2_INDEXES, ids=M2_INDEXES)
def test_m2_26_every_named_index_has_exactly_its_declared_columns_and_uniqueness(
    migrated: Path, index: str
) -> None:
    """M2-26: each of the nineteen named indexes resolves, and `index_info` /
    `index_list` agree on its ordered columns and its uniqueness with the fixed
    oracle above -- not with each other, and not with whatever the migration text
    happens to say.
    """
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        assert actual_index_columns(connection, index) == M2_INDEX_COLUMNS[index]
        assert actual_index_is_unique(connection, index) == (index in M2_UNIQUE_INDEXES)
    finally:
        connection.close()


@pytest.mark.parametrize("table", M2_TABLES, ids=M2_TABLES)
def test_m2_26b_every_table_has_exactly_its_declared_foreign_key_edges(
    migrated: Path, table: str
) -> None:
    """M2-26: each M2 table's actual `foreign_key_list`, reassembled into whole
    composite keys by `foreign_keys_of`, is exactly the fixed set declared for it --
    same parent, same ordered child columns, same ordered parent columns -- no more
    edges and no fewer.
    """
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        assert foreign_keys_of(connection, table) == M2_FOREIGN_KEYS[table]
    finally:
        connection.close()


#: None of the migration's `FOREIGN KEY` clauses names an `ON UPDATE`/`ON DELETE`
#: action, so every edge carries SQLite's default for both -- the expected value
#: below is that default, not something read back from the connection under test.
M2_FOREIGN_KEY_DEFAULT_ACTION = "NO ACTION"


@pytest.mark.parametrize("table", M2_TABLES, ids=M2_TABLES)
def test_m2_26c_every_declared_foreign_key_carries_the_default_actions(
    migrated: Path, table: str
) -> None:
    """M2-26: every row `PRAGMA foreign_key_list` reports for this table names
    `NO ACTION` for both ON UPDATE and ON DELETE -- so an edge that silently gained a
    CASCADE or SET NULL clause is a failure here, not something only a mutation test
    downstream would notice.
    """
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        rows = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        assert len(rows) == sum(
            len(child_columns)
            for _parent, child_columns, _parent_columns in M2_FOREIGN_KEYS[table]
        )
        for row in rows:
            on_update, on_delete = str(row[5]), str(row[6])
            assert on_update == M2_FOREIGN_KEY_DEFAULT_ACTION, row
            assert on_delete == M2_FOREIGN_KEY_DEFAULT_ACTION, row
    finally:
        connection.close()


def test_m2_26d_the_fixed_foreign_key_graph_is_acyclic() -> None:
    """M2-26: walking the fixed `M2_FOREIGN_KEYS` edges, child to parent, never
    revisits a table on the *same path* -- a property of the declared oracle itself,
    independent of what the live database reports, so a future edit that introduced a
    cycle in the oracle would fail here even before touching a connection. A table
    reached twice via two different edges (a diamond, such as both
    `omnivia_evidence_artifacts` and `omnivia_staged_sources` naming
    `omnivia_blob_objects`) is not a cycle and must not be flagged as one.
    """

    def walk(table: str, path: tuple[str, ...]) -> None:
        assert table not in path, (path, table)
        for parent, _child_columns, _parent_columns in M2_FOREIGN_KEYS.get(
            table, set()
        ):
            if parent in M2_FOREIGN_KEYS:
                walk(parent, path + (table,))

    for start in M2_TABLES:
        walk(start, ())


def test_m2_26e_every_referenced_parent_table_and_columns_exist(
    migrated: Path,
) -> None:
    """M2-26: every parent named by the fixed FK oracle is a real table in the
    migrated schema, and every parent column named by an edge is a real column of
    that table -- so a typo in the hand-authored oracle itself is caught here rather
    than silently describing a relationship that cannot exist.
    """
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        existing_tables = object_names(connection, "table")
        for table in M2_TABLES:
            for parent, _child_columns, parent_columns in M2_FOREIGN_KEYS[table]:
                assert parent in existing_tables, (table, parent)
                parent_columns_actual = set(columns_of(connection, parent))
                for column in parent_columns:
                    assert column in parent_columns_actual, (table, parent, column)
    finally:
        connection.close()


# --- M2-27: the singleton workspace binding holds for every table --------------


@pytest.mark.parametrize("table", M2_TABLES)
def test_m2_27_insert_refuses_a_row_bound_to_a_different_workspace_for_every_table(
    owned: Owned, table: str
) -> None:
    """M2-27: `NEW.workspace_id` must equal the one singleton workspace this database
    holds, not merely satisfy the row's own foreign keys -- and this is true for every
    one of the nine tables, not only the ones a foreign key happens to reach.

    Executed straight on the connection rather than through `write`, whose overrides
    still land inside a transaction already opened for `WORKSPACE_ID`: the *only*
    fenced-transaction call here is the one made directly against the current owner's
    real authority tuple, so nothing about it is wrong, and the only thing wrong with
    the row itself is the value the persisted guard inspects -- `NEW.workspace_id`,
    forced to a second, otherwise valid, workspace identifier.
    """
    seed_chain(owned)
    before = counts(owned.connection)

    row = unique_row_for(table, workspace_id=OTHER_WORKSPACE_ID)
    with (
        pytest.raises(sqlite3.DatabaseError, match=f"unguarded INSERT on {table}"),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        owned.connection.execute(insert_sql(table, row))

    assert counts(owned.connection) == before


# --- M2-28: the internal sha256 digest is exact -- 64 lowercase hex, one algorithm --


#: Every internal `sha256:<64 lowercase hex>` column, paired with whatever extra
#: override keeps an ancestry trigger from refusing the row before its own CHECK is
#: reached -- the same bypass M2-25c already needs for
#: `blob_open_code_evidence_source_kind`.
INTERNAL_DIGEST_COLUMNS: tuple[tuple[str, str, dict[str, object]], ...] = (
    (BLOBS, "content_digest", {}),
    (INTEGRITY, "content_digest", {}),
    (STAGED, "declared_checksum", {}),
    (EVIDENCE, "blob_content_digest", {"staged_source_ref": None}),
    (RECORDS, "evidence_blob_digest", {}),
)

DIGEST_SPELLING_SHAPES: tuple[str, ...] = (
    "too_short",
    "too_long",
    "uppercase_hex",
    "wrong_algorithm",
)


def _malformed_digest(shape: str) -> str:
    if shape == "too_short":
        return "sha256:" + "a" * 63
    if shape == "too_long":
        return "sha256:" + "a" * 65
    if shape == "uppercase_hex":
        return "sha256:A" + "a" * 63
    if shape == "wrong_algorithm":
        return "sha255:" + "a" * 64
    raise AssertionError(shape)


DIGEST_SPELLING_CASES: tuple[tuple[str, str, dict[str, object]], ...] = tuple(
    (
        f"{table}_{column}_{shape}",
        table,
        {**UNIQUE_IDS[table], **extra, column: _malformed_digest(shape)},
    )
    for table, column, extra in INTERNAL_DIGEST_COLUMNS
    for shape in DIGEST_SPELLING_SHAPES
)


@pytest.mark.parametrize(
    ("table", "overrides"),
    [(table, overrides) for _case, table, overrides in DIGEST_SPELLING_CASES],
    ids=[case for case, _table, _overrides in DIGEST_SPELLING_CASES],
)
def test_m2_28_the_internal_sha256_digest_refuses_every_malformed_spelling(
    owned: Owned, table: str, overrides: dict[str, object]
) -> None:
    """M2-28: `sha256:` + exactly 64 lowercase hex characters, everywhere this
    runtime's internal blob address is stored -- 63 or 65 hex characters, one
    uppercase hex digit, or a differently-spelled algorithm prefix are each refused.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
        write(owned, table, **overrides)
    assert counts(owned.connection) == before


# --- M2-29: content length is non-negative, and a conflicting identity is refused --

#: A fresh, mutually consistent zero-length row for each table that carries this
#: column -- STAGED's `verified` row must still name a blob whose own length agrees,
#: so its own zero-length BLOBS row is seeded first.
ZERO_LENGTH_ROW: dict[str, dict[str, object]] = {
    BLOBS: {"content_digest": DIGEST_C, "content_length_bytes": 0},
    STAGED: {
        "staged_source_ref": "stg-zero-length",
        "declared_checksum": DIGEST_C,
        "computed_checksum": DIGEST_C,
        "blob_content_digest": DIGEST_C,
        "content_length_bytes": 0,
    },
}


@pytest.mark.parametrize("table", (BLOBS, STAGED))
def test_m2_29_content_length_bytes_admits_zero_and_refuses_negative(
    owned: Owned, table: str
) -> None:
    """M2-29: `content_length_bytes >= 0` on both tables that carry it -- zero is a
    legitimate, checksummable identity by the migration's own design, and only a
    genuinely negative length is refused.
    """
    seed_chain(owned)
    if table == STAGED:
        write(owned, BLOBS, content_digest=DIGEST_C, content_length_bytes=0)
    write(owned, table, **ZERO_LENGTH_ROW[table])
    stored = owned.connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE content_length_bytes = 0"
    ).fetchone()
    assert stored == (1,)

    before = counts(owned.connection)
    with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
        write(owned, table, **{**UNIQUE_IDS[table], "content_length_bytes": -1})
    assert counts(owned.connection) == before


def test_m2_29b_a_conflicting_blob_identity_and_length_is_refused_by_the_foreign_key(
    owned: Owned,
) -> None:
    """M2-29: `omnivia_staged_sources`' composite foreign key names the digest *and*
    the length together -- a staged source cannot claim the right blob at a length
    that blob does not actually have, even though every other constraint on the row
    is satisfied. Executed under `replay_without_m2_triggers` so the refusal is
    provably the declared foreign key's alone, since the staged-sources INSERT
    trigger checks nothing about ancestry beyond the singleton workspace binding.
    """
    connection = replay_without_m2_triggers()
    try:
        seed_chain_without_triggers(connection)
        conflicting_length = cast("int", BLOB_DEFAULTS["content_length_bytes"]) + 1

        before = count(connection, STAGED)
        with pytest.raises(
            sqlite3.DatabaseError, match="FOREIGN KEY constraint failed"
        ):
            insert(
                connection,
                STAGED,
                row_for(
                    STAGED,
                    staged_source_ref="stg-conflicting-length",
                    content_length_bytes=conflicting_length,
                ),
            )
        assert count(connection, STAGED) == before
    finally:
        connection.close()


def test_m2_29c_a_conflicting_blob_content_length_at_the_same_workspace_digest_identity_is_refused(
    owned: Owned,
) -> None:
    """M2-29 (requirement-9 regression): `omnivia_blob_objects`' primary identity is
    `(workspace_id, content_digest)` alone -- `content_length_bytes` is not part of
    it -- so a second row naming the same workspace and digest but a different
    length is a duplicate-identity write, not a distinct row, and the same guard
    that refuses an exact duplicate must refuse it too. Proved against the blob row
    itself, under the real fenced-owner path, for both a plain `INSERT` and an
    `INSERT OR REPLACE`, and the original row's length and timestamps -- not merely
    a foreign key elsewhere -- must survive completely unchanged.
    """
    seed_chain(owned)
    original = row_snapshot(
        owned.connection,
        BLOBS,
        {"workspace_id": WORKSPACE_ID, "content_digest": DIGEST_A},
    )
    before_count = count(owned.connection, BLOBS)
    conflicting_length = cast("int", BLOB_DEFAULTS["content_length_bytes"]) + 1

    with pytest.raises(sqlite3.DatabaseError, match="identity is immutable"):
        write(owned, BLOBS, content_length_bytes=conflicting_length)
    assert count(owned.connection, BLOBS) == before_count
    assert (
        row_snapshot(
            owned.connection,
            BLOBS,
            {"workspace_id": WORKSPACE_ID, "content_digest": DIGEST_A},
        )
        == original
    )

    with (
        pytest.raises(sqlite3.DatabaseError, match="identity is immutable"),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        insert_or_replace(
            owned.connection,
            BLOBS,
            row_for(BLOBS, content_length_bytes=conflicting_length),
        )
    assert count(owned.connection, BLOBS) == before_count
    assert (
        row_snapshot(
            owned.connection,
            BLOBS,
            {"workspace_id": WORKSPACE_ID, "content_digest": DIGEST_A},
        )
        == original
    )


#: Every stored byte-length occurrence in M2. This fixed inventory prevents a new
#: length field from silently escaping the integer and non-negative boundary suite.
LENGTH_COLUMNS_EXHAUSTIVE: tuple[tuple[str, str], ...] = (
    (BLOBS, "content_length_bytes"),
    (INTEGRITY, "observed_length_bytes"),
    (INTEGRITY, "expected_length_bytes"),
    (STAGED, "content_length_bytes"),
)


@pytest.mark.parametrize(("table", "column"), LENGTH_COLUMNS_EXHAUSTIVE)
def test_m2_29d_every_length_column_refuses_non_integer_and_negative_values(
    owned: Owned, table: str, column: str
) -> None:
    """Every byte length is a genuine non-negative SQLite integer, including both
    optional integrity-observation lengths rather than only the blob/staging pair.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    base_overrides: dict[str, object] = dict(UNIQUE_IDS[table])
    if table == STAGED:
        base_overrides.update(
            staging_outcome="failed",
            blob_workspace_id=None,
            blob_content_digest=None,
        )
    for value in (1.5, "not-an-integer", -1):
        with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
            write(owned, table, **{**base_overrides, column: value})
        assert counts(owned.connection) == before


@pytest.mark.parametrize("column", ("observed_length_bytes", "expected_length_bytes"))
def test_m2_29e_every_optional_integrity_length_admits_zero(
    owned: Owned, column: str
) -> None:
    """The two optional integrity lengths share the blob/staging zero boundary."""
    seed_chain(owned)
    write(owned, INTEGRITY, **UNIQUE_IDS[INTEGRITY], **{column: 0})
    stored = owned.connection.execute(
        f"SELECT {column} FROM {INTEGRITY} WHERE integrity_event_id = ?",
        (UNIQUE_IDS[INTEGRITY]["integrity_event_id"],),
    ).fetchone()
    assert stored == (0,)


# --- M2-30: every required wall-clock time column is a strictly positive integer --

#: One required, system-stamped `*_at_us` column per table -- every table except
#: `omnivia_evidence_event_references`, which stamps none of its own.
REQUIRED_POSITIVE_TIME_COLUMN: dict[str, str] = {
    BLOBS: "created_at_us",
    INTEGRITY: "checked_at_us",
    STAGED: "recorded_at_us",
    EVIDENCE: "ingested_at_us",
    LABELS: "recorded_at_us",
    PROVENANCE: "occurred_at_us",
    RECORDS: "recorded_at_us",
    SPANS: "recorded_at_us",
}

#: Exact time inventory. `event_at_us` and `observed_at_us` are signed historical
#: instants; every other stored instant is a positive system/source timestamp.
TIME_COLUMNS_EXHAUSTIVE: tuple[tuple[str, str], ...] = (
    (BLOBS, "created_at_us"),
    (BLOBS, "verified_at_us"),
    (INTEGRITY, "checked_at_us"),
    (STAGED, "recorded_at_us"),
    (EVIDENCE, "source_retrieved_at_us"),
    (EVIDENCE, "event_at_us"),
    (EVIDENCE, "observed_at_us"),
    (EVIDENCE, "ingested_at_us"),
    (EVIDENCE, "recorded_at_us"),
    (LABELS, "recorded_at_us"),
    (PROVENANCE, "occurred_at_us"),
    (REFERENCES, "source_retrieved_at_us"),
    (RECORDS, "recorded_at_us"),
    (SPANS, "recorded_at_us"),
)

SIGNED_HISTORICAL_TIME_COLUMNS = frozenset(
    {(EVIDENCE, "event_at_us"), (EVIDENCE, "observed_at_us")}
)
POSITIVE_TIME_COLUMNS_EXHAUSTIVE = tuple(
    item
    for item in TIME_COLUMNS_EXHAUSTIVE
    if item not in SIGNED_HISTORICAL_TIME_COLUMNS
)


@pytest.mark.parametrize("table", sorted(REQUIRED_POSITIVE_TIME_COLUMN))
def test_m2_30_every_required_wall_clock_time_column_refuses_zero_and_negative(
    owned: Owned, table: str
) -> None:
    """M2-30: every instant this system itself stamps is a strictly positive
    signed-64 microsecond integer -- `0` and `-1` are each refused on the one
    required, system-stamped `*_at_us` column of every table that has one.
    """
    column = REQUIRED_POSITIVE_TIME_COLUMN[table]
    seed_chain(owned)
    before = counts(owned.connection)

    for value in (0, -1):
        with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
            write(owned, table, **{**UNIQUE_IDS[table], column: value})
        assert counts(owned.connection) == before


@pytest.mark.parametrize(("table", "column"), TIME_COLUMNS_EXHAUSTIVE)
def test_m2_30b_every_time_column_refuses_non_integer_storage_classes(
    owned: Owned, table: str, column: str
) -> None:
    """Every time occurrence, optional or required, refuses REAL and TEXT values."""
    seed_chain(owned)
    before = counts(owned.connection)
    for value in (BASE_US + 0.5, "not-an-integer"):
        with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
            write(owned, table, **{**UNIQUE_IDS[table], column: value})
        assert counts(owned.connection) == before


@pytest.mark.parametrize(("table", "column"), POSITIVE_TIME_COLUMNS_EXHAUSTIVE)
def test_m2_30c_every_positive_time_column_refuses_zero_and_negative(
    owned: Owned, table: str, column: str
) -> None:
    """All positive timestamps are covered, including optional and second columns."""
    seed_chain(owned)
    before = counts(owned.connection)
    for value in (0, -1):
        with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
            write(owned, table, **{**UNIQUE_IDS[table], column: value})
        assert counts(owned.connection) == before


@pytest.mark.parametrize(("table", "column"), TIME_COLUMNS_EXHAUSTIVE)
def test_m2_30d_every_time_column_admits_the_signed_64_bit_upper_boundary(
    owned: Owned, table: str, column: str
) -> None:
    """No time occurrence narrows Core's signed-64 microsecond representation."""
    seed_chain(owned)
    overrides: dict[str, object] = {**UNIQUE_IDS[table], column: MAX_SIGNED_64}
    if table == BLOBS and column == "created_at_us":
        overrides["verified_at_us"] = MAX_SIGNED_64
    if table == EVIDENCE and column == "observed_at_us":
        overrides["ingested_at_us"] = MAX_SIGNED_64
    write(owned, table, **overrides)

    primary_key = next(iter(UNIQUE_IDS[table]))
    stored = owned.connection.execute(
        f"SELECT {column} FROM {table} WHERE {primary_key} = ?",
        (UNIQUE_IDS[table][primary_key],),
    ).fetchone()
    assert stored == (MAX_SIGNED_64,)


# --- M2-31: the sequence CHECK alone admits 1 and refuses non-positive ------------


def _minimal_chain_for(connection: sqlite3.Connection, table: str) -> None:
    """Insert only the FK prerequisites `table`'s own stream needs to exist --
    unlike `seed_chain_without_triggers`, nothing of `table` itself, so its own
    `sequence == 1` slot is still genuinely empty rather than already taken.
    """
    if table == INTEGRITY:
        return
    insert(connection, BLOBS, row_for(BLOBS))
    insert(connection, STAGED, row_for(STAGED))
    insert(connection, EVIDENCE, row_for(EVIDENCE))
    if table == REFERENCES:
        insert(connection, PROVENANCE, row_for(PROVENANCE))
    elif table == SPANS:
        insert(connection, RECORDS, row_for(RECORDS))


@pytest.mark.parametrize(
    ("table", "sequence_column"),
    [(table, sequence_column) for table, sequence_column, _parent in SEQUENCE_STREAMS],
)
def test_m2_31_the_sequence_check_alone_admits_one_and_refuses_non_positive(
    table: str, sequence_column: str
) -> None:
    """M2-31: M2-21 proves a stream's sequence must *advance*, under the trigger
    that carries both halves of that rule at once. With the trigger layer stripped
    away entirely (`replay_without_m2_triggers`), `1` -- every stream's default and
    the smallest positive integer -- still lands, and `0` and `-1` are still
    refused, by each table's own `> 0` CHECK alone.
    """
    connection = replay_without_m2_triggers()
    try:
        _minimal_chain_for(connection, table)

        insert(connection, table, row_for(table))
        assert count(connection, table) == 1

        for value in (0, -1):
            with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
                insert(
                    connection,
                    table,
                    row_for(table, **{**UNIQUE_IDS[table], sequence_column: value}),
                )
            assert count(connection, table) == 1
    finally:
        connection.close()


# --- M2-32: canonical JSON is bounded, and its digest is exact sha256 spelling ----

#: (table, json column, its digest column, max length) for every canonical-JSON pair
#: this migration stores.
CANONICAL_JSON_COLUMNS: tuple[tuple[str, str, str, int], ...] = (
    (STAGED, "original_metadata_json", "original_metadata_digest", 8192),
    (EVIDENCE, "original_metadata_json", "original_metadata_digest", 8192),
    (RECORDS, "content_json", "content_digest", 8192),
)


@pytest.mark.parametrize(
    ("table", "json_column", "digest_column", "max_length"), CANONICAL_JSON_COLUMNS
)
def test_m2_32_canonical_json_is_bounded_and_its_digest_is_exact_sha256_spelling(
    owned: Owned, table: str, json_column: str, digest_column: str, max_length: int
) -> None:
    """M2-32: the canonical JSON text is `1..8192` bytes -- empty and one byte past
    the bound are both refused -- and its own digest column carries the same exact
    `sha256:` + 64 lowercase hex spelling as every other internal digest; the exact
    upper boundary of the length itself still lands.
    """
    seed_chain(owned)
    before = counts(owned.connection)

    with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
        write(owned, table, **{**UNIQUE_IDS[table], json_column: ""})
    assert counts(owned.connection) == before

    with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
        write(
            owned,
            table,
            **{**UNIQUE_IDS[table], json_column: "x" * (max_length + 1)},
        )
    assert counts(owned.connection) == before

    with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
        write(
            owned,
            table,
            **{**UNIQUE_IDS[table], digest_column: "sha256:" + "g" * 64},
        )
    assert counts(owned.connection) == before

    write(owned, table, **{**UNIQUE_IDS[table], json_column: "x" * max_length})
    assert count(owned.connection, table) == 2


def test_m2_32b_canonical_json_storage_neither_parses_nor_recomputes_its_digest(
    owned: Owned,
) -> None:
    """M2-32: the CHECKs above police the canonical JSON column's type and bounds and
    its digest column's exact `sha256:` spelling, and nothing else. SQLite's own
    `json(...)` is deliberately never called here, so text that is not itself
    parseable JSON, or JSON that is well-formed but not Core canonical (out-of-order
    keys, incidental whitespace), still lands unchanged -- and no CHECK recomputes the
    digest from the stored bytes, so a well-formed `sha256:` value that is not
    actually that content's digest lands beside it unchanged too. Canonicalisation and
    digest computation happen above this layer; a CHECK that appeared to confirm
    either would be asserting something this layer cannot know.
    """
    seed_chain(owned)
    not_canonical_json = '{"b": 2, "a": 1}   '
    wrong_digest = "sha256:" + "f" * 64

    for table, json_column, digest_column, _max_length in CANONICAL_JSON_COLUMNS:
        pk_column, pk_value = next(iter(UNIQUE_IDS[table].items()))
        write(
            owned,
            table,
            **{
                **UNIQUE_IDS[table],
                json_column: not_canonical_json,
                digest_column: wrong_digest,
            },
        )
        stored = owned.connection.execute(
            f"SELECT {json_column}, {digest_column} FROM {table} WHERE {pk_column} = ?",
            (pk_value,),
        ).fetchone()
        assert stored == (not_canonical_json, wrong_digest)

    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


# --- M2-33: event_at_us/observed_at_us carry the full signed 64-bit domain --------


def test_m2_33_event_and_observed_at_us_carry_the_full_signed_64_bit_domain(
    owned: Owned,
) -> None:
    """M2-33: `event_at_us` and `observed_at_us` are the two deliberate exceptions to
    "times the system stamps are positive" -- a document or message predating 1970
    is an ordinary thing to capture evidence of. Both admit the full signed 64-bit
    domain, from `PRE_EPOCH_US` to `MAX_SIGNED_64`, and refuse only a value that is
    not a genuine integer: a fractional microsecond, or one large enough that SQLite
    itself can only store it as REAL, which is exactly what
    `typeof(...) = 'integer'` catches.
    """
    seed_chain(owned)

    write(
        owned,
        EVIDENCE,
        **UNIQUE_IDS[EVIDENCE],
        event_at_us=PRE_EPOCH_US,
        observed_at_us=PRE_EPOCH_US,
    )
    stored = owned.connection.execute(
        "SELECT event_at_us, observed_at_us FROM omnivia_evidence_artifacts "
        "WHERE evidence_id = ?",
        (UNIQUE_IDS[EVIDENCE]["evidence_id"],),
    ).fetchone()
    assert stored == (PRE_EPOCH_US, PRE_EPOCH_US)

    write(
        owned,
        EVIDENCE,
        evidence_id="evd-max-time",
        source_native_id="doc-max-time",
        event_at_us=MAX_SIGNED_64,
    )
    assert count(owned.connection, EVIDENCE) == 3

    before = counts(owned.connection)
    with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
        write(
            owned,
            EVIDENCE,
            evidence_id="evd-fractional-time",
            source_native_id="doc-fractional-time",
            event_at_us=BASE_US + 0.5,
        )
    assert counts(owned.connection) == before

    overflow_row = row_for(
        EVIDENCE,
        evidence_id="evd-overflow-time",
        source_native_id="doc-overflow-time",
        event_at_us=MAX_SIGNED_64 + 1,
    )
    with (
        pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        owned.connection.execute(insert_sql(EVIDENCE, overflow_row))
    assert counts(owned.connection) == before


# --- M2-34: verified staging is exact self-agreement, and nothing else is -------

#: (case id, overrides against `STAGED_DEFAULTS`, expected refusal or `None` for a
#: row that must land). Every negative case disturbs exactly one of the five facts a
#: `verified` staging must agree about at once -- `declared_checksum`,
#: `computed_checksum`, `blob_content_digest`, the blob's own workspace and its
#: `content_length_bytes` -- so each case isolates a single disagreement rather than
#: several at once. `computed_checksum` is the one fact that may simply be absent.
STAGED_VERIFIED_AGREEMENT_CASES: tuple[
    tuple[str, dict[str, object], str | None], ...
] = (
    ("exact_agreement", {}, None),
    ("computed_checksum_may_be_absent", {"computed_checksum": None}, None),
    (
        "blob_content_digest_disagrees_with_declared",
        {"blob_content_digest": DIGEST_B},
        "CHECK constraint failed",
    ),
    (
        "computed_checksum_disagrees_with_declared",
        {"computed_checksum": DIGEST_B},
        "CHECK constraint failed",
    ),
    (
        "blob_workspace_id_missing",
        {"blob_workspace_id": None},
        "CHECK constraint failed",
    ),
    (
        "content_length_disagrees_with_the_named_blob",
        {"content_length_bytes": 2048},
        "FOREIGN KEY constraint failed",
    ),
    (
        "named_blob_does_not_exist",
        {
            "declared_checksum": DIGEST_E,
            "computed_checksum": DIGEST_E,
            "blob_content_digest": DIGEST_E,
        },
        "FOREIGN KEY constraint failed",
    ),
)


@pytest.mark.parametrize(
    ("case", "overrides", "expected"),
    STAGED_VERIFIED_AGREEMENT_CASES,
    ids=[case for case, _overrides, _expected in STAGED_VERIFIED_AGREEMENT_CASES],
)
def test_m2_34_verified_staging_requires_an_existing_blob_and_exact_agreement(
    owned: Owned, case: str, overrides: dict[str, object], expected: str | None
) -> None:
    """M2-34: `verified` is a claim about five facts agreeing at once -- the named
    blob exists, `blob_content_digest` equals `declared_checksum`, an optional
    `computed_checksum` agrees with that same value, the blob's own workspace is
    named, and `content_length_bytes` is the length that exact blob was verified
    at. Disturbing any one of them alone is refused, whether by the table's own
    CHECK or, for the blob's own identity and length, by the foreign key beneath it.
    """
    seed_chain(owned)
    write(owned, BLOBS, content_digest=DIGEST_B)
    before = counts(owned.connection)
    ref = f"stg-{case}"

    if expected is None:
        write(owned, STAGED, staged_source_ref=ref, **overrides)
        assert count(owned.connection, STAGED) == before[STAGED] + 1
        stored = owned.connection.execute(
            "SELECT staging_outcome, blob_workspace_id, blob_content_digest "
            "FROM omnivia_staged_sources WHERE staged_source_ref = ?",
            (ref,),
        ).fetchone()
        assert stored == ("verified", WORKSPACE_ID, DIGEST_A)
    else:
        with pytest.raises(sqlite3.DatabaseError, match=expected):
            write(owned, STAGED, staged_source_ref=ref, **overrides)
        assert counts(owned.connection) == before


@pytest.mark.parametrize("outcome", STAGED_FAILURE_OUTCOMES)
def test_m2_34b_a_failed_staging_outcome_can_never_carry_accepted_blob_identity(
    owned: Owned, outcome: str
) -> None:
    """M2-34b: `digest_mismatch`, `missing_blob`, `unsupported` and `unsafe` are
    exactly as durable as `verified` -- the row lands -- but the same CHECK that
    demands blob identity for `verified` demands its absence for every other
    outcome. Naming a real, otherwise-valid blob from a failed staging is refused
    outright, not merely discouraged, and leaves no partial row behind.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    ref = f"stg-{outcome}-with-blob"

    with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
        write(
            owned,
            STAGED,
            staged_source_ref=ref,
            staging_outcome=outcome,
            blob_workspace_id=WORKSPACE_ID,
            blob_content_digest=DIGEST_A,
        )
    assert counts(owned.connection) == before

    # The same outcome, naming no blob at all, is exactly as durable as `verified`.
    write(
        owned,
        STAGED,
        staged_source_ref=ref,
        staging_outcome=outcome,
        blob_workspace_id=None,
        blob_content_digest=None,
    )
    assert count(owned.connection, STAGED) == before[STAGED] + 1


#: Exact accepted staged-source persistence boundary. Any additional column is a
#: schema-surface expansion and fails, regardless of how it is named.
STAGED_SOURCE_COLUMNS: tuple[str, ...] = (
    "staged_source_ref",
    "workspace_id",
    "source_kind",
    "declared_checksum",
    "content_length_bytes",
    "media_type",
    "source_version",
    "computed_checksum",
    "original_metadata_json",
    "original_metadata_digest",
    "staging_outcome",
    "blob_workspace_id",
    "blob_content_digest",
    "recorded_at_us",
)

#: Supplementary semantic tripwire. The exact inventory above is authoritative;
#: these fragments make failures for common forbidden concepts self-explanatory.
STAGED_FORBIDDEN_COLUMN_FRAGMENTS: tuple[str, ...] = (
    "path",
    "url",
    "uri",
    "archive",
    "inline",
    "payload",
    "credential",
    "principal",
    "token",
    "secret",
    "password",
    "selector",
    "locator",
    "engine",
    "connector",
    "config",
    "parser",
    "runtime",
    "runtime_option",
    "storage_option",
)


def test_m2_34c_the_staged_source_table_has_no_path_url_payload_or_selector_column(
    migrated: Path,
) -> None:
    """M2-34c: a staged descriptor is the accepted `ImportSourceDescriptor` boundary
    made structural -- no filesystem path, URL, inline or archive payload, no
    credential or bearer token, no workspace selector or parser selector, no
    runtime or storage option, and no storage-engine locator, because there is no
    column any of them could ever be written into. Checked against a fixed
    forbidden-name/fragment oracle rather than against a value the schema itself
    supplies, so a later column named for any one of these concepts fails this test
    instead of silently redefining what "no such column" means.
    """
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        columns = columns_of(connection, STAGED)
    finally:
        connection.close()

    assert columns == STAGED_SOURCE_COLUMNS
    for column in columns:
        lowered = column.lower()
        for fragment in STAGED_FORBIDDEN_COLUMN_FRAGMENTS:
            assert fragment not in lowered, (column, fragment)


# --- M2-35: evidence text-domain negatives beyond content_checksum/media_type ----

#: Every remaining `omnivia_evidence_artifacts` TEXT column that M2-17 and M2-25c do
#: not already exercise directly for *this* table -- an Identifier and an OpenCode
#: column each appear more than once across the nine tables, so this is the missing
#: "required, empty, overlength, malformed-pattern" set for the ones the two shared
#: sections above happened to reach through a different table instead.
EVIDENCE_TEXT_DOMAIN_NEGATIVE_CASES: tuple[tuple[str, dict[str, object], str], ...] = (
    # evidence_id: Identifier, required, its own primary key.
    ("evidence_id_required", {"evidence_id": None}, "NOT NULL constraint failed"),
    ("evidence_id_empty", {"evidence_id": ""}, "CHECK constraint failed"),
    ("evidence_id_overlength", {"evidence_id": "e" * 129}, "CHECK constraint failed"),
    ("evidence_id_whitespace", {"evidence_id": "evd 1"}, "CHECK constraint failed"),
    # source_native_id: Identifier, required.
    (
        "source_native_id_required",
        {**UNIQUE_IDS[EVIDENCE], "source_native_id": None},
        "NOT NULL constraint failed",
    ),
    (
        "source_native_id_empty",
        {**UNIQUE_IDS[EVIDENCE], "source_native_id": ""},
        "CHECK constraint failed",
    ),
    (
        "source_native_id_overlength",
        {**UNIQUE_IDS[EVIDENCE], "source_native_id": "d" * 129},
        "CHECK constraint failed",
    ),
    (
        "source_native_id_whitespace",
        {**UNIQUE_IDS[EVIDENCE], "source_native_id": "doc 2"},
        "CHECK constraint failed",
    ),
    # source_kind: OpenCode, required. `staged_source_ref` is cleared so the
    # ancestry trigger's own lookup -- which also compares `source_kind` and would
    # otherwise refuse first -- is not reached, exactly as M2-25c does for this same
    # column.
    (
        "source_kind_required",
        {**UNIQUE_IDS[EVIDENCE], "source_kind": None, "staged_source_ref": None},
        "NOT NULL constraint failed",
    ),
    (
        "source_kind_empty",
        {**UNIQUE_IDS[EVIDENCE], "source_kind": "", "staged_source_ref": None},
        "CHECK constraint failed",
    ),
    (
        "source_kind_overlength",
        {**UNIQUE_IDS[EVIDENCE], "source_kind": "a" * 129, "staged_source_ref": None},
        "CHECK constraint failed",
    ),
    (
        "source_kind_uppercase",
        {
            **UNIQUE_IDS[EVIDENCE],
            "source_kind": "Filesystem.archive",
            "staged_source_ref": None,
        },
        "CHECK constraint failed",
    ),
    # sensitivity / parser_status / ingestion_status: OpenCode, required.
    (
        "sensitivity_required",
        {**UNIQUE_IDS[EVIDENCE], "sensitivity": None},
        "NOT NULL constraint failed",
    ),
    (
        "sensitivity_uppercase",
        {**UNIQUE_IDS[EVIDENCE], "sensitivity": "Internal"},
        "CHECK constraint failed",
    ),
    (
        "parser_status_required",
        {**UNIQUE_IDS[EVIDENCE], "parser_status": None},
        "NOT NULL constraint failed",
    ),
    (
        "parser_status_uppercase",
        {**UNIQUE_IDS[EVIDENCE], "parser_status": "Parsed"},
        "CHECK constraint failed",
    ),
    (
        "ingestion_status_required",
        {**UNIQUE_IDS[EVIDENCE], "ingestion_status": None},
        "NOT NULL constraint failed",
    ),
    (
        "ingestion_status_uppercase",
        {**UNIQUE_IDS[EVIDENCE], "ingestion_status": "Ingested"},
        "CHECK constraint failed",
    ),
    # source_locator: optional, length-only 1..2048 -- present but out of bounds.
    (
        "source_locator_empty",
        {**UNIQUE_IDS[EVIDENCE], "source_locator": ""},
        "CHECK constraint failed",
    ),
    (
        "source_locator_overlength",
        {**UNIQUE_IDS[EVIDENCE], "source_locator": "x" * 2049},
        "CHECK constraint failed",
    ),
)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (overrides, expected)
        for _case, overrides, expected in EVIDENCE_TEXT_DOMAIN_NEGATIVE_CASES
    ],
    ids=[case for case, _overrides, _expected in EVIDENCE_TEXT_DOMAIN_NEGATIVE_CASES],
)
def test_m2_35_evidence_text_domains_refuse_required_empty_overlength_and_malformed(
    owned: Owned, overrides: dict[str, object], expected: str
) -> None:
    """M2-35: the `omnivia_evidence_artifacts` TEXT columns M2-17 and M2-25c do not
    already exercise directly -- `evidence_id`, `source_native_id`, `source_kind`,
    `sensitivity`, `parser_status`, `ingestion_status` and `source_locator` -- each
    refuse a missing required value, an empty string, one byte past its bound, or a
    malformed pattern, and no case leaves a partial row behind.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError), match=expected):
        write(owned, EVIDENCE, **overrides)
    assert counts(owned.connection) == before


# --- M2-35b: composite evidence foreign keys require the *same* workspace --------


def test_m2_35b_the_composite_evidence_foreign_keys_require_the_same_workspace() -> (
    None
):
    """M2-35: `omnivia_evidence_artifacts`' two composite foreign keys name the
    workspace alongside the digest and the staged-source ref, so a blob or a staged
    source that exists only in *another* workspace cannot be named, even though the
    digest or ref value alone is real. Executed under `replay_without_m2_triggers`,
    which admits a row bound to a workspace other than the singleton, so the refusal
    proved here is the declared foreign key's alone: under the ordinary fenced path
    a second workspace is refused outright before any foreign key is ever consulted,
    per M2-27.
    """
    connection = replay_without_m2_triggers()
    try:
        seed_chain_without_triggers(connection)

        insert(
            connection,
            BLOBS,
            row_for(BLOBS, workspace_id=OTHER_WORKSPACE_ID, content_digest=DIGEST_B),
        )
        before = count(connection, EVIDENCE)
        with pytest.raises(
            sqlite3.DatabaseError, match="FOREIGN KEY constraint failed"
        ):
            insert(
                connection,
                EVIDENCE,
                row_for(
                    EVIDENCE,
                    evidence_id="evd-cross-ws-blob",
                    source_native_id="doc-cross-ws-blob",
                    blob_content_digest=DIGEST_B,
                    staged_source_ref=None,
                ),
            )
        assert count(connection, EVIDENCE) == before

        insert(
            connection,
            BLOBS,
            row_for(BLOBS, workspace_id=OTHER_WORKSPACE_ID, content_digest=DIGEST_A),
        )
        insert(
            connection,
            STAGED,
            row_for(
                STAGED,
                workspace_id=OTHER_WORKSPACE_ID,
                staged_source_ref="stg-other-ws",
                blob_workspace_id=OTHER_WORKSPACE_ID,
                blob_content_digest=DIGEST_A,
            ),
        )
        with pytest.raises(
            sqlite3.DatabaseError, match="FOREIGN KEY constraint failed"
        ):
            insert(
                connection,
                EVIDENCE,
                row_for(
                    EVIDENCE,
                    evidence_id="evd-cross-ws-staged",
                    source_native_id="doc-cross-ws-staged",
                    staged_source_ref="stg-other-ws",
                ),
            )
        assert count(connection, EVIDENCE) == before
    finally:
        connection.close()


# --- M2-35c: two further malformed checksum/media-type shapes, and coexistence ---


CONTENT_CHECKSUM_ADDITIONAL_NEGATIVE_CASES: tuple[
    tuple[str, dict[str, object]], ...
] = (
    ("checksum_no_colon", {"content_checksum": "sha256abc"}),
    ("checksum_only_colon", {"content_checksum": ":"}),
    ("media_type_triple_slash", {"media_type": "text/plain/extra/thing"}),
)


@pytest.mark.parametrize(
    ("overrides",),
    [(overrides,) for _case, overrides in CONTENT_CHECKSUM_ADDITIONAL_NEGATIVE_CASES],
    ids=[case for case, _overrides in CONTENT_CHECKSUM_ADDITIONAL_NEGATIVE_CASES],
)
def test_m2_35c_further_malformed_checksum_and_media_type_shapes_are_refused(
    owned: Owned, overrides: dict[str, object]
) -> None:
    """M2-35: two shapes M2-17 does not already enumerate -- an `EvidenceChecksum`
    with no colon at all, one that is nothing but a colon, and a `MediaType` with
    three slashes instead of one -- are refused exactly as every other malformed
    shape in that domain is, and leave no partial row behind.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    with pytest.raises(sqlite3.DatabaseError, match="CHECK constraint failed"):
        write(owned, EVIDENCE, **UNIQUE_IDS[EVIDENCE], **overrides)
    assert counts(owned.connection) == before


def test_m2_35d_a_provider_neutral_checksum_coexists_with_a_distinct_internal_digest(
    owned: Owned,
) -> None:
    """M2-35: the public `content_checksum` and the internal `blob_content_digest`
    are two separate domains that never substitute for each other -- a provider
    publishing a non-SHA checksum is still storable, and it coexists in the same row
    with the exact `sha256:` address of the bytes this workspace actually verified,
    the two staying visibly distinct rather than collapsed into one value.
    """
    seed_chain(owned)
    write(
        owned,
        EVIDENCE,
        **UNIQUE_IDS[EVIDENCE],
        content_checksum=PROVIDER_CHECKSUM,
        blob_content_digest=DIGEST_A,
    )
    stored = owned.connection.execute(
        "SELECT content_checksum, blob_content_digest FROM omnivia_evidence_artifacts "
        "WHERE evidence_id = ?",
        (UNIQUE_IDS[EVIDENCE]["evidence_id"],),
    ).fetchone()
    assert stored == (PROVIDER_CHECKSUM, DIGEST_A)
    assert stored[0] != stored[1]


# --- M2-35e/f: import_run_id accepts only an existing ingestion.import job -------


IMPORT_RUN_ID_NEGATIVE_CASES: tuple[tuple[str, str], ...] = (
    ("nonexistent_job", "job-does-not-exist-0001"),
    ("existing_wrong_type_job", OTHER_JOB_ID),
)


@pytest.mark.parametrize(
    ("case", "import_run_id"),
    IMPORT_RUN_ID_NEGATIVE_CASES,
    ids=[case for case, _job in IMPORT_RUN_ID_NEGATIVE_CASES],
)
def test_m2_35e_import_run_id_refuses_a_nonexistent_or_wrong_type_durable_job(
    owned: Owned, case: str, import_run_id: str
) -> None:
    """M2-35: `import_run_id` refuses a job id that names nothing at all and one
    that names a real, existing job of the wrong type alike -- a foreign key alone
    can only prove the job exists, and it is the INSERT trigger that proves what
    kind of work it was, exactly as M2-16's docstring for the reopened durable-jobs
    guard describes.
    """
    del case
    seed_chain(owned)
    before = counts(owned.connection)
    with pytest.raises(
        sqlite3.DatabaseError,
        match="must name an existing ingestion.import durable job",
    ):
        write(owned, EVIDENCE, **UNIQUE_IDS[EVIDENCE], import_run_id=import_run_id)
    assert counts(owned.connection) == before


def test_m2_35f_import_run_id_accepts_an_existing_ingestion_import_job(
    owned: Owned,
) -> None:
    """M2-35: the one job type `import_run_id` accepts lands exactly as durably as
    any other satisfied ancestry column, naming the real job. M2-16 already proves
    that job's type cannot later be retagged out from under this reference; nothing
    here disturbs that invariant.
    """
    seed_chain(owned)
    write(
        owned,
        EVIDENCE,
        evidence_id="evd-import-run-accept",
        source_native_id="doc-import-run-accept",
        import_run_id=IMPORT_JOB_ID,
    )
    stored = owned.connection.execute(
        "SELECT import_run_id FROM omnivia_evidence_artifacts WHERE evidence_id = ?",
        ("evd-import-run-accept",),
    ).fetchone()
    assert stored == (IMPORT_JOB_ID,)


# --- M2-36: shared blob bytes do not merge two distinct source/evidence identities


def test_m2_36_shared_blob_bytes_do_not_merge_two_distinct_source_evidence_identities(
    owned: Owned,
) -> None:
    """M2-36: two staged sources and two evidence artifacts that verify against the
    exact same content-addressed blob stay two identities everywhere else. Sharing
    `content_digest` is the point of `omnivia_blob_objects` -- dedup is a property of
    content, not a merge of the things that reference it -- and this proves the
    sharing stops at the blob: distinct staged-source and evidence metadata, distinct
    sensitivity, and independent permission-label and provenance sequences all
    survive naming the same bytes, and a permission event stays attached to its exact
    evidence identity rather than to the digest both identities share.
    """
    shared_digest = DIGEST_A
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(owned.connection, BLOBS, row_for(BLOBS))
        insert(
            owned.connection,
            STAGED,
            row_for(
                STAGED,
                staged_source_ref="stg-shared-a",
                source_kind="filesystem.archive",
                original_metadata_json='{"kind":"archive-a"}',
                original_metadata_digest=DIGEST_C,
            ),
        )
        insert(
            owned.connection,
            STAGED,
            row_for(
                STAGED,
                staged_source_ref="stg-shared-b",
                source_kind="web.page",
                media_type="text/html",
                original_metadata_json='{"kind":"webpage-b"}',
                original_metadata_digest=DIGEST_E,
            ),
        )
        insert(
            owned.connection,
            EVIDENCE,
            row_for(
                EVIDENCE,
                evidence_id="evd-shared-a",
                source_kind="filesystem.archive",
                source_native_id="doc-shared-a",
                source_locator="archive://doc-a.md",
                staged_source_ref="stg-shared-a",
                original_metadata_json='{"title":"doc-a"}',
                original_metadata_digest=DIGEST_C,
                sensitivity="internal",
                parser_status="parsed",
                ingestion_status="ingested",
            ),
        )
        insert(
            owned.connection,
            EVIDENCE,
            row_for(
                EVIDENCE,
                evidence_id="evd-shared-b",
                source_kind="web.page",
                source_native_id="doc-shared-b",
                source_locator="https://example.test/b",
                media_type="text/html",
                staged_source_ref="stg-shared-b",
                original_metadata_json='{"title":"doc-b"}',
                original_metadata_digest=DIGEST_E,
                sensitivity="restricted",
                parser_status="pending",
                ingestion_status="staged",
            ),
        )
        insert(
            owned.connection,
            LABELS,
            row_for(
                LABELS,
                label_event_id="lbl-shared-a-1",
                evidence_id="evd-shared-a",
                label_sequence=1,
                label_action="attached",
                permission_label="group.engineering",
            ),
        )
        insert(
            owned.connection,
            LABELS,
            row_for(
                LABELS,
                label_event_id="lbl-shared-a-2",
                evidence_id="evd-shared-a",
                label_sequence=2,
                label_action="withdrawn",
                permission_label="group.engineering",
                recorded_at_us=BASE_US + 60,
            ),
        )
        insert(
            owned.connection,
            LABELS,
            row_for(
                LABELS,
                label_event_id="lbl-shared-b-1",
                evidence_id="evd-shared-b",
                label_sequence=1,
                label_action="attached",
                permission_label="group.legal",
                recorded_at_us=BASE_US + 61,
            ),
        )
        insert(
            owned.connection,
            PROVENANCE,
            row_for(
                PROVENANCE,
                provenance_event_id="prv-shared-a-1",
                evidence_id="evd-shared-a",
                provenance_sequence=1,
                actor_id="actor-a",
                action="captured",
                source_kind="filesystem.archive",
                source_native_id="doc-shared-a",
            ),
        )
        insert(
            owned.connection,
            PROVENANCE,
            row_for(
                PROVENANCE,
                provenance_event_id="prv-shared-b-1",
                evidence_id="evd-shared-b",
                provenance_sequence=1,
                actor_id="actor-b",
                action="captured",
                source_kind="web.page",
                source_native_id="doc-shared-b",
                occurred_at_us=BASE_US + 70,
            ),
        )

    # The blob identity is the point of sharing: one row for the digest both
    # identities verified against.
    assert owned.connection.execute(
        "SELECT workspace_id, content_digest FROM omnivia_blob_objects "
        "WHERE content_digest = ?",
        (shared_digest,),
    ).fetchall() == [(WORKSPACE_ID, shared_digest)]

    staged_rows = {
        row[0]: row[1:]
        for row in owned.connection.execute(
            "SELECT staged_source_ref, source_kind, original_metadata_json, "
            "original_metadata_digest, blob_content_digest "
            "FROM omnivia_staged_sources WHERE staged_source_ref IN (?, ?)",
            ("stg-shared-a", "stg-shared-b"),
        ).fetchall()
    }
    assert staged_rows == {
        "stg-shared-a": (
            "filesystem.archive",
            '{"kind":"archive-a"}',
            DIGEST_C,
            shared_digest,
        ),
        "stg-shared-b": (
            "web.page",
            '{"kind":"webpage-b"}',
            DIGEST_E,
            shared_digest,
        ),
    }

    evidence_rows = {
        row[0]: row[1:]
        for row in owned.connection.execute(
            "SELECT evidence_id, source_kind, source_native_id, "
            "original_metadata_json, sensitivity, blob_content_digest "
            "FROM omnivia_evidence_artifacts WHERE evidence_id IN (?, ?)",
            ("evd-shared-a", "evd-shared-b"),
        ).fetchall()
    }
    assert evidence_rows == {
        "evd-shared-a": (
            "filesystem.archive",
            "doc-shared-a",
            '{"title":"doc-a"}',
            "internal",
            shared_digest,
        ),
        "evd-shared-b": (
            "web.page",
            "doc-shared-b",
            '{"title":"doc-b"}',
            "restricted",
            shared_digest,
        ),
    }
    # Both artifacts name the exact same blob -- the shared bytes -- while every
    # other column above stayed distinct per identity.
    assert evidence_rows["evd-shared-a"][-1] == evidence_rows["evd-shared-b"][-1]

    labels_by_evidence: dict[str, list[tuple[int, str, str]]] = {}
    for evidence_id, sequence, action, label in owned.connection.execute(
        "SELECT evidence_id, label_sequence, label_action, permission_label "
        "FROM omnivia_evidence_permission_labels "
        "WHERE evidence_id IN (?, ?) ORDER BY evidence_id, label_sequence",
        ("evd-shared-a", "evd-shared-b"),
    ).fetchall():
        labels_by_evidence.setdefault(evidence_id, []).append((sequence, action, label))
    assert labels_by_evidence == {
        "evd-shared-a": [
            (1, "attached", "group.engineering"),
            (2, "withdrawn", "group.engineering"),
        ],
        "evd-shared-b": [(1, "attached", "group.legal")],
    }

    provenance_by_evidence: dict[str, list[tuple[int, str, str]]] = {}
    for evidence_id, sequence, actor_id, action in owned.connection.execute(
        "SELECT evidence_id, provenance_sequence, actor_id, action "
        "FROM omnivia_evidence_provenance_events "
        "WHERE evidence_id IN (?, ?) ORDER BY evidence_id, provenance_sequence",
        ("evd-shared-a", "evd-shared-b"),
    ).fetchall():
        provenance_by_evidence.setdefault(evidence_id, []).append(
            (sequence, actor_id, action)
        )
    assert provenance_by_evidence == {
        "evd-shared-a": [(1, "actor-a", "captured")],
        "evd-shared-b": [(1, "actor-b", "captured")],
    }

    # Permission events stay attached to their exact evidence identity even though
    # `blob_content_digest` is shared: joining every label through the shared digest
    # still resolves each one back to exactly one evidence row, never both at once.
    joined = owned.connection.execute(
        "SELECT l.evidence_id, e.blob_content_digest FROM "
        "omnivia_evidence_permission_labels l "
        "JOIN omnivia_evidence_artifacts e ON e.evidence_id = l.evidence_id "
        "WHERE e.blob_content_digest = ? ORDER BY l.evidence_id, l.label_sequence",
        (shared_digest,),
    ).fetchall()
    assert joined == [
        ("evd-shared-a", shared_digest),
        ("evd-shared-a", shared_digest),
        ("evd-shared-b", shared_digest),
    ]
    assert {evidence_id for evidence_id, _digest in joined} == {
        "evd-shared-a",
        "evd-shared-b",
    }


# --- M2-37: provenance events and references cannot cross evidence, source or ----
# --- parent-event identity -------------------------------------------------------


def _seed_two_provenance_chains(owned: Owned) -> None:
    """`evd-0001`/`prv-0001`/`ref-0001` from `seed_chain`, declaring
    `filesystem.archive`/`doc-1`, plus a second, independently coherent chain --
    `evd-second`/`prv-second`/`ref-second` -- declaring the distinct `web.page`/
    `doc-2` against `DIGEST_B`. A negative case that substitutes one chain's
    evidence, source or parent-event identity into the other is then provably
    crossing a real fence rather than two values that already happened to agree.
    """
    seed_chain(owned)
    write(owned, BLOBS, content_digest=DIGEST_B)
    write(
        owned,
        EVIDENCE,
        evidence_id="evd-second",
        source_kind="web.page",
        source_native_id="doc-2",
        source_locator="https://example.test/b",
        media_type="text/html",
        blob_content_digest=DIGEST_B,
        staged_source_ref=None,
    )
    write(
        owned,
        PROVENANCE,
        provenance_event_id="prv-second",
        evidence_id="evd-second",
        provenance_sequence=1,
        actor_id="actor-b",
        source_kind="web.page",
        source_native_id="doc-2",
    )
    write(
        owned,
        REFERENCES,
        event_reference_id="ref-second",
        provenance_event_id="prv-second",
        evidence_id="evd-second",
        reference_ordinal=1,
        source_kind="web.page",
        source_native_id="doc-2",
    )


#: (case id, table, overrides, expected error substring), run against
#: `_seed_two_provenance_chains`. A provenance event that borrows another chain's
#: source identity for a real evidence id, and a reference that borrows another
#: chain's evidence, source or parent provenance event, are each refused -- and
#: refused without inserting anything, since the composite foreign keys below name
#: the full tuple the parent row must already have proven true, not any subset of it.
PROVENANCE_ISOLATION_NEGATIVE_CASES: tuple[
    tuple[str, str, dict[str, object], str], ...
] = (
    (
        "provenance_cross_evidence_source_substitution",
        PROVENANCE,
        {
            "provenance_event_id": "prv-cross",
            "evidence_id": "evd-second",
            "provenance_sequence": 2,
            "source_kind": "filesystem.archive",
            "source_native_id": "doc-1",
        },
        "FOREIGN KEY constraint failed",
    ),
    (
        "reference_cross_evidence_substitution",
        REFERENCES,
        {
            "event_reference_id": "ref-cross-evidence",
            "provenance_event_id": "prv-second",
            "evidence_id": "evd-0001",
            "reference_ordinal": 2,
            "source_kind": "filesystem.archive",
            "source_native_id": "doc-1",
        },
        "FOREIGN KEY constraint failed",
    ),
    (
        "reference_cross_source_substitution",
        REFERENCES,
        {
            "event_reference_id": "ref-cross-source",
            "provenance_event_id": "prv-second",
            "evidence_id": "evd-second",
            "reference_ordinal": 2,
            "source_kind": "filesystem.archive",
            "source_native_id": "doc-1",
        },
        "FOREIGN KEY constraint failed",
    ),
    (
        "reference_cross_parent_event_substitution",
        REFERENCES,
        {
            "event_reference_id": "ref-cross-parent",
            "provenance_event_id": "prv-0001",
            "evidence_id": "evd-second",
            "reference_ordinal": 2,
            "source_kind": "web.page",
            "source_native_id": "doc-2",
        },
        "FOREIGN KEY constraint failed",
    ),
)


@pytest.mark.parametrize(
    ("table", "overrides", "expected"),
    [
        (table, overrides, expected)
        for _case, table, overrides, expected in PROVENANCE_ISOLATION_NEGATIVE_CASES
    ],
    ids=[
        case
        for case, _table, _overrides, _expected in PROVENANCE_ISOLATION_NEGATIVE_CASES
    ],
)
def test_m2_37_provenance_events_and_references_are_refused_without_partial_rows(
    owned: Owned, table: str, overrides: dict[str, object], expected: str
) -> None:
    """M2-37: a provenance event that names a real evidence id but a source identity
    borrowed from a different chain, a reference that names a real parent event but a
    different chain's evidence, a reference that names its own real event and
    evidence but a different chain's source, and a reference that names its own real
    evidence and source but a different chain's parent event, are each refused -- and
    each refusal leaves every table exactly as populated as it was before the
    attempt. Each composite foreign key names the whole tuple its parent row must
    already agree on, so matching three columns out of four is never enough.
    """
    _seed_two_provenance_chains(owned)
    before = counts(owned.connection)

    with pytest.raises(sqlite3.DatabaseError, match=expected):
        write(owned, table, **overrides)

    assert counts(owned.connection) == before


# --- M2-38: one fenced transaction, all nine tables, rolled back whole ----------


def test_m2_38_a_fenced_transaction_that_raises_before_commit_rolls_back_completely(
    owned: Owned,
) -> None:
    """M2-38: a second, valid-looking row in every one of the nine M2 tables, all
    inserted in one `fenced_transaction`, then a deliberate failure before COMMIT.

    `fenced_transaction` rolls back on any exception and re-raises it unchanged, so
    this is the M2 slice's evidence that the rollback is whole: not a partial write
    landing in some prefix of the nine tables, and not a `durable_jobs` row this
    slice's writer touched along the way. Both the aggregate counts and each
    attempted row's own identity are checked, so a rollback that dropped eight
    tables' rows but left one behind would still be caught.
    """
    seed_chain(owned)
    before = counts(owned.connection)
    before_jobs = count(owned.connection, DURABLE_JOBS)

    with (
        pytest.raises(MigrationInterrupted),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        for table in M2_TABLES:
            insert(owned.connection, table, unique_row_for(table))
        raise MigrationInterrupted("deliberate failure before commit")

    assert counts(owned.connection) == before
    assert count(owned.connection, DURABLE_JOBS) == before_jobs
    for table in M2_TABLES:
        column, value = next(iter(UNIQUE_IDS[table].items()))
        remaining = owned.connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (value,)
        ).fetchone()[0]
        assert remaining == 0, table


# --- M2-39: a stock SQLite process, valid-looking rows, all nine tables --------


@pytest.mark.parametrize("table", M2_TABLES)
def test_m2_39_a_stock_sqlite_process_cannot_insert_a_valid_looking_row_into_any_m2_table(
    migrated: Path, table: str
) -> None:
    """M2-39: a row built the same way `write` builds one -- `unique_row_for`,
    rendered through `insert_sql` rather than parameter binding, since a stock
    client speaks only literal SQL -- attempted by a real OS process that never
    imported OmniVia and holds no connection authority.

    The Runtime connection is closed on purpose, and *only* closed: no
    `close_guard`, so the persisted guard row and the lease it was opened under are
    exactly the pair a crashed service instance leaves behind, and they still agree
    with each other. That is the state in which an ordinary `sqlite3` client used to
    be able to walk in and write; every M2 table's count and the durable-jobs count
    are captured before the attempt and checked again after.
    """
    holder = take_ownership(migrated)
    try:
        seed_chain(holder)
        before = counts(holder.connection)
        before_jobs = count(holder.connection, DURABLE_JOBS)
    finally:
        holder.connection.close()

    persisted = open_database(migrated, OpenMode.READ_ONLY)
    try:
        guard = persisted.execute(
            "SELECT COUNT(*) FROM omnivia_mutation_guard g "
            "JOIN omnivia_workspace_state s ON s.singleton = 1 "
            "JOIN omnivia_workspace_lease l ON l.singleton = 1 "
            "WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation "
            "AND l.service_instance_id = g.service_instance_id "
            "AND l.lifecycle = 'held'"
        ).fetchone()
        assert guard is not None and guard[0] == 1, "the persisted rows must look valid"
    finally:
        persisted.close()

    statement = insert_sql(table, unique_row_for(table))
    report = run_child(
        STOCK_SQLITE_CHILD, str(migrated), statement, drop_pythonpath=True
    )
    assert report["omnivia_modules"] == [], report
    assert report["succeeded"] is False, report
    assert re.search(REFUSED_EXTERNAL_WRITE, str(report["error"]), re.IGNORECASE), (
        report
    )

    after = open_database(migrated, OpenMode.READ_ONLY)
    try:
        assert counts(after) == before
        assert count(after, DURABLE_JOBS) == before_jobs
    finally:
        after.close()


# --- M2-40 … M2-41: interruption after every statement, and fresh-process retry --


@pytest.mark.parametrize("stop_after", list(range(1, len(MIGRATION_STATEMENTS) + 1)))
def test_m2_40_m2_41_failure_after_every_statement_converges_exactly_once(
    tmp_path: Path, stop_after: int
) -> None:
    """M2-40/41: interrupt after each of 0008's 57 statements; retry lands it once.

    After the injected failure nothing of M2 may be observable: no v8 ledger row,
    `user_version` still 7, all nine tables, nineteen indexes and twenty-seven
    triggers absent, the inherited `durable_jobs` guard exactly as `0007` left it --
    including statements 56 and 57, where the live database briefly has no guard at
    all between the `DROP TRIGGER` and the `CREATE TRIGGER` that reopens it -- and
    the pre-existing fingerprint and legacy inventory undisturbed. A genuinely fresh
    process then applies `0008` exactly once, and the attempt each half left behind
    survives untouched by the other: the failed row keeps the injected error, and a
    distinct succeeded row is added beside it.
    """
    path = tmp_path / "interrupted.sqlite"
    materialise_phase0_baseline(path)
    generation = _apply_through_predecessor(path)

    before_legacy = legacy_inventory(path)
    snapshot = open_database(path, OpenMode.READ_ONLY)
    try:
        before_fingerprint = fingerprint_schema(snapshot)
        before_ledger = applied_migrations(snapshot)
        before_user_version = read_user_version(snapshot)
        before_guard_sql = object_sql(snapshot, INHERITED_JOB_GUARD)
    finally:
        snapshot.close()
    assert MIGRATION_VERSION not in before_ledger
    assert before_user_version == MIGRATION_VERSION - 1

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        crashing = FailAfterStatement(connection, MIGRATION_STATEMENTS, stop_after)
        with (
            pytest.raises(MigrationInterrupted, match=f"statement {stop_after}$"),
            migration_catalogue_through(MIGRATION_VERSION),
        ):
            apply_pending_migrations(
                cast("sqlite3.Connection", crashing),
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=SERVICE_INSTANCE,
                fencing_generation=generation,
                workspace_id=WORKSPACE_ID,
            )
        assert crashing.executed == stop_after
    finally:
        connection.close()

    interrupted = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        ledger = applied_migrations(interrupted)
        assert MIGRATION_VERSION not in ledger, ledger
        assert read_user_version(interrupted) == MIGRATION_VERSION - 1

        tables = object_names(interrupted, "table")
        indexes = object_names(interrupted, "index")
        triggers = object_names(interrupted, "trigger")
        assert tables.isdisjoint(M2_TABLES), sorted(tables & set(M2_TABLES))
        assert indexes.isdisjoint(M2_INDEXES), sorted(indexes & set(M2_INDEXES))
        assert triggers.isdisjoint(M2_TRIGGERS), sorted(triggers & set(M2_TRIGGERS))

        assert object_sql(interrupted, INHERITED_JOB_GUARD) == before_guard_sql
        assert fingerprint_schema(interrupted).matches(before_fingerprint)
        assert integrity_check(interrupted) == []
        assert foreign_key_check(interrupted) == []

        failed = migration_attempts(interrupted)
        assert [row[0] for row in failed] == ["failed"], failed
        _outcome, started_at, finished_at, detail = failed[0]
        assert started_at
        assert finished_at
        assert detail is not None and f"statement {stop_after}" in detail, failed
    finally:
        interrupted.close()

    assert legacy_inventory(path) == before_legacy

    report = run_child(RETRY_CHILD, str(path), WORKSPACE_ID, SERVICE_INSTANCE)
    assert report["ok"] is True, report
    assert report["applied"] == [MIGRATION_VERSION], report
    assert report["ledger"] == list(range(1, MIGRATION_VERSION + 1)), report
    assert report["ledger_rows_for_8"] == 1, report
    complete = set(M2_TABLES) | set(M2_INDEXES) | set(M2_TRIGGERS)
    assert complete <= set(report["objects"]), report
    assert INHERITED_JOB_GUARD in report["objects"], report
    assert report["integrity"] == [], report
    assert report["foreign_keys"] == [], report
    assert legacy_inventory(path) == before_legacy

    settled = open_database(path, OpenMode.READ_ONLY)
    try:
        with migration_catalogue_through(MIGRATION_VERSION):
            assert fingerprint_schema(settled).matches(canonical_schema_fingerprint())
        attempts = migration_attempts(settled)
        assert sorted(row[0] for row in attempts) == ["failed", "succeeded"], attempts
        preserved = next(row for row in attempts if row[0] == "failed")
        assert preserved[3] is not None and f"statement {stop_after}" in preserved[3], (
            attempts
        )
        succeeded = next(row for row in attempts if row[0] == "succeeded")
        assert succeeded[1] and succeeded[2]
    finally:
        settled.close()


# --- M2-42 … M2-42b: backup and rollback restore the exact pre-0008 workspace ---


@pytest.fixture
def adopted_with_m2_backup(tmp_path: Path) -> tuple[Path, Any, dict[str, Any]]:
    """An adopted workspace, a verified pre-0008 backup, then 0008 applied and seeded.

    The backup is taken and the pre-0008 state is captured before `0008` exists, then
    `0008` is applied under the real migrator and one coherent M2 row is seeded under
    current authority, so a rollback restoring the pre-0008 state is a claim about
    undoing both the schema change and the rows it made possible -- not just an
    unused schema nobody ever wrote to.
    """
    path = tmp_path / "adopted.sqlite"
    materialise_phase0_baseline(path)
    populate_legacy_corpus(path)
    generation = _apply_through_predecessor(path)

    installation = InstallationLayout(root=tmp_path / "installation-state")
    installation.create(WORKSPACE_ID)
    backup = create_verified_backup(
        path, installation, workspace_id=WORKSPACE_ID, attempt_id=new_attempt_id()
    )
    assert backup.verified

    before = open_database(path, OpenMode.READ_ONLY)
    try:
        pre = {
            "fingerprint": fingerprint_schema(before),
            "inventory": capture_inventory(before),
            "tables": table_names(before),
            "ledger": applied_migrations(before),
            "user_version": read_user_version(before),
            "state": read_workspace_state(before),
            "legacy": legacy_inventory(path),
            "guard_sql": object_sql(before, INHERITED_JOB_GUARD),
        }
    finally:
        before.close()

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        with migration_catalogue_through(MIGRATION_VERSION):
            applied = apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=SERVICE_INSTANCE,
                fencing_generation=generation,
                workspace_id=WORKSPACE_ID,
            )
        assert [migration.version for migration in applied] == [MIGRATION_VERSION]
    finally:
        connection.close()

    holder = take_ownership(path)
    try:
        seed_chain(holder)
    finally:
        holder.connection.close()

    return path, backup, pre


def _inventory_of(path: Path) -> Any:
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        return capture_inventory(connection)
    finally:
        connection.close()


def test_m2_42_rollback_restores_the_exact_pre_0008_workspace(
    adopted_with_m2_backup: tuple[Path, Any, dict[str, Any]],
) -> None:
    """M2-42: schema, rows, ledger, guard and workspace identity, all exactly as
    before `0008` -- including the durable-jobs guard `0008` reopened, which must
    come back exactly as `0007` left it rather than as whatever `0008` recreated.
    """
    path, backup, pre = adopted_with_m2_backup

    migrated = open_database(path, OpenMode.READ_ONLY)
    try:
        assert set(M2_TABLES) <= set(table_names(migrated))
        assert MIGRATION_VERSION in applied_migrations(migrated)
        assert count(migrated, BLOBS) == 1
    finally:
        migrated.close()

    restored = rollback_migration(backup, path)
    connection = open_database(restored, OpenMode.READ_ONLY)
    try:
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert fingerprint_schema(connection).matches(pre["fingerprint"])
        assert table_names(connection) == pre["tables"]
        assert set(table_names(connection)).isdisjoint(M2_TABLES)
        assert object_names(connection, "index").isdisjoint(M2_INDEXES)
        assert object_names(connection, "trigger").isdisjoint(M2_TRIGGERS)
        assert object_sql(connection, INHERITED_JOB_GUARD) == pre["guard_sql"]
        assert applied_migrations(connection) == pre["ledger"]
        assert MIGRATION_VERSION not in applied_migrations(connection)
        assert read_user_version(connection) == pre["user_version"]
        assert read_workspace_state(connection) == pre["state"]
        assert migration_attempts(connection, version=MIGRATION_VERSION) == []
    finally:
        connection.close()
    assert legacy_inventory(restored) == pre["legacy"]
    assert compare_inventories(backup.source_inventory, _inventory_of(restored)) == []
    assert compare_inventories(pre["inventory"], _inventory_of(restored)) == []


#: Bytes deliberately left in the replaced database's sidecars. Not a valid WAL
#: header, so if a restore ever did adopt them the next open would fail loudly rather
#: than quietly -- and either way they must not survive into the restored state.
STALE_SIDECAR_BYTES = b"stale frames that must never be replayed"


def assert_no_stale_frames(sidecars: list[Path]) -> None:
    """No sidecar beside the restored database carries a byte of the stale ones."""
    for sidecar in sidecars:
        if sidecar.exists():
            assert STALE_SIDECAR_BYTES not in sidecar.read_bytes(), sidecar


def test_m2_42b_stale_wal_and_shm_cannot_replay_over_a_restore(
    adopted_with_m2_backup: tuple[Path, Any, dict[str, Any]],
) -> None:
    """M2-42b: sidecars belonging to the replaced database are discarded, not reused.

    The sentinel is written to both sidecars only once every connection the fixture
    opened is closed, so what follows is a claim about files left on disk between
    processes -- not about a live WAL a connection still holds open. A restore drops
    the replaced database's `-wal` and `-shm` before putting the verified backup in
    their place, and SQLite may recreate an empty pair the moment the restored file
    is opened, including by the read-only verification `rollback_migration` does
    before it returns and by every later read-only open below; that recreation is
    fine. What must never happen is the sentinel surviving into a sidecar or being
    replayed into the restored state, on the first open or the third.
    """
    path, backup, pre = adopted_with_m2_backup
    sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]
    for sidecar in sidecars:
        sidecar.write_bytes(STALE_SIDECAR_BYTES)

    restored = rollback_migration(backup, path)
    assert_no_stale_frames(sidecars)

    for _ in range(3):
        connection = open_database(restored, OpenMode.READ_ONLY)
        try:
            assert integrity_check(connection) == []
            assert foreign_key_check(connection) == []
            assert fingerprint_schema(connection).matches(pre["fingerprint"])
            assert table_names(connection) == pre["tables"]
            assert set(table_names(connection)).isdisjoint(M2_TABLES)
            assert object_sql(connection, INHERITED_JOB_GUARD) == pre["guard_sql"]
            assert applied_migrations(connection) == pre["ledger"]
            assert read_user_version(connection) == pre["user_version"]
            assert read_workspace_state(connection) == pre["state"]
        finally:
            connection.close()
        assert_no_stale_frames(sidecars)

    assert legacy_inventory(restored) == pre["legacy"]
    assert compare_inventories(backup.source_inventory, _inventory_of(restored)) == []


# --- M2-43 … M2-43b: a damaged backup is refused, not restored -----------------

#: A legacy value the corpus writes exactly once, in an unindexed column, and the
#: same-length replacement put in its place. Same length so the page it lives on keeps
#: its layout, unindexed so no second copy of it exists to disagree with -- which is
#: what makes the damaged file still a structurally valid database carrying one value
#: that is no longer the one the backup was verified as.
M2_TAMPERED_VALUE = b"M2 corpus"
M2_TAMPERED_REPLACEMENT = b"M2 corpuz"


def assert_m2_database_is_untouched(path: Path) -> None:
    """The live, migrated M2 database, unaffected by a damaged *backup copy*.

    Damaging `backup.path` and failing to restore from it must say nothing about
    `path` itself: the workspace this fixture actually migrated and seeded stays
    exactly as canonical, with clean integrity and foreign keys and 0008 still
    recorded applied.
    """
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert set(M2_TABLES) <= set(table_names(connection))
        assert MIGRATION_VERSION in applied_migrations(connection)
    finally:
        connection.close()


@pytest.mark.parametrize("damage", ["tampered", "incomplete"])
def test_m2_43_a_tampered_or_incomplete_backup_is_refused(
    adopted_with_m2_backup: tuple[Path, Any, dict[str, Any]], damage: str
) -> None:
    """M2-43: a backup damaged after verification is not accepted as one.

    Both damages are applied to the file's bytes rather than through SQL, exactly as
    M1-37 established: a backup of a migrated workspace carries the same guard
    triggers, so a stock SQLite client cannot delete a row from it at all, and a test
    that tried to would be proving the guard a second time instead of proving that a
    damaged backup is refused. Byte damage also models what actually happens to a
    backup after it was verified -- nobody edits it through the product.

    Because `adopted_with_m2_backup` is function-scoped, each parametrized case gets
    its own fresh backup file and its own restore target, so "tampered" and
    "incomplete" damage two genuinely separate copies rather than the same one twice.

    The expectations are drawn from what the existing implementation emits, and are
    kept wider than one exact type and message: a torn file can be caught by the
    restore itself, by the integrity check, or by the inventory comparison, and which
    one answers first is not part of the accepted behaviour. That it is refused, that
    the target is not left standing as a completed rollback, and that the original M2
    database stays canonical, is.
    """
    path, backup, _pre = adopted_with_m2_backup
    target = path.with_name(f"restore-target-{damage}.sqlite")

    expected: tuple[type[BaseException], ...]
    if damage == "tampered":
        raw = backup.path.read_bytes()
        at = raw.find(M2_TAMPERED_VALUE)
        assert at > 0, "the legacy value being tampered with is not in the backup"
        assert raw.count(M2_TAMPERED_VALUE) == 1, "the tampered value must be unique"
        backup.path.write_bytes(
            raw[:at] + M2_TAMPERED_REPLACEMENT + raw[at + len(M2_TAMPERED_VALUE) :]
        )
        expected = (LegacyMigrationError,)
        match = "did not restore the original"
    else:
        content = backup.path.read_bytes()
        backup.path.write_bytes(content[: len(content) // 3])
        expected = (StorageError, sqlite3.DatabaseError)
        match = "malformed|not a database|integrity|did not restore"

    with pytest.raises(expected, match=match):
        rollback_migration(backup, target)

    # Nothing may read the target as a successful restore: either it was never
    # produced, or what was produced is not the state the backup was verified as.
    if target.exists():
        assert compare_inventories(backup.source_inventory, _inventory_of(target)) != []

    assert_m2_database_is_untouched(path)


# --- M2-43b: this module adds no skip and no xfail -----------------------------


def test_m2_43b_this_module_declares_no_skip_or_xfail() -> None:
    """M2-43b: acceptance stays green without anything being excused from running."""
    source = Path(__file__).read_text(encoding="utf-8")
    for pattern in (
        r"@pytest\.mark\.skip",
        r"@pytest\.mark\.xfail",
        r"pytest\.skip\(",
        r"pytest\.xfail\(",
        r"pytest\.importorskip\(",
    ):
        assert re.search(pattern, source) is None, pattern
