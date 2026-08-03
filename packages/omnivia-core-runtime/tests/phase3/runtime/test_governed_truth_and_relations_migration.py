"""V06-1 M3 acceptance for governed records, versions and relations."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    create_verified_backup,
    new_attempt_id,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

MIGRATION_VERSION = 9
MIGRATION_NAME = "0009_governed_truth_and_relations.sql"
WORKSPACE_ID = m2.WORKSPACE_ID
BASE_US = m2.BASE_US + 100
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64

CATALOGUE = "omnivia_governed_schema_catalogue"
RECORDS = "omnivia_governed_records"
ASSEMBLIES = "omnivia_governed_version_assemblies"
EXTRACTION = "omnivia_governed_extraction_lineage"
LEGACY = "omnivia_governed_legacy_lineage"
EVENTS = "omnivia_governed_provenance_events"
LINKS = "omnivia_governed_version_evidence_links"
ENDPOINTS = "omnivia_governed_relation_endpoints"
SUPERSESSIONS = "omnivia_record_supersessions"
SEALS = "omnivia_governed_version_seals"
VIEW = "omnivia_authoritative_governed_versions"

M3_TABLES = {
    CATALOGUE,
    RECORDS,
    ASSEMBLIES,
    EXTRACTION,
    LEGACY,
    EVENTS,
    LINKS,
    ENDPOINTS,
    SUPERSESSIONS,
    SEALS,
}

M3_INDEXES = {
    "omnivia_idx_governed_schema_catalogue_identity",
    "omnivia_idx_governed_records_identity",
    "omnivia_idx_governed_records_typed_identity",
    "omnivia_idx_governed_version_assemblies_identity",
    "omnivia_idx_governed_version_assemblies_version",
    "omnivia_idx_governed_version_assemblies_correlation",
    "omnivia_idx_governed_version_assemblies_version_correlation",
    "omnivia_idx_governed_provenance_events_identity",
    "omnivia_idx_governed_version_assemblies_append_ordinal",
    "omnivia_idx_governed_provenance_events_sequence",
    "omnivia_idx_governed_version_evidence_links_ordinal",
    "omnivia_idx_governed_version_seals_assembly",
    "omnivia_idx_governed_version_seals_version",
    "omnivia_idx_record_supersessions_assembly",
    "omnivia_idx_record_supersessions_source",
    "omnivia_idx_record_supersessions_target",
    "omnivia_idx_governed_relation_endpoints_assembly",
    "omnivia_idx_governed_extraction_lineage_assembly",
    "omnivia_idx_governed_legacy_lineage_assembly",
    "omnivia_idx_governed_version_assemblies_record",
    "omnivia_idx_governed_provenance_events_action",
    "omnivia_idx_governed_version_evidence_links_evidence",
    "omnivia_idx_governed_relation_endpoints_source",
    "omnivia_idx_governed_relation_endpoints_target",
    "omnivia_idx_record_supersessions_record",
}

M3_TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{verb}"
    for table in M3_TABLES
    for verb in ("insert", "update", "delete")
}

VIEW_COLUMNS = (
    "workspace_id",
    "assembly_id",
    "seal_id",
    "governed_record_id",
    "governed_record_version_id",
    "record_type",
    "domain_scope",
    "layer",
    "governance_disposition",
    "authority_level",
    "decision_source_kind",
    "decision_source_id",
    "content_schema_version",
    "content_json",
    "content_digest",
    "evidence_disposition",
    "valid_from_us",
    "valid_to_us",
    "recorded_at_us",
    "append_ordinal",
    "correlation_kind",
    "correlation_id",
    "audit_ref",
    "sealed_at_us",
)

CATALOGUE_ROWS = {
    ("knowledge.claim", "1.0", "statement"),
    ("knowledge.decision", "1.0", "decision"),
    ("knowledge.proposal", "1.0", "proposal"),
    ("knowledge.requirement", "1.0", "requirement"),
    ("knowledge.constraint", "1.0", "constraint"),
    ("knowledge.assumption", "1.0", "assumption"),
    ("knowledge.preference", "1.0", "preference"),
    ("knowledge.question", "1.0", "question"),
    ("knowledge.finding", "1.0", "finding"),
    ("knowledge.risk", "1.0", "risk"),
    ("knowledge.outcome", "1.0", "outcome"),
    ("knowledge.entity", "1.0", "name"),
    ("knowledge.relation", "1.0", "statement"),
}

ACCEPTED_PREDECESSOR_HASHES = {
    1: "2ca7c190c8678994a2229a8ee9b2d1c12b259406de6dada108202cf51a8aa87c",
    2: "10807d2ff70ba1e1c4c9b407b9cf1efd7fdb11767726ae10bcb0831de40b1d06",
    3: "bf1a837c16d3e012db0c4fa78071d7198c116f27b75dfd0308fce3f4ec5d3f0f",
    4: "67048d8b1d9cf5dd565f7dff0c2bbd07529a3c18f3b8e05ae4add7d1521c1cde",
    5: "e6cc0d7f6d0ce59c60a35eeaed77780b47af93175bbcb3eea933cc98c7832e89",
    6: "c3c1501b0cf22ccaa3af16d1ea2c77e7c62dd3b94d4e26cf5cd0041caf1a471e",
    7: "1a315bb097092c1981bf9c3175d8a8c69fb5436006f7a4e6145f9ef9ca42891a",
    8: "5a725ba9fb9bd04f8295b6730abe5010ca5a5b1df8e0f8168010998fad7417bc",
}


def migration_under_test() -> Any:
    found = [migration for migration in load_migrations() if migration.version == 9]
    assert len(found) == 1
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))

M3_RETRY_CHILD = """
import json
import sys
from pathlib import Path
from omnivia_core_runtime.storage.connection import (
    OpenMode, foreign_key_check, integrity_check, open_database,
)
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations, read_workspace_state,
)

path, service, workspace_id = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
try:
    state = read_workspace_state(connection)
    applied = apply_pending_migrations(
        connection,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=service,
        fencing_generation=state.fencing_generation,
        workspace_id=workspace_id,
    )
    result = {
        "applied": [migration.version for migration in applied],
        "integrity": integrity_check(connection),
        "foreign_keys": foreign_key_check(connection),
    }
finally:
    connection.close()
print(json.dumps(result, sort_keys=True))
"""


def insert(connection: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    connection.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES "
        f"({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def audit_row(audit_ref: str, principal: str = "principal-1") -> dict[str, object]:
    return {
        "audit_ref": audit_ref,
        "workspace_id": WORKSPACE_ID,
        "principal_id": principal,
        "operation": "knowledge.record",
        "purpose": "governance",
        "request_id": f"request-{audit_ref}",
        "correlation_id": f"correlation-{audit_ref}",
        "trace_id": f"trace-{audit_ref}",
        "granted_authority_json": '{"roles":["reviewer"]}',
        "outcome_class": "succeeded",
        "error_code": None,
        "recorded_at_us": BASE_US,
        "claim_id": None,
        "outcome_id": None,
    }


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    m2.seed_chain(holder)
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        for number in range(1, 9):
            insert(
                holder.connection,
                "omnivia_application_audit_events",
                audit_row(f"audit-{number}"),
            )
    yield holder
    holder.connection.close()


def record_row(
    record_id: str = "record-1",
    *,
    record_type: str = "knowledge.claim",
    scope: str = "product.core",
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "governed_record_id": record_id,
        "record_type": record_type,
        "domain_scope": scope,
        "recorded_at_us": BASE_US + 1,
    }


def assembly_row(
    assembly_id: str = "assembly-1",
    version_id: str = "version-1",
    record_id: str = "record-1",
    *,
    record_type: str = "knowledge.claim",
    scope: str = "product.core",
    layer: str = "candidate",
    origin: str | None = "human_proposed",
    extraction_kind: str | None = None,
    disposition: str | None = None,
    authority: str = "proposed",
    decision_kind: str | None = None,
    decision_id: str | None = None,
    audit_ref: str | None = "audit-1",
    correlation_kind: str = "m1_audit",
    correlation_id: str = "audit-1",
    ordinal: int = 1,
    digest: str = DIGEST_A,
    evidence: str = "available",
) -> dict[str, object]:
    governed = layer == "governed"
    return {
        "workspace_id": WORKSPACE_ID,
        "assembly_id": assembly_id,
        "governed_record_id": record_id,
        "governed_record_version_id": version_id,
        "record_type": record_type,
        "domain_scope": scope,
        "layer": layer,
        "authority_level": authority,
        "governance_disposition": disposition,
        "candidate_origin": None if governed else origin,
        "extraction_kind": None if governed else extraction_kind,
        "decision_source_kind": decision_kind,
        "decision_source_id": decision_id,
        "authority_policy_id": "authority-rule" if governed else None,
        "authority_policy_version": "1" if governed else None,
        "policy_decision_ref": "decision-ref" if governed else None,
        "content_schema_version": "1.0",
        "content_json": '{"statement":"A durable claim"}',
        "content_digest": digest,
        "evidence_disposition": evidence,
        "confidence_ppm": 900000,
        "assertion_actor_id": "principal-1",
        "assertion_actor_kind": "human",
        "assertion_actor_role": "author",
        "reason_code": "governance.reviewed" if governed else None,
        "reason_comment": None,
        "valid_from_us": -1,
        "valid_to_us": None,
        "recorded_at_us": BASE_US + ordinal + 1,
        "append_ordinal": ordinal,
        "correlation_kind": correlation_kind,
        "correlation_id": correlation_id,
        "audit_ref": audit_ref,
    }


def event_row(
    event_id: str,
    assembly_id: str,
    version_id: str,
    action: str,
    *,
    sequence: int = 1,
    audit_ref: str | None = "audit-1",
    correlation_kind: str = "m1_audit",
    correlation_id: str = "audit-1",
    actor_id: str | None = "principal-1",
    actor_kind: str | None = "human",
    actor_role: str | None = "author",
    policy_id: str | None = None,
    policy_version: str | None = None,
    predecessor_record_id: str | None = None,
    predecessor_version_id: str | None = None,
    evidence: str = "available",
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "provenance_event_id": event_id,
        "assembly_id": assembly_id,
        "governed_record_version_id": version_id,
        "provenance_sequence": sequence,
        "action": action,
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "actor_role": actor_role,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "occurred_at_us": BASE_US + sequence,
        "recorded_at_us": BASE_US + sequence + 1,
        "reason_code": (
            "governance.reviewed"
            if action.startswith("governance.") or action == "record.superseded"
            else None
        ),
        "reason_comment": None,
        "audit_ref": audit_ref,
        "correlation_kind": correlation_kind,
        "correlation_id": correlation_id,
        "predecessor_record_id": predecessor_record_id,
        "predecessor_version_id": predecessor_version_id,
        "evidence_disposition": evidence,
    }


def link_row(
    event_id: str,
    assembly_id: str,
    *,
    ordinal: int = 1,
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "assembly_id": assembly_id,
        "provenance_event_id": event_id,
        "link_ordinal": ordinal,
        "evidence_id": "evd-0001",
        "normalized_record_id": "nrc-0001",
        "normalized_span_id": "nsp-0001",
        "recorded_at_us": BASE_US + ordinal + 10,
    }


def seal_row(
    assembly_id: str = "assembly-1",
    version_id: str = "version-1",
    *,
    seal_id: str = "seal-1",
    correlation_kind: str = "m1_audit",
    correlation_id: str = "audit-1",
) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "seal_id": seal_id,
        "assembly_id": assembly_id,
        "governed_record_version_id": version_id,
        "correlation_kind": correlation_kind,
        "correlation_id": correlation_id,
        "sealed_at_us": BASE_US + 50,
    }


def seed_human_candidate(
    holder: m2.Owned,
    *,
    record_id: str = "record-1",
    assembly_id: str = "assembly-1",
    version_id: str = "version-1",
    record_type: str = "knowledge.claim",
    audit_ref: str = "audit-1",
    ordinal: int = 1,
    evidence_link: bool = True,
    seal: bool = True,
) -> None:
    event_id = f"event-{assembly_id}"
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        if not holder.connection.execute(
            f"SELECT 1 FROM {RECORDS} WHERE workspace_id=? AND governed_record_id=?",
            (WORKSPACE_ID, record_id),
        ).fetchone():
            insert(
                holder.connection,
                RECORDS,
                record_row(record_id, record_type=record_type),
            )
        insert(
            holder.connection,
            ASSEMBLIES,
            assembly_row(
                assembly_id,
                version_id,
                record_id,
                record_type=record_type,
                audit_ref=audit_ref,
                correlation_id=audit_ref,
                ordinal=ordinal,
            ),
        )
        insert(
            holder.connection,
            EVENTS,
            event_row(
                event_id,
                assembly_id,
                version_id,
                "candidate.human_proposed",
                audit_ref=audit_ref,
                correlation_id=audit_ref,
            ),
        )
        if evidence_link:
            insert(holder.connection, LINKS, link_row(event_id, assembly_id))
        if seal:
            insert(
                holder.connection,
                SEALS,
                seal_row(
                    assembly_id,
                    version_id,
                    seal_id=f"seal-{assembly_id}",
                    correlation_id=audit_ref,
                ),
            )


def seed_extracted_candidate(
    holder: m2.Owned,
    *,
    extraction_kind: str = "deterministic",
    lineage_overrides: dict[str, object] | None = None,
    seal: bool = True,
) -> None:
    assembly_id = "assembly-extracted"
    version_id = "version-extracted"
    event_id = "event-extracted"
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(holder.connection, RECORDS, record_row("record-extracted"))
        row = assembly_row(
            assembly_id,
            version_id,
            "record-extracted",
            origin="extracted",
            extraction_kind=extraction_kind,
            audit_ref=None,
            correlation_kind="extraction_run",
            correlation_id="run-extracted",
        )
        row.update(
            assertion_actor_id="extractor-1",
            assertion_actor_kind="service",
            assertion_actor_role="extractor",
        )
        insert(holder.connection, ASSEMBLIES, row)
        event = event_row(
            event_id,
            assembly_id,
            version_id,
            "candidate.extracted",
            audit_ref=None,
            correlation_kind="extraction_run",
            correlation_id="run-extracted",
            actor_id="extractor-1",
            actor_kind="service",
            actor_role="extractor",
        )
        insert(holder.connection, EVENTS, event)
        lineage: dict[str, object] = {
            "workspace_id": WORKSPACE_ID,
            "assembly_id": assembly_id,
            "provenance_event_id": event_id,
            "correlation_kind": "extraction_run",
            "correlation_id": "run-extracted",
            "extraction_kind": extraction_kind,
            "extraction_run_id": "run-extracted",
            "extractor_id": "extractor-1",
            "extractor_version": "1",
            "pipeline_version": "1",
            "configuration_version": "1",
            "configuration_digest": (
                None if extraction_kind == "model_backed" else DIGEST_A
            ),
            "algorithm_id": (
                None if extraction_kind == "model_backed" else "algorithm-1"
            ),
            "algorithm_version": (None if extraction_kind == "model_backed" else "1"),
            "randomness_seed": (
                "seed-1" if extraction_kind == "model_free_nondeterministic" else None
            ),
            "model_id": "model-1" if extraction_kind == "model_backed" else None,
            "model_version": "1" if extraction_kind == "model_backed" else None,
            "inference_configuration_digest": (
                DIGEST_A if extraction_kind == "model_backed" else None
            ),
            "prompt_template_id": None,
            "prompt_template_version": None,
            "prompt_template_digest": None,
            "actor_id": "extractor-1",
            "actor_kind": "service",
            "actor_role": "extractor",
            "evidence_disposition": "available",
            "occurred_at_us": BASE_US + 1,
            "recorded_at_us": BASE_US + 2,
            "append_ordinal": 1,
        }
        if lineage_overrides:
            lineage.update(lineage_overrides)
        insert(holder.connection, EXTRACTION, lineage)
        insert(holder.connection, LINKS, link_row(event_id, assembly_id))
        if seal:
            insert(
                holder.connection,
                SEALS,
                seal_row(
                    assembly_id,
                    version_id,
                    seal_id="seal-extracted",
                    correlation_kind="extraction_run",
                    correlation_id="run-extracted",
                ),
            )


def seed_legacy_candidate(holder: m2.Owned) -> None:
    assembly_id = "assembly-legacy"
    version_id = "version-legacy"
    event_id = "event-legacy"
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(holder.connection, RECORDS, record_row("record-legacy"))
        row = assembly_row(
            assembly_id,
            version_id,
            "record-legacy",
            origin="migrated_legacy_claim",
            audit_ref=None,
            correlation_kind="migration_run",
            correlation_id="run-migration",
        )
        row.update(
            assertion_actor_id="migration-1",
            assertion_actor_kind="service",
            assertion_actor_role="migrator",
        )
        insert(holder.connection, ASSEMBLIES, row)
        insert(
            holder.connection,
            EVENTS,
            event_row(
                event_id,
                assembly_id,
                version_id,
                "candidate.legacy_migrated",
                audit_ref=None,
                correlation_kind="migration_run",
                correlation_id="run-migration",
                actor_id="migration-1",
                actor_kind="service",
                actor_role="migrator",
            ),
        )
        insert(
            holder.connection,
            LEGACY,
            {
                "workspace_id": WORKSPACE_ID,
                "assembly_id": assembly_id,
                "provenance_event_id": event_id,
                "correlation_kind": "migration_run",
                "correlation_id": "run-migration",
                "legacy_source_id": "legacy-source",
                "legacy_source_version": "1",
                "legacy_source_digest": DIGEST_A,
                "migration_run_id": "run-migration",
                "actor_id": "migration-1",
                "actor_kind": "service",
                "actor_role": "migrator",
                "evidence_disposition": "available",
                "occurred_at_us": BASE_US + 1,
                "recorded_at_us": BASE_US + 2,
                "append_ordinal": 1,
            },
        )
        insert(holder.connection, LINKS, link_row(event_id, assembly_id))
        insert(
            holder.connection,
            SEALS,
            seal_row(
                assembly_id,
                version_id,
                seal_id="seal-legacy",
                correlation_kind="migration_run",
                correlation_id="run-migration",
            ),
        )


def seed_accepted_version(
    holder: m2.Owned,
    *,
    record_id: str = "record-1",
    candidate_assembly: str = "assembly-1",
    candidate_version: str = "version-1",
    assembly_id: str = "assembly-accepted",
    version_id: str = "version-accepted",
    audit_ref: str = "audit-2",
    ordinal: int = 2,
) -> None:
    if not holder.connection.execute(
        f"SELECT 1 FROM {SEALS} WHERE workspace_id=? AND governed_record_version_id=?",
        (WORKSPACE_ID, candidate_version),
    ).fetchone():
        seed_human_candidate(
            holder,
            record_id=record_id,
            assembly_id=candidate_assembly,
            version_id=candidate_version,
        )
    event_id = f"event-{assembly_id}"
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(
            holder.connection,
            ASSEMBLIES,
            assembly_row(
                assembly_id,
                version_id,
                record_id,
                layer="governed",
                origin=None,
                disposition="accepted",
                authority="canonical",
                decision_kind="human_reviewer",
                decision_id="reviewer-1",
                audit_ref=audit_ref,
                correlation_id=audit_ref,
                ordinal=ordinal,
                digest=DIGEST_B,
            ),
        )
        insert(
            holder.connection,
            EVENTS,
            event_row(
                event_id,
                assembly_id,
                version_id,
                "governance.accepted",
                audit_ref=audit_ref,
                correlation_id=audit_ref,
                actor_id="reviewer-1",
                actor_kind="human",
                actor_role="reviewer",
                predecessor_record_id=record_id,
                predecessor_version_id=candidate_version,
            ),
        )
        insert(holder.connection, LINKS, link_row(event_id, assembly_id))
        insert(
            holder.connection,
            SEALS,
            seal_row(
                assembly_id,
                version_id,
                seal_id=f"seal-{assembly_id}",
                correlation_id=audit_ref,
            ),
        )


def seed_governed_outcome(
    holder: m2.Owned,
    disposition: str,
    *,
    decision_kind: str = "human_reviewer",
) -> None:
    seed_human_candidate(holder)
    assembly_id = f"assembly-{disposition}"
    version_id = f"version-{disposition}"
    event_id = f"event-{disposition}"
    policy = decision_kind == "policy_evaluator"
    decision_id = "policy-1" if policy else "reviewer-1"
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(
            holder.connection,
            ASSEMBLIES,
            assembly_row(
                assembly_id,
                version_id,
                "record-1",
                layer="governed",
                origin=None,
                disposition=disposition,
                authority="canonical" if disposition == "accepted" else "reviewed",
                decision_kind=decision_kind,
                decision_id=decision_id,
                audit_ref="audit-2",
                correlation_id="audit-2",
                ordinal=2,
                digest=DIGEST_B,
            ),
        )
        insert(
            holder.connection,
            EVENTS,
            event_row(
                event_id,
                assembly_id,
                version_id,
                f"governance.{disposition}",
                audit_ref="audit-2",
                correlation_id="audit-2",
                actor_id=None if policy else decision_id,
                actor_kind=None if policy else "human",
                actor_role=None if policy else "reviewer",
                policy_id=decision_id if policy else None,
                policy_version="1" if policy else None,
                predecessor_record_id="record-1",
                predecessor_version_id="version-1",
            ),
        )
        insert(holder.connection, LINKS, link_row(event_id, assembly_id))
        insert(
            holder.connection,
            SEALS,
            seal_row(
                assembly_id,
                version_id,
                seal_id=f"seal-{disposition}",
                correlation_id="audit-2",
            ),
        )


def seed_candidate_relation(
    holder: m2.Owned,
    *,
    suffix: str = "1",
    ordinal: int = 1,
    relation_type: str = "reconciliation.related_to",
    source_record_id: str = "record-source",
    source_version_id: str = "version-source",
    target_record_id: str = "record-target",
    target_version_id: str = "version-target",
    audit_ref: str = "audit-3",
    seal: bool = True,
) -> tuple[str, str]:
    if not holder.connection.execute(
        f"SELECT 1 FROM {SEALS} WHERE governed_record_version_id='version-source'"
    ).fetchone():
        seed_human_candidate(
            holder,
            record_id="record-source",
            assembly_id="assembly-source",
            version_id="version-source",
            audit_ref="audit-1",
        )
        seed_human_candidate(
            holder,
            record_id="record-target",
            assembly_id="assembly-target",
            version_id="version-target",
            audit_ref="audit-2",
        )
    record_id = f"record-relation-{suffix}"
    assembly_id = f"assembly-relation-{suffix}"
    version_id = f"version-relation-{suffix}"
    origin_event = f"event-relation-origin-{suffix}"
    relation_event = f"event-relation-asserted-{suffix}"
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(
            holder.connection,
            RECORDS,
            record_row(record_id, record_type="knowledge.relation"),
        )
        insert(
            holder.connection,
            ASSEMBLIES,
            assembly_row(
                assembly_id,
                version_id,
                record_id,
                record_type="knowledge.relation",
                audit_ref=audit_ref,
                correlation_id=audit_ref,
                ordinal=ordinal,
            ),
        )
        insert(
            holder.connection,
            EVENTS,
            event_row(
                origin_event,
                assembly_id,
                version_id,
                "candidate.human_proposed",
                audit_ref=audit_ref,
                correlation_id=audit_ref,
            ),
        )
        insert(
            holder.connection,
            EVENTS,
            event_row(
                relation_event,
                assembly_id,
                version_id,
                "relation.asserted",
                sequence=2,
                audit_ref=audit_ref,
                correlation_id=audit_ref,
            ),
        )
        insert(holder.connection, LINKS, link_row(origin_event, assembly_id))
        insert(holder.connection, LINKS, link_row(relation_event, assembly_id))
        insert(
            holder.connection,
            ENDPOINTS,
            {
                "workspace_id": WORKSPACE_ID,
                "assembly_id": assembly_id,
                "provenance_event_id": relation_event,
                "correlation_kind": "m1_audit",
                "correlation_id": audit_ref,
                "relation_type": relation_type,
                "source_record_id": source_record_id,
                "source_version_id": source_version_id,
                "target_record_id": target_record_id,
                "target_version_id": target_version_id,
                "recorded_at_us": BASE_US + 20,
            },
        )
        if seal:
            insert(
                holder.connection,
                SEALS,
                seal_row(
                    assembly_id,
                    version_id,
                    seal_id=f"seal-{assembly_id}",
                    correlation_id=audit_ref,
                ),
            )
    return assembly_id, version_id


def seed_corrected_version(
    holder: m2.Owned,
    *,
    source_version_id: str = "version-accepted",
    target_recorded_at_us: int = BASE_US + 20,
    assembly_id: str = "assembly-corrected",
    version_id: str = "version-corrected",
    audit_ref: str = "audit-3",
    ordinal: int = 1,
    digest: str = "sha256:" + "3" * 64,
    bootstrap: bool = True,
    seal: bool = True,
) -> None:
    if bootstrap:
        seed_accepted_version(holder)
    correction_event = f"event-{assembly_id}-corrected"
    supersession_event = f"event-{assembly_id}-superseded"
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        row = assembly_row(
            assembly_id,
            version_id,
            "record-1",
            layer="governed",
            origin=None,
            disposition="accepted",
            authority="canonical",
            decision_kind="human_reviewer",
            decision_id="reviewer-1",
            audit_ref=audit_ref,
            correlation_id=audit_ref,
            ordinal=ordinal,
            digest=digest,
        )
        row["recorded_at_us"] = target_recorded_at_us
        insert(holder.connection, ASSEMBLIES, row)
        for event_id, action, sequence in (
            (correction_event, "governance.corrected", 1),
            (supersession_event, "record.superseded", 2),
        ):
            insert(
                holder.connection,
                EVENTS,
                event_row(
                    event_id,
                    assembly_id,
                    version_id,
                    action,
                    sequence=sequence,
                    audit_ref=audit_ref,
                    correlation_id=audit_ref,
                    actor_id="reviewer-1",
                    actor_kind="human",
                    actor_role="reviewer",
                    predecessor_record_id="record-1",
                    predecessor_version_id=source_version_id,
                ),
            )
            insert(holder.connection, LINKS, link_row(event_id, assembly_id))
        insert(
            holder.connection,
            SUPERSESSIONS,
            {
                "workspace_id": WORKSPACE_ID,
                "supersession_id": f"supersession-{version_id}",
                "assembly_id": assembly_id,
                "governed_record_id": "record-1",
                "source_version_id": source_version_id,
                "target_version_id": version_id,
                "provenance_event_id": supersession_event,
                "correlation_kind": "m1_audit",
                "correlation_id": audit_ref,
                "recorded_at_us": target_recorded_at_us,
            },
        )
        if seal:
            insert(
                holder.connection,
                SEALS,
                seal_row(
                    assembly_id,
                    version_id,
                    seal_id=f"seal-{version_id}",
                    correlation_id=audit_ref,
                ),
            )


def seed_accepted_relation(
    holder: m2.Owned,
    *,
    evidence_links: bool = True,
    governed_endpoints: bool = True,
    evidence: str = "available",
) -> None:
    seed_human_candidate(
        holder,
        record_id="record-source",
        assembly_id="assembly-source",
        version_id="version-source",
        audit_ref="audit-1",
    )
    seed_human_candidate(
        holder,
        record_id="record-target",
        assembly_id="assembly-target",
        version_id="version-target",
        audit_ref="audit-2",
    )
    if governed_endpoints:
        seed_accepted_version(
            holder,
            record_id="record-source",
            candidate_assembly="assembly-source",
            candidate_version="version-source",
            assembly_id="assembly-source-accepted",
            version_id="version-source-accepted",
            audit_ref="audit-3",
            ordinal=1,
        )
        seed_accepted_version(
            holder,
            record_id="record-target",
            candidate_assembly="assembly-target",
            candidate_version="version-target",
            assembly_id="assembly-target-accepted",
            version_id="version-target-accepted",
            audit_ref="audit-4",
            ordinal=1,
        )
    source_version = (
        "version-source-accepted" if governed_endpoints else "version-source"
    )
    target_version = (
        "version-target-accepted" if governed_endpoints else "version-target"
    )
    seed_candidate_relation(
        holder,
        source_version_id=source_version,
        target_version_id=target_version,
        audit_ref="audit-5",
    )
    assembly_id = "assembly-relation-accepted"
    version_id = "version-relation-accepted"
    governance_event = "event-relation-governed"
    relation_event = "event-relation-governed-asserted"
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(
            holder.connection,
            ASSEMBLIES,
            assembly_row(
                assembly_id,
                version_id,
                "record-relation-1",
                record_type="knowledge.relation",
                layer="governed",
                origin=None,
                disposition="accepted",
                authority="canonical",
                decision_kind="human_reviewer",
                decision_id="reviewer-1",
                audit_ref="audit-6",
                correlation_id="audit-6",
                ordinal=1,
                digest="sha256:" + "4" * 64,
                evidence=evidence,
            ),
        )
        insert(
            holder.connection,
            EVENTS,
            event_row(
                governance_event,
                assembly_id,
                version_id,
                "governance.accepted",
                audit_ref="audit-6",
                correlation_id="audit-6",
                actor_id="reviewer-1",
                actor_kind="human",
                actor_role="reviewer",
                predecessor_record_id="record-relation-1",
                predecessor_version_id="version-relation-1",
                evidence=evidence,
            ),
        )
        relation_event_row = event_row(
            relation_event,
            assembly_id,
            version_id,
            "relation.asserted",
            sequence=2,
            audit_ref="audit-6",
            correlation_id="audit-6",
            actor_id="reviewer-1",
            actor_kind="human",
            actor_role="reviewer",
            evidence=evidence,
        )
        if evidence != "available":
            relation_event_row["reason_code"] = f"evidence.{evidence}"
        insert(holder.connection, EVENTS, relation_event_row)
        if evidence_links:
            insert(holder.connection, LINKS, link_row(governance_event, assembly_id))
            insert(holder.connection, LINKS, link_row(relation_event, assembly_id))
        insert(
            holder.connection,
            ENDPOINTS,
            {
                "workspace_id": WORKSPACE_ID,
                "assembly_id": assembly_id,
                "provenance_event_id": relation_event,
                "correlation_kind": "m1_audit",
                "correlation_id": "audit-6",
                "relation_type": "reconciliation.supports",
                "source_record_id": "record-source",
                "source_version_id": source_version,
                "target_record_id": "record-target",
                "target_version_id": target_version,
                "recorded_at_us": BASE_US + 40,
            },
        )
        insert(
            holder.connection,
            SEALS,
            seal_row(
                assembly_id,
                version_id,
                seal_id="seal-relation-accepted",
                correlation_id="audit-6",
            ),
        )


def seed_table_family(holder: m2.Owned, table: str) -> None:
    if table == CATALOGUE:
        return
    if table == EXTRACTION:
        seed_extracted_candidate(holder)
    elif table == LEGACY:
        seed_legacy_candidate(holder)
    elif table == ENDPOINTS:
        seed_candidate_relation(holder)
    elif table == SUPERSESSIONS:
        seed_corrected_version(holder)
    else:
        seed_human_candidate(holder)


def test_m3_01_unique_consecutive_successor_and_predecessor_hashes() -> None:
    migrations = load_migrations()
    assert [migration.version for migration in migrations] == list(range(1, 10))
    assert migrations[-1].name == MIGRATION_NAME
    assert {
        m.version: m.checksum for m in migrations[:-1]
    } == ACCEPTED_PREDECESSOR_HASHES
    assert MIGRATION.checksum == hashlib.sha256(MIGRATION.sql.encode()).hexdigest()


def test_m3_02_pristine_migration_converges_and_records_version(tmp_path: Path) -> None:
    path = tmp_path / "pristine.sqlite"
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        assert max(applied_migrations(connection)) == MIGRATION_VERSION
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_m3_03_exact_inventory_and_authoritative_view(owned: m2.Owned) -> None:
    connection = owned.connection
    before = sqlite3.connect(":memory:")
    try:
        before.executescript(m2.phase0_baseline_sql())
        for migration in load_migrations():
            if migration.version <= 8:
                before.executescript(migration.sql)
        assert (
            m2.object_names(connection, "table") - m2.object_names(before, "table")
            == M3_TABLES
        )
        assert (
            m2.object_names(connection, "index") - m2.object_names(before, "index")
            == M3_INDEXES
        )
        assert (
            m2.object_names(connection, "trigger") - m2.object_names(before, "trigger")
            == M3_TRIGGERS
        )
    finally:
        before.close()
    assert m2.object_names(connection, "view") == {VIEW}
    assert (
        tuple(row[1] for row in connection.execute(f"PRAGMA table_info({VIEW})"))
        == VIEW_COLUMNS
    )
    for table in M3_TABLES:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        assert "WITHOUT ROWID" in sql
    view_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (VIEW,)
    ).fetchone()[0]
    assert "omnivia_governed_version_seals" in view_sql
    assert "current" not in view_sql.lower()


def test_m3_04_catalogue_is_complete_and_immutable(owned: m2.Owned) -> None:
    connection = owned.connection
    assert set(connection.execute(f"SELECT * FROM {CATALOGUE}")) == CATALOGUE_ROWS
    for statement in (
        f"INSERT INTO {CATALOGUE} VALUES ('knowledge.future','1.0','statement')",
        f"UPDATE {CATALOGUE} SET primary_member='other'",
        f"DELETE FROM {CATALOGUE}",
    ):
        with (
            fenced_transaction(
                owned.connection,
                owned.identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=owned.generation,
            ),
            pytest.raises(sqlite3.DatabaseError, match="frozen|not authorized"),
        ):
            connection.execute(statement)


def test_m3_05_human_candidate_seals_and_enters_view(owned: m2.Owned) -> None:
    seed_human_candidate(owned)
    row = owned.connection.execute(
        f"SELECT governed_record_id, governed_record_version_id, layer FROM {VIEW}"
    ).fetchone()
    assert tuple(row) == ("record-1", "version-1", "candidate")


@pytest.mark.parametrize(
    "override",
    (
        {"governed_record_id": "record-other"},
        {"record_type": "knowledge.decision"},
        {"domain_scope": "product.other"},
        {"content_schema_version": "2.0"},
        {"authority_level": "canonical"},
        {"decision_source_kind": "human_reviewer", "decision_source_id": "reviewer"},
        {"content_json": "x"},
        {"content_json": "x" * 1_048_577},
        {"content_digest": "sha256:" + "G" * 64},
        {"valid_to_us": -1},
        {"confidence_ppm": 1_000_001},
        {"recorded_at_us": 0},
        {"append_ordinal": 0},
        {"audit_ref": None},
        {"correlation_id": "audit-2"},
    ),
    ids=(
        "foreign-record",
        "foreign-type",
        "foreign-scope",
        "unknown-schema",
        "candidate-canonical",
        "candidate-decision-source",
        "content-too-short",
        "content-too-long",
        "malformed-digest",
        "invalid-interval",
        "confidence-overflow",
        "nonpositive-recorded-time",
        "nonpositive-ordinal",
        "missing-audit",
        "correlation-mismatch",
    ),
)
def test_m3_05b_stable_identity_content_and_candidate_matrix_refuses_substitution(
    owned: m2.Owned, override: dict[str, object]
) -> None:
    row = assembly_row()
    row.update(override)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(owned.connection, RECORDS, record_row())
        with pytest.raises(sqlite3.IntegrityError):
            insert(owned.connection, ASSEMBLIES, row)


@pytest.mark.parametrize("confidence", (None, 0, 1_000_000))
def test_m3_05c_signed_time_null_upper_and_confidence_boundaries_are_admitted(
    owned: m2.Owned, confidence: int | None
) -> None:
    row = assembly_row()
    row.update(
        valid_from_us=-(2**63),
        valid_to_us=None,
        confidence_ppm=confidence,
    )
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(owned.connection, RECORDS, record_row())
        insert(owned.connection, ASSEMBLIES, row)


def test_m3_06_unsealed_assembly_is_inert(owned: m2.Owned) -> None:
    seed_human_candidate(owned, seal=False)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {VIEW}").fetchone()[0] == 0
    assert integrity_check(owned.connection) == []


def test_m3_07_missing_event_or_evidence_refuses_seal(owned: m2.Owned) -> None:
    seed_human_candidate(owned, evidence_link=False, seal=False)
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(sqlite3.IntegrityError, match="evidence disposition"),
    ):
        insert(owned.connection, SEALS, seal_row())


def test_m3_08_content_digest_and_interval_constraints(owned: m2.Owned) -> None:
    for index, override in enumerate(
        (
            {"content_digest": "sha256:" + "G" * 64},
            {"valid_from_us": 3, "valid_to_us": 3},
            {"confidence_ppm": 1_000_001},
        ),
        start=1,
    ):
        record_id = f"invalid-record-{index}"
        row = assembly_row(
            f"invalid-assembly-{index}",
            f"invalid-version-{index}",
            record_id,
            ordinal=index,
        )
        row.update(override)
        with fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ):
            insert(owned.connection, RECORDS, record_row(record_id))
            with pytest.raises(sqlite3.IntegrityError):
                insert(owned.connection, ASSEMBLIES, row)
        owned.connection.rollback()


def test_m3_09_append_ordinal_is_parent_scoped_and_positive(owned: m2.Owned) -> None:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(owned.connection, RECORDS, record_row())
        insert(owned.connection, ASSEMBLIES, assembly_row())
        with pytest.raises(sqlite3.IntegrityError):
            insert(
                owned.connection,
                ASSEMBLIES,
                assembly_row("assembly-2", "version-2", ordinal=1),
            )


@pytest.mark.parametrize("table", sorted(M3_TABLES))
@pytest.mark.parametrize("statement", ("UPDATE", "DELETE", "REPLACE", "UPSERT"))
def test_m3_10_all_m3_tables_refuse_rewrite_delete_and_replacement(
    owned: m2.Owned, table: str, statement: str
) -> None:
    seed_table_family(owned, table)
    writable_columns = tuple(
        row[1]
        for row in owned.connection.execute(f"PRAGMA table_xinfo({table})")
        if row[6] == 0
    )
    first_column = writable_columns[0]
    columns = ", ".join(writable_columns)
    sql = {
        "UPDATE": f"UPDATE {table} SET {first_column}={first_column}",
        "DELETE": f"DELETE FROM {table}",
        "REPLACE": (
            f"INSERT OR REPLACE INTO {table} ({columns}) "
            f"SELECT {columns} FROM {table} LIMIT 1"
        ),
        "UPSERT": (
            f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table} WHERE true "
            f"ON CONFLICT DO UPDATE SET {first_column}=excluded.{first_column}"
        ),
    }[statement]
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(
            sqlite3.DatabaseError,
            match="append-only|frozen|immutable|sealed|cannot be added",
        ),
    ):
        owned.connection.execute(sql)


def test_m3_11_schema_fingerprint_is_stable(owned: m2.Owned) -> None:
    before = fingerprint_schema(owned.connection)
    seed_human_candidate(owned)
    assert fingerprint_schema(owned.connection) == before


def test_m3_12_statement_inventory_has_no_deferred_foreign_keys() -> None:
    executable = "\n".join(MIGRATION_STATEMENTS).upper()
    assert "DEFERRABLE" not in executable
    assert "PRAGMA FOREIGN_KEYS = OFF" not in executable


def test_m3_13_integrity_and_foreign_keys_are_clean(owned: m2.Owned) -> None:
    seed_human_candidate(owned)
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


@pytest.mark.parametrize("disposition", ("unavailable", "redacted"))
def test_m3_13b_unavailable_or_redacted_evidence_requires_reason_and_forbids_links(
    owned: m2.Owned, disposition: str
) -> None:
    assembly = assembly_row(evidence=disposition)
    assembly["reason_code"] = f"evidence.{disposition}"
    event = event_row(
        "event-assembly-1",
        "assembly-1",
        "version-1",
        "candidate.human_proposed",
        evidence=disposition,
    )
    event["reason_code"] = f"evidence.{disposition}"
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(owned.connection, RECORDS, record_row())
        insert(owned.connection, ASSEMBLIES, assembly)
        insert(owned.connection, EVENTS, event)
        insert(owned.connection, SEALS, seal_row())
    assert (
        owned.connection.execute(
            f"SELECT evidence_disposition FROM {VIEW} WHERE assembly_id='assembly-1'"
        ).fetchone()[0]
        == disposition
    )


def test_m3_13c_unavailable_evidence_with_a_link_refuses_seal(owned: m2.Owned) -> None:
    assembly = assembly_row(evidence="unavailable")
    assembly["reason_code"] = "evidence.unavailable"
    event = event_row(
        "event-assembly-1",
        "assembly-1",
        "version-1",
        "candidate.human_proposed",
        evidence="unavailable",
    )
    event["reason_code"] = "evidence.unavailable"
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(owned.connection, RECORDS, record_row())
        insert(owned.connection, ASSEMBLIES, assembly)
        insert(owned.connection, EVENTS, event)
        insert(owned.connection, LINKS, link_row("event-assembly-1", "assembly-1"))
        with pytest.raises(sqlite3.IntegrityError, match="evidence disposition"):
            insert(owned.connection, SEALS, seal_row())


def test_m3_13d_span_evidence_record_chain_substitution_refuses_seal(
    owned: m2.Owned,
) -> None:
    m2.write(owned, m2.BLOBS, content_digest=m2.DIGEST_B)
    m2.write(
        owned,
        m2.EVIDENCE,
        evidence_id="evd-second",
        source_native_id="doc-2",
        blob_content_digest=m2.DIGEST_B,
        staged_source_ref=None,
    )
    m2.write(
        owned,
        m2.RECORDS,
        normalized_record_id="nrc-second",
        evidence_id="evd-second",
        evidence_blob_digest=m2.DIGEST_B,
    )
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(owned.connection, RECORDS, record_row())
        insert(owned.connection, ASSEMBLIES, assembly_row())
        insert(
            owned.connection,
            EVENTS,
            event_row(
                "event-assembly-1",
                "assembly-1",
                "version-1",
                "candidate.human_proposed",
            ),
        )
        bad_link = link_row("event-assembly-1", "assembly-1")
        bad_link.update(
            evidence_id="evd-second",
            normalized_record_id="nrc-second",
            normalized_span_id="nsp-0001",
        )
        insert(owned.connection, LINKS, bad_link)
        with pytest.raises(sqlite3.IntegrityError, match="span ancestry"):
            insert(owned.connection, SEALS, seal_row())


def test_m3_13e_logically_duplicate_evidence_link_is_null_safe(
    owned: m2.Owned,
) -> None:
    seed_human_candidate(owned, seal=False)
    duplicate = link_row("event-assembly-1", "assembly-1", ordinal=2)
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(sqlite3.IntegrityError, match="duplicate logical evidence"),
    ):
        insert(owned.connection, LINKS, duplicate)


def test_m3_14_extracted_candidate_requires_exact_lineage(owned: m2.Owned) -> None:
    seed_extracted_candidate(owned)
    assert (
        owned.connection.execute(
            f"SELECT layer FROM {VIEW} WHERE assembly_id='assembly-extracted'"
        ).fetchone()[0]
        == "candidate"
    )
    assert "AND randomness_seed IS NULL" in MIGRATION.sql


@pytest.mark.parametrize(
    ("kind", "overrides"),
    (
        ("deterministic", {}),
        ("model_free_nondeterministic", {}),
        ("model_backed", {}),
        (
            "model_backed",
            {
                "prompt_template_id": "prompt-1",
                "prompt_template_version": "1",
                "prompt_template_digest": DIGEST_B,
            },
        ),
    ),
)
def test_m3_14b_every_extraction_lineage_variant_seals(
    owned: m2.Owned, kind: str, overrides: dict[str, object]
) -> None:
    seed_extracted_candidate(owned, extraction_kind=kind, lineage_overrides=overrides)
    assert (
        owned.connection.execute(
            f"SELECT COUNT(*) FROM {VIEW} WHERE assembly_id='assembly-extracted'"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    ("kind", "overrides"),
    (
        ("deterministic", {"algorithm_id": None}),
        ("deterministic", {"algorithm_version": None}),
        ("deterministic", {"configuration_digest": None}),
        ("deterministic", {"randomness_seed": "seed-1"}),
        ("deterministic", {"model_id": "model-1"}),
        ("model_free_nondeterministic", {"model_id": "model-1"}),
        ("model_backed", {"model_id": None}),
        ("model_backed", {"model_version": None}),
        ("model_backed", {"inference_configuration_digest": None}),
        ("model_backed", {"randomness_seed": "seed-1"}),
        ("model_backed", {"prompt_template_id": "prompt-1"}),
    ),
)
def test_m3_14c_extraction_lineage_matrix_refuses_missing_or_forbidden_fields(
    owned: m2.Owned, kind: str, overrides: dict[str, object]
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        seed_extracted_candidate(
            owned, extraction_kind=kind, lineage_overrides=overrides
        )


def test_m3_15_migrated_candidate_requires_exact_legacy_lineage(
    owned: m2.Owned,
) -> None:
    seed_legacy_candidate(owned)
    assert (
        owned.connection.execute(
            f"SELECT layer FROM {VIEW} WHERE assembly_id='assembly-legacy'"
        ).fetchone()[0]
        == "candidate"
    )


def test_m3_16_candidate_promotion_appends_without_rewriting(owned: m2.Owned) -> None:
    seed_human_candidate(owned)
    before = owned.connection.execute(
        f"SELECT content_json, content_digest FROM {ASSEMBLIES} WHERE assembly_id='assembly-1'"
    ).fetchone()
    seed_accepted_version(owned)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {VIEW}").fetchone()[0] == 2
    after = owned.connection.execute(
        f"SELECT content_json, content_digest FROM {ASSEMBLIES} WHERE assembly_id='assembly-1'"
    ).fetchone()
    assert tuple(after) == tuple(before)


@pytest.mark.parametrize(
    "disposition",
    ("accepted", "rejected", "contested", "withdrawn", "superseded"),
)
def test_m3_16b_each_governance_outcome_requires_its_exact_event(
    owned: m2.Owned, disposition: str
) -> None:
    seed_governed_outcome(owned, disposition)
    assert (
        owned.connection.execute(
            f"SELECT governance_disposition FROM {VIEW} WHERE assembly_id=?",
            (f"assembly-{disposition}",),
        ).fetchone()[0]
        == disposition
    )


def test_m3_16c_policy_evaluator_identity_is_exclusive_and_exact(
    owned: m2.Owned,
) -> None:
    seed_governed_outcome(owned, "accepted", decision_kind="policy_evaluator")
    event = owned.connection.execute(
        f"SELECT actor_id, policy_id, policy_version FROM {EVENTS} "
        "WHERE assembly_id='assembly-accepted'"
    ).fetchone()
    assert tuple(event) == (None, "policy-1", "1")


@pytest.mark.parametrize(
    "override",
    (
        {"authority_level": "proposed"},
        {"governance_disposition": "rejected", "authority_level": "canonical"},
        {"decision_source_kind": None},
        {"decision_source_id": None},
        {"authority_policy_id": None},
        {"authority_policy_version": None},
        {"candidate_origin": "human_proposed"},
        {"audit_ref": None},
    ),
)
def test_m3_16d_governed_authority_matrix_refuses_inadmissible_assemblies(
    owned: m2.Owned, override: dict[str, object]
) -> None:
    seed_human_candidate(owned)
    row = assembly_row(
        "assembly-invalid-governed",
        "version-invalid-governed",
        "record-1",
        layer="governed",
        origin=None,
        disposition="accepted",
        authority="canonical",
        decision_kind="human_reviewer",
        decision_id="reviewer-1",
        audit_ref="audit-2",
        correlation_id="audit-2",
        ordinal=2,
        digest=DIGEST_B,
    )
    row.update(override)
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(sqlite3.IntegrityError),
    ):
        insert(owned.connection, ASSEMBLIES, row)


def test_m3_17_missing_extraction_lineage_refuses_last_step(owned: m2.Owned) -> None:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(owned.connection, RECORDS, record_row("record-no-lineage"))
        row = assembly_row(
            "assembly-no-lineage",
            "version-no-lineage",
            "record-no-lineage",
            origin="extracted",
            extraction_kind="deterministic",
            audit_ref=None,
            correlation_kind="extraction_run",
            correlation_id="run-no-lineage",
        )
        row.update(
            assertion_actor_id="extractor-1",
            assertion_actor_kind="service",
            assertion_actor_role="extractor",
        )
        insert(owned.connection, ASSEMBLIES, row)
        insert(
            owned.connection,
            EVENTS,
            event_row(
                "event-no-lineage",
                "assembly-no-lineage",
                "version-no-lineage",
                "candidate.extracted",
                audit_ref=None,
                correlation_kind="extraction_run",
                correlation_id="run-no-lineage",
                actor_id="extractor-1",
                actor_kind="service",
                actor_role="extractor",
            ),
        )
        insert(
            owned.connection, LINKS, link_row("event-no-lineage", "assembly-no-lineage")
        )
        with pytest.raises(sqlite3.IntegrityError, match="lineage"):
            insert(
                owned.connection,
                SEALS,
                seal_row(
                    "assembly-no-lineage",
                    "version-no-lineage",
                    seal_id="seal-no-lineage",
                    correlation_kind="extraction_run",
                    correlation_id="run-no-lineage",
                ),
            )


def test_m3_17b_missing_origin_event_refuses_last_step(owned: m2.Owned) -> None:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(owned.connection, RECORDS, record_row())
        insert(owned.connection, ASSEMBLIES, assembly_row())
        with pytest.raises(sqlite3.IntegrityError, match="event set"):
            insert(owned.connection, SEALS, seal_row())


def test_m3_17c_extra_or_duplicate_origin_event_refuses_last_step(
    owned: m2.Owned,
) -> None:
    seed_human_candidate(owned, seal=False)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            EVENTS,
            event_row(
                "event-extra",
                "assembly-1",
                "version-1",
                "candidate.human_proposed",
                sequence=2,
            ),
        )
        insert(owned.connection, LINKS, link_row("event-extra", "assembly-1"))
        with pytest.raises(sqlite3.IntegrityError, match="event set"):
            insert(owned.connection, SEALS, seal_row())


def test_m3_17d_governance_event_requires_a_sealed_same_record_candidate(
    owned: m2.Owned,
) -> None:
    seed_human_candidate(owned)
    assembly_id = "assembly-wrong-predecessor"
    version_id = "version-wrong-predecessor"
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            ASSEMBLIES,
            assembly_row(
                assembly_id,
                version_id,
                "record-1",
                layer="governed",
                origin=None,
                disposition="accepted",
                authority="canonical",
                decision_kind="human_reviewer",
                decision_id="reviewer-1",
                audit_ref="audit-2",
                correlation_id="audit-2",
                ordinal=1,
                digest=DIGEST_B,
            ),
        )
        insert(
            owned.connection,
            EVENTS,
            event_row(
                "event-wrong-predecessor",
                assembly_id,
                version_id,
                "governance.accepted",
                audit_ref="audit-2",
                correlation_id="audit-2",
                actor_id="reviewer-1",
                actor_kind="human",
                actor_role="reviewer",
            ),
        )
        insert(
            owned.connection,
            LINKS,
            link_row("event-wrong-predecessor", assembly_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="predecessor"):
            insert(
                owned.connection,
                SEALS,
                seal_row(
                    assembly_id,
                    version_id,
                    seal_id="seal-wrong-predecessor",
                    correlation_id="audit-2",
                ),
            )


@pytest.mark.parametrize(
    "table", (EXTRACTION, LEGACY, EVENTS, LINKS, ENDPOINTS, SUPERSESSIONS)
)
def test_m3_18_components_cannot_be_added_after_seal(
    owned: m2.Owned, table: str
) -> None:
    seed_table_family(owned, table)
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(sqlite3.IntegrityError, match="after the version is sealed"),
    ):
        owned.connection.execute(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1")


def test_m3_19_symmetric_relation_normalizes_and_seals(owned: m2.Owned) -> None:
    assembly_id, _version_id = seed_candidate_relation(owned)
    assert (
        owned.connection.execute(
            f"SELECT relation_type FROM {ENDPOINTS} WHERE assembly_id=?", (assembly_id,)
        ).fetchone()[0]
        == "reconciliation.related_to"
    )


@pytest.mark.parametrize(
    "relation_type",
    (
        "governance.contradicts",
        "reconciliation.duplicate_of",
        "reconciliation.equivalent_to",
        "reconciliation.related_to",
        "reconciliation.supports",
    ),
)
def test_m3_19b_each_frozen_relation_type_seals(
    owned: m2.Owned, relation_type: str
) -> None:
    seed_candidate_relation(owned, relation_type=relation_type)
    assert (
        owned.connection.execute(f"SELECT relation_type FROM {ENDPOINTS}").fetchone()[0]
        == relation_type
    )


def test_m3_19c_relation_self_edge_is_refused(owned: m2.Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        seed_candidate_relation(
            owned,
            target_record_id="record-source",
            target_version_id="version-source",
        )


def test_m3_20_unknown_relation_code_remains_inert(owned: m2.Owned) -> None:
    assembly_id, version_id = seed_candidate_relation(
        owned, relation_type="reconciliation.future", seal=False
    )
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(sqlite3.IntegrityError, match="relation type"),
    ):
        insert(
            owned.connection,
            SEALS,
            seal_row(
                assembly_id,
                version_id,
                seal_id="seal-future-relation",
                correlation_id="audit-3",
            ),
        )


def test_m3_21_symmetric_relation_reversed_order_refuses_seal(owned: m2.Owned) -> None:
    assembly_id, version_id = seed_candidate_relation(
        owned,
        source_record_id="record-target",
        source_version_id="version-target",
        target_record_id="record-source",
        target_version_id="version-source",
        seal=False,
    )
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(sqlite3.IntegrityError, match="normalization"),
    ):
        insert(
            owned.connection,
            SEALS,
            seal_row(
                assembly_id,
                version_id,
                seal_id="seal-reversed",
                correlation_id="audit-3",
            ),
        )


def test_m3_20b_semantic_duplicate_relation_is_refused(owned: m2.Owned) -> None:
    seed_candidate_relation(owned)
    assembly_id, version_id = seed_candidate_relation(
        owned, suffix="2", ordinal=2, seal=False
    )
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(sqlite3.IntegrityError, match="duplicate sealed relation"),
    ):
        insert(
            owned.connection,
            SEALS,
            seal_row(
                assembly_id,
                version_id,
                seal_id="seal-duplicate-relation",
                correlation_id="audit-3",
            ),
        )


def test_m3_21b_relation_endpoint_record_version_mismatch_refuses_seal(
    owned: m2.Owned,
) -> None:
    assembly_id, version_id = seed_candidate_relation(
        owned,
        source_record_id="record-mismatch",
        source_version_id="version-source",
        seal=False,
    )
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(sqlite3.IntegrityError, match="endpoint authority"),
    ):
        insert(
            owned.connection,
            SEALS,
            seal_row(
                assembly_id,
                version_id,
                seal_id="seal-mismatch",
                correlation_id="audit-3",
            ),
        )


def test_m3_22_accepted_relation_requires_governed_endpoints_and_evidence(
    owned: m2.Owned,
) -> None:
    seed_accepted_relation(owned)
    assert tuple(
        owned.connection.execute(
            f"SELECT layer, governance_disposition FROM {VIEW} "
            "WHERE assembly_id='assembly-relation-accepted'"
        ).fetchone()
    ) == ("governed", "accepted")


def test_m3_22b_accepted_relation_without_evidence_refuses_seal(
    owned: m2.Owned,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="evidence disposition"):
        seed_accepted_relation(owned, evidence_links=False)


def test_m3_22c_accepted_relation_refuses_candidate_endpoints(owned: m2.Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="endpoint authority"):
        seed_accepted_relation(owned, governed_endpoints=False)


@pytest.mark.parametrize("disposition", ("unavailable", "redacted"))
def test_m3_22d_accepted_relation_refuses_nonavailable_evidence(
    owned: m2.Owned, disposition: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="endpoint authority"):
        seed_accepted_relation(owned, evidence_links=False, evidence=disposition)


def test_m3_22e_later_endpoint_versions_do_not_retarget_a_relation(
    owned: m2.Owned,
) -> None:
    seed_accepted_relation(owned)
    before = tuple(
        owned.connection.execute(
            f"SELECT source_version_id, target_version_id FROM {ENDPOINTS} "
            "WHERE assembly_id='assembly-relation-accepted'"
        ).fetchone()
    )
    seed_accepted_version(
        owned,
        record_id="record-source",
        candidate_assembly="assembly-source",
        candidate_version="version-source",
        assembly_id="assembly-source-later",
        version_id="version-source-later",
        audit_ref="audit-7",
        ordinal=1,
    )
    seed_accepted_version(
        owned,
        record_id="record-target",
        candidate_assembly="assembly-target",
        candidate_version="version-target",
        assembly_id="assembly-target-later",
        version_id="version-target-later",
        audit_ref="audit-8",
        ordinal=1,
    )
    after = tuple(
        owned.connection.execute(
            f"SELECT source_version_id, target_version_id FROM {ENDPOINTS} "
            "WHERE assembly_id='assembly-relation-accepted'"
        ).fetchone()
    )
    assert after == before


def test_m3_23_corrected_supersession_is_atomic_and_monotonic(
    owned: m2.Owned,
) -> None:
    seed_corrected_version(owned)
    assert tuple(
        owned.connection.execute(
            f"SELECT source_version_id, target_version_id FROM {SUPERSESSIONS} "
            "WHERE supersession_id='supersession-version-corrected'"
        ).fetchone()
    ) == ("version-accepted", "version-corrected")
    assert (
        owned.connection.execute(
            f"SELECT COUNT(*) FROM {VIEW} WHERE governed_record_id='record-1'"
        ).fetchone()[0]
        == 3
    )


def test_m3_23b_supersession_refuses_an_earlier_target_time(owned: m2.Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="supersession invariant"):
        seed_corrected_version(owned, target_recorded_at_us=BASE_US + 2)


def test_m3_23bb_equal_timestamp_supersession_is_ordered_by_its_edge(
    owned: m2.Owned,
) -> None:
    seed_corrected_version(owned, target_recorded_at_us=BASE_US + 3)
    assert (
        owned.connection.execute(f"SELECT COUNT(*) FROM {SUPERSESSIONS}").fetchone()[0]
        == 1
    )


def test_m3_23c_supersession_refuses_a_candidate_source(owned: m2.Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="predecessor|supersession"):
        seed_corrected_version(owned, source_version_id="version-1")


def test_m3_23d_unsealed_supersession_is_inert_until_the_atomic_seal(
    owned: m2.Owned,
) -> None:
    seed_corrected_version(owned, seal=False)
    assert (
        owned.connection.execute(
            f"SELECT COUNT(*) FROM {VIEW} WHERE assembly_id='assembly-corrected'"
        ).fetchone()[0]
        == 0
    )
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            SEALS,
            seal_row(
                "assembly-corrected",
                "version-corrected",
                seal_id="seal-version-corrected",
                correlation_id="audit-3",
            ),
        )
    assert (
        owned.connection.execute(
            f"SELECT COUNT(*) FROM {VIEW} WHERE assembly_id='assembly-corrected'"
        ).fetchone()[0]
        == 1
    )


def test_m3_24b_supersession_branching_source_is_refused(owned: m2.Owned) -> None:
    seed_corrected_version(owned)
    with pytest.raises(sqlite3.IntegrityError):
        seed_corrected_version(
            owned,
            source_version_id="version-accepted",
            assembly_id="assembly-branch",
            version_id="version-branch",
            audit_ref="audit-4",
            digest="sha256:" + "5" * 64,
            bootstrap=False,
        )


def test_m3_24c_linear_supersession_chain_remains_valid(owned: m2.Owned) -> None:
    seed_corrected_version(owned)
    seed_corrected_version(
        owned,
        source_version_id="version-corrected",
        target_recorded_at_us=BASE_US + 30,
        assembly_id="assembly-corrected-next",
        version_id="version-corrected-next",
        audit_ref="audit-4",
        digest="sha256:" + "5" * 64,
        bootstrap=False,
    )
    assert (
        owned.connection.execute(f"SELECT COUNT(*) FROM {SUPERSESSIONS}").fetchone()[0]
        == 2
    )


def test_m3_24_supersession_self_edge_is_refused(owned: m2.Owned) -> None:
    seed_human_candidate(owned)
    with (
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
        pytest.raises(sqlite3.IntegrityError),
    ):
        insert(
            owned.connection,
            SUPERSESSIONS,
            {
                "workspace_id": WORKSPACE_ID,
                "supersession_id": "sup-self",
                "assembly_id": "assembly-1",
                "governed_record_id": "record-1",
                "source_version_id": "version-1",
                "target_version_id": "version-1",
                "provenance_event_id": "event-assembly-1",
                "correlation_kind": "m1_audit",
                "correlation_id": "audit-1",
                "recorded_at_us": BASE_US + 30,
            },
        )


def test_m3_25_recursive_cycle_guard_is_present() -> None:
    normalized = " ".join(MIGRATION.sql.split()).upper()
    assert "WITH RECURSIVE WALK" in normalized
    assert "SUPERSESSION CYCLE REFUSED" in normalized


def test_m3_25b_semantic_support_cycles_remain_valid(owned: m2.Owned) -> None:
    seed_candidate_relation(
        owned,
        relation_type="reconciliation.supports",
    )
    seed_candidate_relation(
        owned,
        suffix="2",
        ordinal=2,
        relation_type="reconciliation.supports",
        source_record_id="record-target",
        source_version_id="version-target",
        target_record_id="record-source",
        target_version_id="version-source",
    )
    assert (
        owned.connection.execute(f"SELECT COUNT(*) FROM {ENDPOINTS}").fetchone()[0] == 2
    )


def test_m3_26_ordinals_are_unique_even_at_equal_timestamps(owned: m2.Owned) -> None:
    seed_human_candidate(owned)
    seed_human_candidate(
        owned,
        record_id="record-2",
        assembly_id="assembly-2",
        version_id="version-2",
        audit_ref="audit-1",
        ordinal=2,
    )
    rows = owned.connection.execute(
        f"SELECT append_ordinal FROM {ASSEMBLIES} ORDER BY append_ordinal"
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2]


def test_m3_27_every_m3_table_has_three_immutable_guards(owned: m2.Owned) -> None:
    triggers = m2.object_names(owned.connection, "trigger")
    for table in M3_TABLES - {CATALOGUE}:
        stem = table.removeprefix("omnivia_")
        assert {
            f"omnivia_guard_{stem}_{verb}" for verb in ("insert", "update", "delete")
        } <= triggers
    assert {
        f"omnivia_guard_governed_schema_catalogue_{verb}"
        for verb in ("insert", "update", "delete")
    } <= triggers


def test_m3_27b_every_workspace_insert_guard_repeats_the_complete_authority_predicate(
    owned: m2.Owned,
) -> None:
    required = (
        "omnivia_service_writer()",
        "omnivia_mutation_guard",
        "omnivia_workspace_state",
        "omnivia_workspace_lease",
        "fencing_generation",
        "service_instance_id",
        "lifecycle IN ('acquiring', 'held', 'draining')",
        "NEW.workspace_id IS NOT",
    )
    for table in M3_TABLES - {CATALOGUE}:
        trigger = f"omnivia_guard_{table.removeprefix('omnivia_')}_insert"
        sql = owned.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
        ).fetchone()[0]
        assert all(fragment in sql for fragment in required), trigger


@pytest.mark.parametrize("table", sorted(M3_TABLES - {CATALOGUE}))
@pytest.mark.parametrize("case", m2.AUTHORITY_NEGATIVE_CASES)
def test_m3_28_each_workspace_table_requires_the_exact_current_authority_tuple(
    owned: m2.Owned, table: str, case: str
) -> None:
    seed_table_family(owned, table)
    identity, workspace_id, generation = m2._wrong_authority(case, owned)
    with (
        pytest.raises(m2.StaleGeneration, match=m2.AUTHORITY_REFUSAL_MESSAGE[case]),
        fenced_transaction(
            owned.connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=generation,
        ),
    ):
        owned.connection.execute(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1")


def test_m3_28_stock_sqlite_cannot_mutate_m3(owned: m2.Owned) -> None:
    owned.connection.close()
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
                '\ntry:\n c.execute("INSERT INTO omnivia_governed_records VALUES '
                "('ws-m2-blobs-0001','outside','knowledge.claim','product.core',1)\")\n"
                "except Exception as e:\n print(json.dumps({'error':str(e)}))\n"
            ),
            str(owned.path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "no such function" in json.loads(child.stdout)["error"]


def test_m3_29_interruption_rolls_back_every_migration_statement(
    tmp_path: Path,
) -> None:
    base = tmp_path / "accepted-through-0008.sqlite"
    materialise_phase0_baseline(base)
    with m2.migration_catalogue_through(8):
        m2.bootstrap_and_migrate(base)
    for stop_after in range(1, len(MIGRATION_STATEMENTS) + 1):
        path = tmp_path / f"interrupted-{stop_after}.sqlite"
        shutil.copy2(base, path)
        connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = m2.read_workspace_state(connection)
            crashing = m2.FailAfterStatement(
                connection, MIGRATION_STATEMENTS, stop_after
            )
            with pytest.raises(m2.MigrationInterrupted):
                apply_pending_migrations(
                    crashing,
                    mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                    service_instance_id=m2.SERVICE_INSTANCE,
                    fencing_generation=state.fencing_generation,
                    workspace_id=WORKSPACE_ID,
                )
        finally:
            connection.close()
        check = open_database(path, OpenMode.READ_ONLY)
        try:
            assert MIGRATION_VERSION not in applied_migrations(check)
            assert M3_TABLES.isdisjoint(m2.object_names(check, "table"))
            assert M3_INDEXES.isdisjoint(m2.object_names(check, "index"))
            assert M3_TRIGGERS.isdisjoint(m2.object_names(check, "trigger"))
            assert VIEW not in m2.object_names(check, "view")
        finally:
            check.close()
        retry = subprocess.run(
            [
                sys.executable,
                "-c",
                M3_RETRY_CHILD,
                str(path),
                m2.SERVICE_INSTANCE,
                WORKSPACE_ID,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(retry.stdout)
        assert result == {
            "applied": [MIGRATION_VERSION],
            "foreign_keys": [],
            "integrity": [],
        }


def test_m3_30_verified_backup_preserves_sealed_and_unsealed_state(
    owned: m2.Owned, tmp_path: Path
) -> None:
    seed_human_candidate(owned)
    seed_human_candidate(
        owned,
        record_id="record-unsealed",
        assembly_id="assembly-unsealed",
        version_id="version-unsealed",
        audit_ref="audit-2",
        seal=False,
    )
    owned.connection.close()
    installation = InstallationLayout(root=tmp_path / "installation")
    installation.create(WORKSPACE_ID)
    backup = create_verified_backup(
        owned.path,
        installation,
        workspace_id=WORKSPACE_ID,
        attempt_id=new_attempt_id(),
    )
    assert backup.verified
    restored = open_database(backup.path, OpenMode.READ_ONLY)
    try:
        assert restored.execute(f"SELECT COUNT(*) FROM {VIEW}").fetchone()[0] == 1
        assert restored.execute(f"SELECT COUNT(*) FROM {ASSEMBLIES}").fetchone()[0] == 2
        assert integrity_check(restored) == []
    finally:
        restored.close()


def test_m3_31_authoritative_view_has_no_mutable_currentness_columns() -> None:
    forbidden = {"current", "is_current", "canonical", "superseded_at", "deleted"}
    assert forbidden.isdisjoint(VIEW_COLUMNS)


def test_m3_32_schema_and_migration_attempt_evidence_is_clean(owned: m2.Owned) -> None:
    attempts = owned.connection.execute(
        "SELECT outcome FROM omnivia_migration_attempts WHERE version=9"
    ).fetchall()
    assert [row[0] for row in attempts] == ["succeeded"]
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


def test_m3_33_predecessor_suites_remain_explicit_external_gates() -> None:
    assert "test_application_audit_idempotency_migration.py" not in MIGRATION.sql
    assert "test_blobs_staged_sources_and_evidence_migration.py" not in MIGRATION.sql


def test_m3_34_migration_excludes_consumer_only_behavior() -> None:
    forbidden = (
        "CREATE REPOSITORY",
        "SEMANTICINDEX",
        "POLICY EVALUATOR",
        "PUBLIC DTO",
        "IS_CURRENT",
        "DEFERRABLE",
    )
    executable = "\n".join(MIGRATION_STATEMENTS).upper()
    assert all(term not in executable for term in forbidden)
