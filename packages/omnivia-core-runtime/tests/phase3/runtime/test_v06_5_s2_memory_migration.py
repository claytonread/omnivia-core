"""V06-5 S2 acceptance for migration 0014's application-governed bridges.

This is deliberately a migration test.  It proves the exact catalogue extension,
atomic upgrade and old-binary refusal, then exercises the immutable claim-lineage,
transition-chain and referenced-outcome relations under the real fence.  Repository,
handler and public-contract behaviour belongs to later S2 slices.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import test_application_audit_idempotency_migration as m1
import test_governed_truth_and_relations_migration as m3
from omnivia_core_runtime.ownership.fencing import (
    SchemaDrift,
    assert_guards_intact,
    fenced_transaction,
    verify_fingerprint,
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
    canonical_schema_fingerprint,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

MIGRATION_VERSION = 14
MIGRATION_NAME = "0014_application_governed_bridges.sql"
PREDECESSOR_VERSION = 13

CLAIMS = "omnivia_application_claim_lineage"
TRANSITIONS = "omnivia_application_governance_transitions"
DOCUMENTS = "omnivia_application_outcome_documents"
TABLES = (CLAIMS, TRANSITIONS, DOCUMENTS)

CATALOGUE_ROWS = m3.CATALOGUE_ROWS | {("memory.fact", "1.0", "fact")}


def migration_under_test() -> m1.Migration:
    found = [migration for migration in load_migrations() if migration.version == 14]
    assert len(found) == 1
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


def _apply_through(
    path: Path, version: int, *, workspace_id: str = m1.WORKSPACE_ID
) -> None:
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(version):
        m1.bootstrap_and_migrate(path, workspace_id=workspace_id)


def _upgrade_from_13(path: Path, *, workspace_id: str = m1.WORKSPACE_ID) -> None:
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        with m1.migration_catalogue_through(MIGRATION_VERSION):
            applied = apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=workspace_id,
            )
        assert [migration.version for migration in applied] == [MIGRATION_VERSION]
    finally:
        connection.close()


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.sqlite"
    _apply_through(path, MIGRATION_VERSION)
    return path


@pytest.fixture
def owned(migrated: Path) -> Iterator[m1.Owned]:
    holder = m1.take_ownership(migrated)
    yield holder
    holder.connection.close()


def test_s2_0014_is_the_single_consecutive_storage_only_successor() -> None:
    assert MIGRATION.name == MIGRATION_NAME
    assert MIGRATION.version == PREDECESSOR_VERSION + 1
    assert [migration.version for migration in load_migrations()] == list(
        range(1, MIGRATION_VERSION + 1)
    )
    assert set(TABLES) <= {
        match.group(1)
        for match in __import__("re").finditer(
            r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(omnivia_\w+)", MIGRATION.sql
        )
    }
    lowered = MIGRATION.sql.lower()
    for forbidden in ("deferrable", "legacy memories", "create view"):
        assert forbidden not in lowered


def test_s2_0014_clean_upgrade_has_exact_catalogue_schema_and_guards(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        rows = {
            tuple(row)
            for row in connection.execute(
                "SELECT record_type, content_schema_version, primary_member "
                "FROM omnivia_governed_schema_catalogue"
            )
        }
        assert rows == CATALOGUE_ROWS
        assert set(TABLES) <= m1.object_names(connection, "table")
        assert not any(
            name.startswith("omnivia_migration_0014_")
            for kind in ("table", "trigger")
            for name in m1.object_names(connection, kind)
        )
        assert applied_migrations(connection)[MIGRATION_VERSION] == MIGRATION.checksum
        assert foreign_key_check(connection) == []
        assert integrity_check(connection) == []
        assert_guards_intact(connection)
        expected = canonical_schema_fingerprint()
        assert verify_fingerprint(connection, expected).matches(expected)
    finally:
        connection.close()


def test_s2_0014_missing_catalogue_guard_aborts_without_partial_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-guard.sqlite"
    _apply_through(path, PREDECESSOR_VERSION)
    raw = sqlite3.connect(path)
    try:
        raw.execute("DROP TRIGGER omnivia_guard_governed_schema_catalogue_insert")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(
        sqlite3.DatabaseError, match="all three frozen catalogue guards"
    ):
        _upgrade_from_13(path)

    connection = sqlite3.connect(path)
    try:
        assert MIGRATION_VERSION not in applied_migrations(connection)
        assert not (set(TABLES) & m1.object_names(connection, "table"))
        assert connection.execute(
            "SELECT COUNT(*) FROM omnivia_governed_schema_catalogue "
            "WHERE record_type='memory.fact'"
        ).fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize("statement", ("insert", "update", "delete"))
def test_v06_5_s2_0014_refuses_same_named_but_altered_catalogue_guard(
    tmp_path: Path, statement: str
) -> None:
    path = tmp_path / f"altered-{statement}.sqlite"
    _apply_through(path, PREDECESSOR_VERSION)
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        connection.execute(
            f"DROP TRIGGER omnivia_guard_governed_schema_catalogue_{statement}"
        )
        connection.execute(
            f"CREATE TRIGGER omnivia_guard_governed_schema_catalogue_{statement} "
            f"BEFORE {statement.upper()} ON omnivia_governed_schema_catalogue "
            "BEGIN SELECT 1; END"
        )
    finally:
        connection.close()

    with pytest.raises(
        sqlite3.DatabaseError, match="exact frozen catalogue guard definitions"
    ):
        _upgrade_from_13(path)

    check = open_database(path, OpenMode.READ_ONLY)
    try:
        assert MIGRATION_VERSION not in applied_migrations(check)
        assert check.execute(
            "SELECT COUNT(*) FROM omnivia_governed_schema_catalogue"
        ).fetchone() == (13,)
    finally:
        check.close()


def test_s2_0014_dirty_thirteen_row_catalogue_aborts_without_partial_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dirty-catalogue.sqlite"
    _apply_through(path, PREDECESSOR_VERSION)
    raw = sqlite3.connect(path)
    try:
        raw.executescript(
            "DROP TRIGGER omnivia_guard_governed_schema_catalogue_update;"
            "UPDATE omnivia_governed_schema_catalogue SET primary_member='changed' "
            "WHERE record_type='knowledge.claim';"
            "CREATE TRIGGER omnivia_guard_governed_schema_catalogue_update "
            "BEFORE UPDATE ON omnivia_governed_schema_catalogue BEGIN "
            "SELECT RAISE(ABORT, 'omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; UPDATE is never permitted'); END;"
        )
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(sqlite3.DatabaseError, match="exact 13-row"):
        _upgrade_from_13(path)
    connection = sqlite3.connect(path)
    try:
        assert MIGRATION_VERSION not in applied_migrations(connection)
        assert not (set(TABLES) & m1.object_names(connection, "table"))
        assert connection.execute(
            "SELECT COUNT(*) FROM omnivia_governed_schema_catalogue "
            "WHERE record_type='memory.fact'"
        ).fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize("stop_after", range(1, len(MIGRATION_STATEMENTS) + 1))
def test_v06_5_s2_0014_exact_catalogue_extension_and_recovery(
    tmp_path: Path, stop_after: int
) -> None:
    path = tmp_path / f"interrupted-{stop_after}.sqlite"
    _apply_through(path, PREDECESSOR_VERSION)
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        crashing = m1.FailAfterStatement(connection, MIGRATION_STATEMENTS, stop_after)
        with (
            m1.migration_catalogue_through(MIGRATION_VERSION),
            pytest.raises(m1.MigrationInterrupted, match=f"statement {stop_after}$"),
        ):
            apply_pending_migrations(
                cast("sqlite3.Connection", crashing),
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=m1.GENERATION_ONE,
                workspace_id=m1.WORKSPACE_ID,
            )
        assert crashing.executed == stop_after
    finally:
        connection.close()

    interrupted = sqlite3.connect(path)
    try:
        assert MIGRATION_VERSION not in applied_migrations(interrupted)
        assert not (set(TABLES) & m1.object_names(interrupted, "table"))
        assert interrupted.execute(
            "SELECT COUNT(*) FROM omnivia_governed_schema_catalogue "
            "WHERE record_type='memory.fact'"
        ).fetchone() == (0,)
        assert foreign_key_check(interrupted) == []
        assert integrity_check(interrupted) == []
    finally:
        interrupted.close()

    _upgrade_from_13(path)
    converged = open_database(path, OpenMode.READ_ONLY)
    try:
        assert set(TABLES) <= m1.object_names(converged, "table")
        assert {
            tuple(row)
            for row in converged.execute(
                "SELECT record_type, content_schema_version, primary_member "
                "FROM omnivia_governed_schema_catalogue"
            )
        } == CATALOGUE_ROWS
        assert verify_fingerprint(converged, canonical_schema_fingerprint()).matches(
            canonical_schema_fingerprint()
        )
    finally:
        converged.close()


def test_v06_5_s2_old_binary_refuses_0014_workspace(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        with m1.migration_catalogue_through(PREDECESSOR_VERSION):
            old_expected = canonical_schema_fingerprint()
            with pytest.raises(SchemaDrift, match="fingerprint differs"):
                verify_fingerprint(connection, old_expected)
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
    finally:
        connection.close()


def _audit(operation: str, audit_ref: str, settled_at_us: int) -> dict[str, object]:
    row = m1.row_for(
        "omnivia_application_audit_events",
        audit_ref=audit_ref,
        workspace_id=m1.WORKSPACE_ID,
        operation=operation,
        purpose="memory.write"
        if operation == "memory.create"
        else "knowledge.governance",
        request_id=f"request-{audit_ref}",
        correlation_id=f"correlation-{audit_ref}",
        trace_id=f"trace-{audit_ref}",
        recorded_at_us=settled_at_us,
    )
    return row


def _claim(audit_ref: str) -> dict[str, object]:
    return m1.row_for(
        "omnivia_idempotency_claims",
        claim_id=f"claim-{audit_ref}",
        workspace_id=m1.WORKSPACE_ID,
        operation="memory.create",
        audit_ref=audit_ref,
    )


def test_s2_0014_referenced_outcome_requires_exact_document_and_preserves_inline(
    owned: m1.Owned,
) -> None:
    audit_ref = "audit-reference"
    claim_id = f"claim-{audit_ref}"
    document = json.dumps({"payload": "x" * 8300}, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(document.encode()).hexdigest()
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m1.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        m1.insert(
            owned.connection,
            m1.M1_TABLES[0],
            _audit("memory.create", audit_ref, 1_800_000_000_000_000),
        )
        m1.insert(owned.connection, m1.M1_TABLES[1], _claim(audit_ref))
        m1.insert(
            owned.connection,
            DOCUMENTS,
            {
                "workspace_id": m1.WORKSPACE_ID,
                "outcome_reference": "outcome://reference",
                "claim_id": claim_id,
                "audit_ref": audit_ref,
                "outcome_json": document,
                "outcome_digest": digest,
                "outcome_byte_length": len(document.encode()),
            },
        )
        m1.insert(
            owned.connection,
            m1.M1_TABLES[2],
            m1.row_for(
                m1.M1_TABLES[2],
                outcome_id="outcome-reference",
                claim_id=claim_id,
                workspace_id=m1.WORKSPACE_ID,
                outcome_json=None,
                outcome_reference="outcome://reference",
                outcome_digest=digest,
                audit_ref=audit_ref,
            ),
        )

    assert owned.connection.execute(
        f"SELECT outcome_json, outcome_digest FROM {DOCUMENTS} WHERE claim_id=?",
        (claim_id,),
    ).fetchone() == (document, digest)

    # 0007's inline branch is unchanged.
    m1.seed_triad(owned)
    assert owned.connection.execute(
        "SELECT outcome_json, outcome_reference FROM omnivia_idempotency_outcomes "
        "WHERE outcome_id='out-0001'"
    ).fetchone() == ('{"status":"ok"}', None)


def test_s2_0014_referenced_outcome_mismatch_and_early_document_fail_immediately(
    owned: m1.Owned,
) -> None:
    audit_ref = "audit-mismatch"
    claim_id = f"claim-{audit_ref}"
    document = json.dumps({"payload": "y" * 8300}, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(document.encode()).hexdigest()

    with (
        pytest.raises(
            sqlite3.DatabaseError, match="names an audit event its claim does not"
        ),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=m1.WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        m1.insert(
            owned.connection,
            DOCUMENTS,
            {
                "workspace_id": m1.WORKSPACE_ID,
                "outcome_reference": "outcome://early",
                "claim_id": claim_id,
                "audit_ref": audit_ref,
                "outcome_json": document,
                "outcome_digest": digest,
                "outcome_byte_length": len(document.encode()),
            },
        )

    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=m1.WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        m1.insert(
            owned.connection,
            m1.M1_TABLES[0],
            _audit("memory.create", audit_ref, 1_800_000_000_000_100),
        )
        m1.insert(owned.connection, m1.M1_TABLES[1], _claim(audit_ref))
        m1.insert(
            owned.connection,
            DOCUMENTS,
            {
                "workspace_id": m1.WORKSPACE_ID,
                "outcome_reference": "outcome://mismatch",
                "claim_id": claim_id,
                "audit_ref": audit_ref,
                "outcome_json": document,
                "outcome_digest": digest,
                "outcome_byte_length": len(document.encode()),
            },
        )

    with (
        pytest.raises(sqlite3.DatabaseError, match="no exact canonical document"),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=m1.WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        m1.insert(
            owned.connection,
            m1.M1_TABLES[2],
            m1.row_for(
                m1.M1_TABLES[2],
                outcome_id="outcome-mismatch",
                claim_id=claim_id,
                workspace_id=m1.WORKSPACE_ID,
                outcome_json=None,
                outcome_reference="outcome://mismatch",
                outcome_digest="sha256:" + "f" * 64,
                audit_ref=audit_ref,
            ),
        )


@pytest.fixture
def governed_owned(tmp_path: Path) -> Iterator[m3.m2.Owned]:
    path = tmp_path / "governed.sqlite"
    _apply_through(path, MIGRATION_VERSION, workspace_id=m3.WORKSPACE_ID)
    holder = m3.m2.take_ownership(path)
    m3.m2.seed_chain(holder)
    yield holder
    holder.connection.close()


def _seed_application_candidate(
    holder: m3.m2.Owned,
    *,
    operation: str,
    audit_ref: str,
    assembly_id: str,
    version_id: str,
    settled_at_us: int,
    claim_json: str,
    claim_ingested_at_us: int,
    create_record: bool,
) -> None:
    claim_digest = "sha256:" + hashlib.sha256(claim_json.encode()).hexdigest()
    audit = m3.audit_row(audit_ref)
    audit.update(
        operation=operation,
        principal_id="principal-1",
        recorded_at_us=settled_at_us,
    )
    assembly = m3.assembly_row(
        assembly_id,
        version_id,
        "record-memory",
        record_type="memory.fact",
        audit_ref=audit_ref,
        correlation_id=audit_ref,
    )
    assembly.update(
        content_json='{"fact":"durable memory"}',
        recorded_at_us=settled_at_us,
    )
    event = m3.event_row(
        f"event-{assembly_id}",
        assembly_id,
        version_id,
        "candidate.human_proposed",
        audit_ref=audit_ref,
        correlation_id=audit_ref,
    )
    event.update(occurred_at_us=settled_at_us - 1, recorded_at_us=settled_at_us)
    seal = m3.seal_row(
        assembly_id,
        version_id,
        seal_id=f"seal-{assembly_id}",
        correlation_id=audit_ref,
    )
    seal["sealed_at_us"] = settled_at_us

    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        m3.insert(holder.connection, "omnivia_application_audit_events", audit)
        if create_record:
            m3.insert(
                holder.connection,
                m3.RECORDS,
                m3.record_row("record-memory", record_type="memory.fact"),
            )
        m3.insert(holder.connection, m3.ASSEMBLIES, assembly)
        m3.insert(holder.connection, m3.EVENTS, event)
        m3.insert(
            holder.connection,
            m3.LINKS,
            m3.link_row(f"event-{assembly_id}", assembly_id),
        )
        m3.insert(holder.connection, m3.SEALS, seal)
        m3.insert(
            holder.connection,
            CLAIMS,
            {
                "workspace_id": m3.WORKSPACE_ID,
                "assembly_id": assembly_id,
                "governed_record_version_id": version_id,
                "operation": operation,
                "audit_ref": audit_ref,
                "claim_json": claim_json,
                "claim_digest": claim_digest,
                "claim_byte_length": len(claim_json.encode()),
                "claim_ingested_at_us": claim_ingested_at_us,
                "settled_at_us": settled_at_us,
            },
        )


def test_v06_5_s2_0014_transition_and_outcome_bridges_are_guarded(
    governed_owned: m3.m2.Owned,
) -> None:
    claim_json = '{"content":{"fact":"durable memory"},"record_type":"memory.fact"}'
    first = 1_900_000_000_000_000
    second = first + 100
    _seed_application_candidate(
        governed_owned,
        operation="memory.create",
        audit_ref="audit-memory-create",
        assembly_id="assembly-proposed",
        version_id="version-proposed",
        settled_at_us=first,
        claim_json=claim_json,
        claim_ingested_at_us=first,
        create_record=True,
    )
    _seed_application_candidate(
        governed_owned,
        operation="knowledge.propose",
        audit_ref="audit-knowledge-propose",
        assembly_id="assembly-candidate",
        version_id="version-candidate",
        settled_at_us=second,
        claim_json=claim_json,
        claim_ingested_at_us=first,
        create_record=False,
    )
    rationale = '{"reason_code":"knowledge.proposed"}'
    invalid_transition = {
        "workspace_id": m3.WORKSPACE_ID,
        "transition_id": "transition-propose",
        "governed_record_id": "record-memory",
        "source_assembly_id": "assembly-proposed",
        "source_record_version_id": "version-proposed",
        "target_assembly_id": "assembly-candidate",
        "target_record_version_id": "version-candidate",
        "operation": "knowledge.propose",
        "rationale_json": rationale,
        "rationale_digest": "sha256:" + hashlib.sha256(rationale.encode()).hexdigest(),
        "rationale_byte_length": len(rationale.encode()),
        "reason_code": "knowledge.proposed",
        "reason_comment": None,
        "actor_id": "principal-1",
        "actor_kind": "fabricated-kind",
        "audit_ref": "audit-knowledge-propose",
        "settled_at_us": second,
    }
    with (
        pytest.raises(sqlite3.DatabaseError, match="target does not own"),
        fenced_transaction(
            governed_owned.connection,
            governed_owned.identity,
            workspace_id=m3.WORKSPACE_ID,
            fencing_generation=governed_owned.generation,
        ),
    ):
        m3.insert(governed_owned.connection, TRANSITIONS, invalid_transition)

    with fenced_transaction(
        governed_owned.connection,
        governed_owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=governed_owned.generation,
    ):
        m3.insert(
            governed_owned.connection,
            TRANSITIONS,
            {**invalid_transition, "actor_kind": "human"},
        )

    rows = governed_owned.connection.execute(
        f"SELECT operation, claim_json, claim_ingested_at_us, settled_at_us FROM {CLAIMS} ORDER BY settled_at_us"
    ).fetchall()
    assert rows == [
        ("memory.create", claim_json, first, first),
        ("knowledge.propose", claim_json, first, second),
    ]
    assert governed_owned.connection.execute(
        f"SELECT operation, source_record_version_id, target_record_version_id FROM {TRANSITIONS}"
    ).fetchone() == ("knowledge.propose", "version-proposed", "version-candidate")

    outcome_document = json.dumps({"payload": "z" * 8300}, separators=(",", ":"))
    with fenced_transaction(
        governed_owned.connection,
        governed_owned.identity,
        workspace_id=m3.WORKSPACE_ID,
        fencing_generation=governed_owned.generation,
    ):
        m1.insert(
            governed_owned.connection,
            m1.M1_TABLES[1],
            {
                **_claim("audit-memory-create"),
                "workspace_id": m3.WORKSPACE_ID,
            },
        )
        m1.insert(
            governed_owned.connection,
            DOCUMENTS,
            {
                "workspace_id": m3.WORKSPACE_ID,
                "outcome_reference": "outcome://guard-proof",
                "claim_id": "claim-audit-memory-create",
                "audit_ref": "audit-memory-create",
                "outcome_json": outcome_document,
                "outcome_digest": "sha256:"
                + hashlib.sha256(outcome_document.encode()).hexdigest(),
                "outcome_byte_length": len(outcome_document.encode()),
            },
        )

    for table in TABLES:
        with (
            pytest.raises(sqlite3.DatabaseError, match="append-only"),
            fenced_transaction(
                governed_owned.connection,
                governed_owned.identity,
                workspace_id=m3.WORKSPACE_ID,
                fencing_generation=governed_owned.generation,
            ),
        ):
            governed_owned.connection.execute(f"DELETE FROM {table}")


def test_s2_0014_claim_preserving_transition_refuses_changed_lineage(
    governed_owned: m3.m2.Owned,
) -> None:
    first = 1_900_000_000_001_000
    second = first + 100
    _seed_application_candidate(
        governed_owned,
        operation="memory.create",
        audit_ref="audit-source",
        assembly_id="assembly-source",
        version_id="version-source",
        settled_at_us=first,
        claim_json='{"content":{"fact":"source"},"record_type":"memory.fact"}',
        claim_ingested_at_us=first,
        create_record=True,
    )
    _seed_application_candidate(
        governed_owned,
        operation="knowledge.propose",
        audit_ref="audit-target",
        assembly_id="assembly-target",
        version_id="version-target",
        settled_at_us=second,
        claim_json='{"content":{"fact":"changed"},"record_type":"memory.fact"}',
        claim_ingested_at_us=first,
        create_record=False,
    )
    rationale = '{"reason_code":"knowledge.proposed"}'
    with (
        pytest.raises(sqlite3.DatabaseError, match="changed canonical claim lineage"),
        fenced_transaction(
            governed_owned.connection,
            governed_owned.identity,
            workspace_id=m3.WORKSPACE_ID,
            fencing_generation=governed_owned.generation,
        ),
    ):
        m3.insert(
            governed_owned.connection,
            TRANSITIONS,
            {
                "workspace_id": m3.WORKSPACE_ID,
                "transition_id": "transition-changed",
                "governed_record_id": "record-memory",
                "source_assembly_id": "assembly-source",
                "source_record_version_id": "version-source",
                "target_assembly_id": "assembly-target",
                "target_record_version_id": "version-target",
                "operation": "knowledge.propose",
                "rationale_json": rationale,
                "rationale_digest": "sha256:"
                + hashlib.sha256(rationale.encode()).hexdigest(),
                "rationale_byte_length": len(rationale.encode()),
                "reason_code": "knowledge.proposed",
                "reason_comment": None,
                "actor_id": "principal-1",
                "actor_kind": "human",
                "audit_ref": "audit-target",
                "settled_at_us": second,
            },
        )
