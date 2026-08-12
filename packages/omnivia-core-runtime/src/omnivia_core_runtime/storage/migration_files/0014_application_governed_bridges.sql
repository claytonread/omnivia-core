-- Application-authored governed lineage, transition and referenced-outcome bridges.
--
-- This migration is deliberately storage-only.  It extends the frozen M3 catalogue
-- by its one accepted application type, then adds immutable facts which let the
-- application layer preserve exact public claim and transition semantics without
-- weakening the 0007/0009 stores or inventing a second canonical record store.
-- Every foreign key remains immediate and every durable table is append-only.

-- Fail closed unless the catalogue and all three of its unconditional guards are
-- exactly the accepted 0009 predecessor.  The gate objects are migration-local in
-- lifetime: the whole migration is one transaction and they are dropped below.
CREATE TABLE omnivia_migration_0014_predecessor_gate (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1)
) WITHOUT ROWID;

CREATE TRIGGER omnivia_migration_0014_predecessor_gate_insert
BEFORE INSERT ON omnivia_migration_0014_predecessor_gate
BEGIN
    SELECT RAISE(ABORT, 'omnivia: migration 0014 requires the exact 13-row governed schema catalogue')
    WHERE (SELECT COUNT(*) FROM omnivia_governed_schema_catalogue) <> 13
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.claim' AND content_schema_version = '1.0' AND primary_member = 'statement')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.decision' AND content_schema_version = '1.0' AND primary_member = 'decision')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.proposal' AND content_schema_version = '1.0' AND primary_member = 'proposal')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.requirement' AND content_schema_version = '1.0' AND primary_member = 'requirement')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.constraint' AND content_schema_version = '1.0' AND primary_member = 'constraint')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.assumption' AND content_schema_version = '1.0' AND primary_member = 'assumption')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.preference' AND content_schema_version = '1.0' AND primary_member = 'preference')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.question' AND content_schema_version = '1.0' AND primary_member = 'question')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.finding' AND content_schema_version = '1.0' AND primary_member = 'finding')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.risk' AND content_schema_version = '1.0' AND primary_member = 'risk')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.outcome' AND content_schema_version = '1.0' AND primary_member = 'outcome')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.entity' AND content_schema_version = '1.0' AND primary_member = 'name')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.relation' AND content_schema_version = '1.0' AND primary_member = 'statement');
    SELECT RAISE(ABORT, 'omnivia: migration 0014 requires all three frozen catalogue guards')
    WHERE (SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'omnivia_governed_schema_catalogue' AND name IN ('omnivia_guard_governed_schema_catalogue_insert', 'omnivia_guard_governed_schema_catalogue_update', 'omnivia_guard_governed_schema_catalogue_delete')) <> 3;
    SELECT RAISE(ABORT, 'omnivia: migration 0014 requires the exact frozen catalogue guard definitions')
    WHERE (SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'omnivia_guard_governed_schema_catalogue_insert') <> 'CREATE TRIGGER omnivia_guard_governed_schema_catalogue_insert
BEFORE INSERT ON omnivia_governed_schema_catalogue
BEGIN
    SELECT RAISE(ABORT, ''omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; INSERT is never permitted'');
END'
       OR (SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'omnivia_guard_governed_schema_catalogue_update') <> 'CREATE TRIGGER omnivia_guard_governed_schema_catalogue_update
BEFORE UPDATE ON omnivia_governed_schema_catalogue
BEGIN
    SELECT RAISE(ABORT, ''omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; UPDATE is never permitted'');
END'
       OR (SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'omnivia_guard_governed_schema_catalogue_delete') <> 'CREATE TRIGGER omnivia_guard_governed_schema_catalogue_delete
BEFORE DELETE ON omnivia_governed_schema_catalogue
BEGIN
    SELECT RAISE(ABORT, ''omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; DELETE is never permitted'');
END';
END;

INSERT INTO omnivia_migration_0014_predecessor_gate (singleton) VALUES (1);
DROP TRIGGER omnivia_migration_0014_predecessor_gate_insert;
DROP TABLE omnivia_migration_0014_predecessor_gate;

-- Only the INSERT guard is opened, for exactly one statement.  UPDATE and DELETE
-- remain closed throughout the migration.
DROP TRIGGER omnivia_guard_governed_schema_catalogue_insert;

INSERT INTO omnivia_governed_schema_catalogue
    (record_type, content_schema_version, primary_member)
VALUES ('memory.fact', '1.0', 'fact');

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_schema_catalogue_insert
BEFORE INSERT ON omnivia_governed_schema_catalogue
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; INSERT is never permitted');
END;

-- Prove the postcondition after the guard has been restored.  Count plus existence
-- of all fourteen primary-keyed rows proves there is no duplicate or extra row.
CREATE TABLE omnivia_migration_0014_result_gate (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1)
) WITHOUT ROWID;

CREATE TRIGGER omnivia_migration_0014_result_gate_insert
BEFORE INSERT ON omnivia_migration_0014_result_gate
BEGIN
    SELECT RAISE(ABORT, 'omnivia: migration 0014 did not produce the exact 14-row governed schema catalogue')
    WHERE (SELECT COUNT(*) FROM omnivia_governed_schema_catalogue) <> 14
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.claim' AND content_schema_version = '1.0' AND primary_member = 'statement')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.decision' AND content_schema_version = '1.0' AND primary_member = 'decision')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.proposal' AND content_schema_version = '1.0' AND primary_member = 'proposal')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.requirement' AND content_schema_version = '1.0' AND primary_member = 'requirement')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.constraint' AND content_schema_version = '1.0' AND primary_member = 'constraint')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.assumption' AND content_schema_version = '1.0' AND primary_member = 'assumption')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.preference' AND content_schema_version = '1.0' AND primary_member = 'preference')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.question' AND content_schema_version = '1.0' AND primary_member = 'question')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.finding' AND content_schema_version = '1.0' AND primary_member = 'finding')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.risk' AND content_schema_version = '1.0' AND primary_member = 'risk')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.outcome' AND content_schema_version = '1.0' AND primary_member = 'outcome')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.entity' AND content_schema_version = '1.0' AND primary_member = 'name')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'knowledge.relation' AND content_schema_version = '1.0' AND primary_member = 'statement')
       OR NOT EXISTS (SELECT 1 FROM omnivia_governed_schema_catalogue WHERE record_type = 'memory.fact' AND content_schema_version = '1.0' AND primary_member = 'fact');
    SELECT RAISE(ABORT, 'omnivia: migration 0014 did not restore all frozen catalogue guards')
    WHERE (SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'omnivia_governed_schema_catalogue' AND name IN ('omnivia_guard_governed_schema_catalogue_insert', 'omnivia_guard_governed_schema_catalogue_update', 'omnivia_guard_governed_schema_catalogue_delete')) <> 3;
    SELECT RAISE(ABORT, 'omnivia: migration 0014 did not restore the exact frozen catalogue guard definitions')
    WHERE (SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'omnivia_guard_governed_schema_catalogue_insert') <> 'CREATE TRIGGER omnivia_guard_governed_schema_catalogue_insert
BEFORE INSERT ON omnivia_governed_schema_catalogue
BEGIN
    SELECT RAISE(ABORT, ''omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; INSERT is never permitted'');
END'
       OR (SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'omnivia_guard_governed_schema_catalogue_update') <> 'CREATE TRIGGER omnivia_guard_governed_schema_catalogue_update
BEFORE UPDATE ON omnivia_governed_schema_catalogue
BEGIN
    SELECT RAISE(ABORT, ''omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; UPDATE is never permitted'');
END'
       OR (SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'omnivia_guard_governed_schema_catalogue_delete') <> 'CREATE TRIGGER omnivia_guard_governed_schema_catalogue_delete
BEFORE DELETE ON omnivia_governed_schema_catalogue
BEGIN
    SELECT RAISE(ABORT, ''omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; DELETE is never permitted'');
END';
END;

INSERT INTO omnivia_migration_0014_result_gate (singleton) VALUES (1);
DROP TRIGGER omnivia_migration_0014_result_gate_insert;
DROP TABLE omnivia_migration_0014_result_gate;

-- One exact canonical MemoryCreateInput claim per application-authored assembly.
-- The claim is provenance for the 0009 assembly, never an independently mutable
-- record: operation/state/authority/content remain owned by 0007 and 0009.
CREATE TABLE omnivia_application_claim_lineage (
    workspace_id               TEXT    NOT NULL,
    assembly_id                TEXT    NOT NULL,
    governed_record_version_id TEXT    NOT NULL,
    operation                  TEXT    NOT NULL,
    audit_ref                  TEXT    NOT NULL,
    claim_json                 TEXT    NOT NULL,
    claim_digest               TEXT    NOT NULL,
    claim_byte_length          INTEGER NOT NULL,
    claim_ingested_at_us       INTEGER NOT NULL,
    settled_at_us              INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, assembly_id),
    UNIQUE (workspace_id, governed_record_version_id),

    CHECK (length(workspace_id) BETWEEN 1 AND 128),
    CHECK (length(assembly_id) BETWEEN 1 AND 128),
    CHECK (length(governed_record_version_id) BETWEEN 1 AND 128),
    CHECK (operation IN ('memory.create', 'knowledge.propose', 'candidate.approve', 'candidate.reject', 'record.supersede')),
    CHECK (length(audit_ref) BETWEEN 1 AND 128),
    CHECK (typeof(claim_json) = 'text' AND length(CAST(claim_json AS BLOB)) >= 2),
    CHECK (typeof(claim_byte_length) = 'integer' AND claim_byte_length = length(CAST(claim_json AS BLOB))),
    CHECK (length(claim_digest) = 71 AND substr(claim_digest, 1, 7) = 'sha256:' AND substr(claim_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(claim_ingested_at_us) = 'integer' AND claim_ingested_at_us > 0),
    CHECK (typeof(settled_at_us) = 'integer' AND settled_at_us > 0),
    CHECK (claim_ingested_at_us <= settled_at_us),

    FOREIGN KEY (workspace_id, assembly_id, governed_record_version_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, assembly_id, governed_record_version_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_version_assemblies_application_identity
    ON omnivia_governed_version_assemblies
        (workspace_id, assembly_id, governed_record_version_id);

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_application_claim_lineage_version
    ON omnivia_application_claim_lineage
        (workspace_id, governed_record_version_id);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_claim_lineage_insert
BEFORE INSERT ON omnivia_application_claim_lineage
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_application_claim_lineage')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1
            FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1
              AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_claim_lineage_consistency
BEFORE INSERT ON omnivia_application_claim_lineage
BEGIN
    SELECT RAISE(ABORT, 'omnivia: application claim lineage contradicts its governed assembly or audit')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        JOIN omnivia_governed_version_seals s
          ON s.workspace_id = a.workspace_id
         AND s.assembly_id = a.assembly_id
         AND s.governed_record_version_id = a.governed_record_version_id
        JOIN omnivia_application_audit_events e
          ON e.audit_ref = NEW.audit_ref
         AND e.workspace_id = NEW.workspace_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND a.governed_record_version_id = NEW.governed_record_version_id
          AND a.correlation_kind = 'm1_audit'
          AND a.correlation_id = NEW.audit_ref
          AND a.audit_ref = NEW.audit_ref
          AND a.recorded_at_us = NEW.settled_at_us
          AND s.sealed_at_us = NEW.settled_at_us
          AND e.operation = NEW.operation
          AND e.recorded_at_us = NEW.settled_at_us
          AND (
            (NEW.operation IN ('memory.create', 'knowledge.propose')
             AND a.layer = 'candidate'
             AND a.authority_level = 'proposed'
             AND a.candidate_origin = 'human_proposed')
            OR (NEW.operation = 'candidate.approve'
                AND a.layer = 'governed'
                AND a.governance_disposition = 'accepted'
                AND a.authority_level = 'canonical')
            OR (NEW.operation = 'candidate.reject'
                AND a.layer = 'governed'
                AND a.governance_disposition = 'rejected'
                AND a.authority_level = 'reviewed')
            OR (NEW.operation = 'record.supersede'
                AND a.layer = 'governed'
                AND a.governance_disposition = 'accepted'
                AND a.authority_level = 'canonical')
          )
    );
    SELECT RAISE(ABORT, 'omnivia: a new application claim must use the settlement-start instant')
    WHERE NEW.operation IN ('memory.create', 'record.supersede')
      AND NEW.claim_ingested_at_us <> NEW.settled_at_us;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_claim_lineage_update
BEFORE UPDATE ON omnivia_application_claim_lineage
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_claim_lineage is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_claim_lineage_delete
BEFORE DELETE ON omnivia_application_claim_lineage
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_claim_lineage is append-only; DELETE is never permitted');
END;

-- One public governance transition between two already-sealed application versions.
-- Public state is derived from the operation and the verified endpoints, so this row
-- carries no caller-writable state, authority, currentness or reviewer decision.
CREATE TABLE omnivia_application_governance_transitions (
    workspace_id               TEXT    NOT NULL,
    transition_id              TEXT    NOT NULL,
    governed_record_id         TEXT    NOT NULL,
    source_assembly_id         TEXT    NOT NULL,
    source_record_version_id   TEXT    NOT NULL,
    target_assembly_id         TEXT    NOT NULL,
    target_record_version_id   TEXT    NOT NULL,
    operation                  TEXT    NOT NULL,
    rationale_json             TEXT    NOT NULL,
    rationale_digest           TEXT    NOT NULL,
    rationale_byte_length      INTEGER NOT NULL,
    reason_code                TEXT    NOT NULL,
    reason_comment             TEXT,
    actor_id                   TEXT    NOT NULL,
    actor_kind                 TEXT    NOT NULL,
    audit_ref                  TEXT    NOT NULL,
    settled_at_us              INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, transition_id),
    UNIQUE (workspace_id, governed_record_id, source_assembly_id, source_record_version_id),
    UNIQUE (workspace_id, governed_record_id, target_assembly_id, target_record_version_id),

    CHECK (length(workspace_id) BETWEEN 1 AND 128),
    CHECK (length(transition_id) BETWEEN 1 AND 128),
    CHECK (length(governed_record_id) BETWEEN 1 AND 128),
    CHECK (length(source_assembly_id) BETWEEN 1 AND 128),
    CHECK (length(source_record_version_id) BETWEEN 1 AND 128),
    CHECK (length(target_assembly_id) BETWEEN 1 AND 128),
    CHECK (length(target_record_version_id) BETWEEN 1 AND 128),
    CHECK (source_assembly_id <> target_assembly_id AND source_record_version_id <> target_record_version_id),
    CHECK (operation IN ('knowledge.propose', 'candidate.approve', 'candidate.reject', 'record.supersede')),
    CHECK (typeof(rationale_json) = 'text' AND length(CAST(rationale_json AS BLOB)) BETWEEN 2 AND 4096),
    CHECK (typeof(rationale_byte_length) = 'integer' AND rationale_byte_length = length(CAST(rationale_json AS BLOB))),
    CHECK (length(rationale_digest) = 71 AND substr(rationale_digest, 1, 7) = 'sha256:' AND substr(rationale_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(reason_code) BETWEEN 1 AND 128),
    CHECK (reason_comment IS NULL OR length(reason_comment) BETWEEN 1 AND 2048),
    CHECK (length(actor_id) BETWEEN 1 AND 128),
    CHECK (length(actor_kind) BETWEEN 1 AND 128),
    CHECK (length(audit_ref) BETWEEN 1 AND 128),
    CHECK (typeof(settled_at_us) = 'integer' AND settled_at_us > 0),

    FOREIGN KEY (workspace_id, governed_record_id)
        REFERENCES omnivia_governed_records (workspace_id, governed_record_id),
    FOREIGN KEY (workspace_id, source_assembly_id, source_record_version_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, assembly_id, governed_record_version_id),
    FOREIGN KEY (workspace_id, target_assembly_id, target_record_version_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, assembly_id, governed_record_version_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_application_governance_transitions_source
    ON omnivia_application_governance_transitions
        (workspace_id, governed_record_id, source_assembly_id, source_record_version_id);

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_application_governance_transitions_target
    ON omnivia_application_governance_transitions
        (workspace_id, governed_record_id, target_assembly_id, target_record_version_id);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_governance_transitions_insert
BEFORE INSERT ON omnivia_application_governance_transitions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_application_governance_transitions')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1
            FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1
              AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_governance_transitions_consistency
BEFORE INSERT ON omnivia_application_governance_transitions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: application transition endpoints are not the named sealed versions of one record')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies src
        JOIN omnivia_governed_version_seals ss
          ON ss.workspace_id = src.workspace_id
         AND ss.assembly_id = src.assembly_id
         AND ss.governed_record_version_id = src.governed_record_version_id
        JOIN omnivia_governed_version_assemblies dst
          ON dst.workspace_id = src.workspace_id
         AND dst.governed_record_id = src.governed_record_id
        JOIN omnivia_governed_version_seals ds
          ON ds.workspace_id = dst.workspace_id
         AND ds.assembly_id = dst.assembly_id
         AND ds.governed_record_version_id = dst.governed_record_version_id
        WHERE src.workspace_id = NEW.workspace_id
          AND src.governed_record_id = NEW.governed_record_id
          AND src.assembly_id = NEW.source_assembly_id
          AND src.governed_record_version_id = NEW.source_record_version_id
          AND dst.assembly_id = NEW.target_assembly_id
          AND dst.governed_record_version_id = NEW.target_record_version_id
    );
    SELECT RAISE(ABORT, 'omnivia: application transition target does not own the exact operation audit and settlement')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_application_claim_lineage l
        JOIN omnivia_governed_version_assemblies a
          ON a.workspace_id = l.workspace_id AND a.assembly_id = l.assembly_id
        JOIN omnivia_application_audit_events e
          ON e.audit_ref = l.audit_ref AND e.workspace_id = l.workspace_id
        WHERE l.workspace_id = NEW.workspace_id
          AND l.assembly_id = NEW.target_assembly_id
          AND l.governed_record_version_id = NEW.target_record_version_id
          AND l.operation = NEW.operation
          AND l.audit_ref = NEW.audit_ref
          AND l.settled_at_us = NEW.settled_at_us
          AND a.recorded_at_us = NEW.settled_at_us
          AND e.operation = NEW.operation
          AND e.principal_id = NEW.actor_id
          AND e.recorded_at_us = NEW.settled_at_us
          AND EXISTS (
              SELECT 1
              FROM omnivia_governed_provenance_events p
              WHERE p.workspace_id = NEW.workspace_id
                AND p.assembly_id = NEW.target_assembly_id
                AND p.governed_record_version_id = NEW.target_record_version_id
                AND p.audit_ref = NEW.audit_ref
                AND p.actor_id = NEW.actor_id
                AND p.actor_kind = NEW.actor_kind
          )
    );
    SELECT RAISE(ABORT, 'omnivia: application transition does not follow the accepted public state matrix')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_application_claim_lineage src_l
        JOIN omnivia_governed_version_assemblies src
          ON src.workspace_id = src_l.workspace_id AND src.assembly_id = src_l.assembly_id
        JOIN omnivia_governed_version_assemblies dst
          ON dst.workspace_id = NEW.workspace_id AND dst.assembly_id = NEW.target_assembly_id
        WHERE src_l.workspace_id = NEW.workspace_id
          AND src_l.assembly_id = NEW.source_assembly_id
          AND src_l.governed_record_version_id = NEW.source_record_version_id
          AND (
            (NEW.operation = 'knowledge.propose'
             AND src_l.operation = 'memory.create'
             AND src.layer = 'candidate' AND src.authority_level = 'proposed'
             AND dst.layer = 'candidate' AND dst.authority_level = 'proposed')
            OR (NEW.operation IN ('candidate.approve', 'candidate.reject')
                AND src_l.operation = 'knowledge.propose'
                AND src.layer = 'candidate' AND src.authority_level = 'proposed'
                AND ((NEW.operation = 'candidate.approve' AND dst.layer = 'governed' AND dst.governance_disposition = 'accepted' AND dst.authority_level = 'canonical')
                     OR (NEW.operation = 'candidate.reject' AND dst.layer = 'governed' AND dst.governance_disposition = 'rejected' AND dst.authority_level = 'reviewed')))
            OR (NEW.operation = 'record.supersede'
                AND src_l.operation IN ('candidate.approve', 'record.supersede')
                AND src.layer = 'governed' AND src.governance_disposition = 'accepted' AND src.authority_level = 'canonical'
                AND dst.layer = 'governed' AND dst.governance_disposition = 'accepted' AND dst.authority_level = 'canonical')
          )
    );
    SELECT RAISE(ABORT, 'omnivia: claim-preserving transition changed canonical claim lineage')
    WHERE NEW.operation <> 'record.supersede'
      AND NOT EXISTS (
        SELECT 1
        FROM omnivia_application_claim_lineage src
        JOIN omnivia_application_claim_lineage dst
          ON dst.workspace_id = src.workspace_id
        WHERE src.workspace_id = NEW.workspace_id
          AND src.assembly_id = NEW.source_assembly_id
          AND dst.assembly_id = NEW.target_assembly_id
          AND dst.claim_json = src.claim_json
          AND dst.claim_digest = src.claim_digest
          AND dst.claim_byte_length = src.claim_byte_length
          AND dst.claim_ingested_at_us = src.claim_ingested_at_us
    );
    SELECT RAISE(ABORT, 'omnivia: record.supersede replacement claim was not ingested at settlement start')
    WHERE NEW.operation = 'record.supersede'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_application_claim_lineage dst
        WHERE dst.workspace_id = NEW.workspace_id
          AND dst.assembly_id = NEW.target_assembly_id
          AND dst.claim_ingested_at_us = NEW.settled_at_us
    );
    SELECT RAISE(ABORT, 'omnivia: non-superseding public transition fabricated a canonical supersession edge')
    WHERE NEW.operation <> 'record.supersede'
      AND EXISTS (
        SELECT 1 FROM omnivia_record_supersessions r
        WHERE r.workspace_id = NEW.workspace_id
          AND r.governed_record_id = NEW.governed_record_id
          AND r.source_version_id = NEW.source_record_version_id
          AND r.target_version_id = NEW.target_record_version_id
    );
    SELECT RAISE(ABORT, 'omnivia: record.supersede transition disagrees with its canonical supersession edge')
    WHERE NEW.operation = 'record.supersede'
      AND NOT EXISTS (
        SELECT 1
        FROM omnivia_record_supersessions r
        JOIN omnivia_governed_provenance_events p
          ON p.workspace_id = r.workspace_id
         AND p.assembly_id = r.assembly_id
         AND p.provenance_event_id = r.provenance_event_id
        WHERE r.workspace_id = NEW.workspace_id
          AND r.assembly_id = NEW.target_assembly_id
          AND r.governed_record_id = NEW.governed_record_id
          AND r.source_version_id = NEW.source_record_version_id
          AND r.target_version_id = NEW.target_record_version_id
          AND r.correlation_kind = 'm1_audit'
          AND r.correlation_id = NEW.audit_ref
          AND r.recorded_at_us = NEW.settled_at_us
          AND p.action = 'record.superseded'
          AND p.actor_id = NEW.actor_id
          AND p.actor_kind = NEW.actor_kind
          AND p.reason_code = NEW.reason_code
          AND p.reason_comment IS NEW.reason_comment
          AND p.audit_ref = NEW.audit_ref
          AND p.recorded_at_us = NEW.settled_at_us
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_governance_transitions_update
BEFORE UPDATE ON omnivia_application_governance_transitions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_governance_transitions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_governance_transitions_delete
BEFORE DELETE ON omnivia_application_governance_transitions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_governance_transitions is append-only; DELETE is never permitted');
END;

-- Canonical documents for the already-accepted 0007 outcome_reference branch.
-- A row is not independently servable; readers must reach it through the one settled
-- outcome whose consistency trigger below proves the exact claim/audit/digest link.
CREATE TABLE omnivia_application_outcome_documents (
    workspace_id        TEXT    NOT NULL,
    outcome_reference   TEXT    NOT NULL,
    claim_id            TEXT    NOT NULL,
    audit_ref           TEXT    NOT NULL,
    outcome_json        TEXT    NOT NULL,
    outcome_digest      TEXT    NOT NULL,
    outcome_byte_length INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, outcome_reference),
    UNIQUE (workspace_id, claim_id),

    CHECK (length(workspace_id) BETWEEN 1 AND 128),
    CHECK (length(outcome_reference) BETWEEN 1 AND 256),
    CHECK (length(claim_id) BETWEEN 1 AND 128),
    CHECK (length(audit_ref) BETWEEN 1 AND 128),
    CHECK (typeof(outcome_json) = 'text' AND length(CAST(outcome_json AS BLOB)) > 8192),
    CHECK (typeof(outcome_byte_length) = 'integer' AND outcome_byte_length = length(CAST(outcome_json AS BLOB))),
    CHECK (length(outcome_digest) = 71 AND substr(outcome_digest, 1, 7) = 'sha256:' AND substr(outcome_digest, 8) NOT GLOB '*[^0-9a-f]*'),

    FOREIGN KEY (claim_id, workspace_id)
        REFERENCES omnivia_idempotency_claims (claim_id, workspace_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_application_outcome_documents_claim
    ON omnivia_application_outcome_documents (workspace_id, claim_id);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_outcome_documents_insert
BEFORE INSERT ON omnivia_application_outcome_documents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_application_outcome_documents')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1
            FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1
              AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_outcome_documents_consistency
BEFORE INSERT ON omnivia_application_outcome_documents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: referenced outcome document names an audit event its claim does not')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_idempotency_claims c
        WHERE c.claim_id = NEW.claim_id
          AND c.workspace_id = NEW.workspace_id
          AND c.audit_ref = NEW.audit_ref
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_outcome_documents_update
BEFORE UPDATE ON omnivia_application_outcome_documents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_outcome_documents is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_outcome_documents_delete
BEFORE DELETE ON omnivia_application_outcome_documents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_outcome_documents is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_idempotency_outcomes_reference_document
BEFORE INSERT ON omnivia_idempotency_outcomes
WHEN NEW.outcome_reference IS NOT NULL
 AND NEW.outcome_json IS NULL
 AND length(NEW.outcome_reference) BETWEEN 1 AND 256
 AND NOT EXISTS (
    SELECT 1 FROM omnivia_idempotency_outcomes o
    WHERE o.claim_id = NEW.claim_id
 )
BEGIN
    SELECT RAISE(ABORT, 'omnivia: referenced idempotency outcome has no exact canonical document')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_application_outcome_documents d
        WHERE d.workspace_id = NEW.workspace_id
          AND d.outcome_reference = NEW.outcome_reference
          AND d.claim_id = NEW.claim_id
          AND d.audit_ref = NEW.audit_ref
          AND d.outcome_digest = NEW.outcome_digest
    );
END;
