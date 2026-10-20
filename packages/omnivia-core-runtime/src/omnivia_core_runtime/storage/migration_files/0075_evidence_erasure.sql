-- Physical erasure of direct-captured evidence (LEX-01): `evidence.erase`.
--
-- 0008 made evidence and its children append-only, and made DELETE abort on them for the
-- fenced owner too. That is the right default and this file does not weaken it: it adds
-- the one durable fact that licenses a delete, and teaches nine DELETE guards to read it.
--
--   omnivia_evidence_erasures   one receipt per erased artifact: who, when (the audit
--                               reference names both), which evidence id, and -- only when
--                               they went with it -- its own staged source and the blob
--                               address of its bytes.
--
-- A receipt is the authority. The guards on the artifact, its permission labels, its
-- provenance events and their references, its normalized records and their spans each
-- refuse a DELETE unless the full connection-authority, guard, lease and workspace
-- predicate holds AND a receipt in the same workspace names that evidence id. The guards
-- on the staged source and on the blob object row and its integrity events read the same
-- receipt through the staged source reference and the blob address it carries. With no
-- receipt every one of them aborts with the 0008 message, so the append-only claim is
-- unchanged for everything but a row an erasure receipt names. A receipt can only be
-- written for an artifact that exists, and only with the staged source reference and blob
-- address that artifact carries, so it cannot pre-authorise the deletion of evidence that
-- has not been captured, or of another artifact's staging or bytes. The staged source and
-- blob object rows also keep their foreign keys: another artifact, a document capture or
-- an import claim still holding the row makes the DELETE fail rather than succeed.
--
-- Receipts are themselves append-only (UPDATE and DELETE abort for the owner too), carry
-- no source identity, locator, text or digest of anything but the blob, and are keyed by
-- the audit reference of the mutation that wrote them, so one request writes one receipt.
-- Evidence ids are not reused by this runtime's allocator; a deterministic id that is
-- captured again after an erase is new evidence and a later erase writes a second receipt.
--
-- Nothing here deletes a row. The delete is the handler's, inside one fenced transaction
-- with the audit event, the idempotency claim and this receipt.
--
-- The search projection needs no guard change: a superseded run's documents are already
-- reclaimable, and an erase makes the builder start a new run (see `fts._plan_run`).
-- `omnivia_blob_integrity_events` has no foreign key to the object it describes, on
-- purpose, so a receipt that names the blob address is what lets the observations about
-- an address that has just been erased go with it.
--
-- Every comment in this file sits between statements and never inside one: the migrator
-- strips comments while the canonical schema fingerprint replays the text verbatim.

CREATE TABLE omnivia_migration_0075_predecessor_gate (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1)
) WITHOUT ROWID;

CREATE TRIGGER omnivia_migration_0075_predecessor_gate_insert
BEFORE INSERT ON omnivia_migration_0075_predecessor_gate
BEGIN
    SELECT RAISE(ABORT, 'omnivia: migration 0075 requires the 0008 append-only delete guards')
    WHERE (SELECT COUNT(*) FROM sqlite_master
           WHERE type = 'trigger' AND name = 'omnivia_guard_evidence_artifacts_delete'
             AND instr(sql, 'DELETE is never permitted') > 0) <> 1
       OR (SELECT COUNT(*) FROM sqlite_master
           WHERE type = 'trigger' AND name = 'omnivia_guard_evidence_permission_labels_delete'
             AND instr(sql, 'DELETE is never permitted') > 0) <> 1
       OR (SELECT COUNT(*) FROM sqlite_master
           WHERE type = 'trigger' AND name = 'omnivia_guard_evidence_provenance_events_delete'
             AND instr(sql, 'DELETE is never permitted') > 0) <> 1
       OR (SELECT COUNT(*) FROM sqlite_master
           WHERE type = 'trigger' AND name = 'omnivia_guard_evidence_event_references_delete'
             AND instr(sql, 'DELETE is never permitted') > 0) <> 1
       OR (SELECT COUNT(*) FROM sqlite_master
           WHERE type = 'trigger' AND name = 'omnivia_guard_normalized_source_records_delete'
             AND instr(sql, 'DELETE is never permitted') > 0) <> 1
       OR (SELECT COUNT(*) FROM sqlite_master
           WHERE type = 'trigger' AND name = 'omnivia_guard_normalized_source_spans_delete'
             AND instr(sql, 'DELETE is never permitted') > 0) <> 1
       OR (SELECT COUNT(*) FROM sqlite_master
           WHERE type = 'trigger' AND name = 'omnivia_guard_staged_sources_delete'
             AND instr(sql, 'DELETE is never permitted') > 0) <> 1
       OR (SELECT COUNT(*) FROM sqlite_master
           WHERE type = 'trigger' AND name = 'omnivia_guard_blob_objects_delete'
             AND instr(sql, 'DELETE is never permitted') > 0) <> 1
       OR (SELECT COUNT(*) FROM sqlite_master
           WHERE type = 'trigger' AND name = 'omnivia_guard_blob_integrity_events_delete'
             AND instr(sql, 'DELETE is never permitted') > 0) <> 1;
END;

INSERT INTO omnivia_migration_0075_predecessor_gate (singleton) VALUES (1);
DROP TRIGGER omnivia_migration_0075_predecessor_gate_insert;
DROP TABLE omnivia_migration_0075_predecessor_gate;

CREATE TABLE IF NOT EXISTS omnivia_evidence_erasures (
    audit_ref           TEXT    NOT NULL,
    workspace_id        TEXT    NOT NULL,
    evidence_id         TEXT    NOT NULL,
    staged_source_ref   TEXT,
    blob_content_digest TEXT,
    erased_at_us        INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, audit_ref),

    CHECK (
        typeof(audit_ref) = 'text'
        AND length(audit_ref) BETWEEN 1 AND 128
        AND audit_ref GLOB '[A-Za-z0-9]*'
        AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(audit_ref, char(0)) = 0
    ),
    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(evidence_id) = 'text'
        AND length(evidence_id) BETWEEN 1 AND 128
        AND evidence_id GLOB '[A-Za-z0-9]*'
        AND evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(evidence_id, char(0)) = 0
    ),
    CHECK (
        staged_source_ref IS NULL
        OR (
            typeof(staged_source_ref) = 'text'
            AND length(staged_source_ref) BETWEEN 1 AND 512
            AND staged_source_ref NOT GLOB '*[^!-~]*'
            AND instr(staged_source_ref, char(0)) = 0
        )
    ),
    CHECK (
        blob_content_digest IS NULL
        OR (
            typeof(blob_content_digest) = 'text' AND length(blob_content_digest) = 71
            AND substr(blob_content_digest, 1, 7) = 'sha256:'
            AND substr(blob_content_digest, 8) NOT GLOB '*[^0-9a-f]*'
            AND instr(blob_content_digest, char(0)) = 0
        )
    ),
    CHECK (typeof(erased_at_us) = 'integer' AND erased_at_us > 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_evidence_erasures_evidence
    ON omnivia_evidence_erasures (workspace_id, evidence_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_evidence_erasures_staged
    ON omnivia_evidence_erasures (workspace_id, staged_source_ref)
    WHERE staged_source_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS omnivia_idx_evidence_erasures_blob
    ON omnivia_evidence_erasures (workspace_id, blob_content_digest)
    WHERE blob_content_digest IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_erasures_insert
BEFORE INSERT ON omnivia_evidence_erasures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_evidence_erasures')
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
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an erasure receipt must name evidence that exists in this workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts a
        WHERE a.workspace_id = NEW.workspace_id AND a.evidence_id = NEW.evidence_id);
    SELECT RAISE(ABORT, 'omnivia: an erasure receipt names the staged source and blob address its own artifact carries, or none')
    WHERE NEW.staged_source_ref IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts a
        WHERE a.workspace_id = NEW.workspace_id AND a.evidence_id = NEW.evidence_id
          AND a.staged_source_ref = NEW.staged_source_ref);
    SELECT RAISE(ABORT, 'omnivia: an erasure receipt names the staged source and blob address its own artifact carries, or none')
    WHERE NEW.blob_content_digest IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts a
        WHERE a.workspace_id = NEW.workspace_id AND a.evidence_id = NEW.evidence_id
          AND a.blob_content_digest = NEW.blob_content_digest);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_erasures_update
BEFORE UPDATE ON omnivia_evidence_erasures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_erasures is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_erasures_delete
BEFORE DELETE ON omnivia_evidence_erasures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_erasures is append-only; DELETE is never permitted');
END;

DROP TRIGGER omnivia_guard_evidence_artifacts_delete;

CREATE TRIGGER omnivia_guard_evidence_artifacts_delete
BEFORE DELETE ON omnivia_evidence_artifacts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_artifacts is append-only; DELETE is never permitted')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_erasures e
        WHERE e.workspace_id = OLD.workspace_id AND e.evidence_id = OLD.evidence_id);
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_evidence_artifacts')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

DROP TRIGGER omnivia_guard_evidence_permission_labels_delete;

CREATE TRIGGER omnivia_guard_evidence_permission_labels_delete
BEFORE DELETE ON omnivia_evidence_permission_labels
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_permission_labels is append-only; DELETE is never permitted')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_erasures e
        WHERE e.workspace_id = OLD.workspace_id AND e.evidence_id = OLD.evidence_id);
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_evidence_permission_labels')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

DROP TRIGGER omnivia_guard_evidence_provenance_events_delete;

CREATE TRIGGER omnivia_guard_evidence_provenance_events_delete
BEFORE DELETE ON omnivia_evidence_provenance_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_provenance_events is append-only; DELETE is never permitted')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_erasures e
        WHERE e.workspace_id = OLD.workspace_id AND e.evidence_id = OLD.evidence_id);
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_evidence_provenance_events')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

DROP TRIGGER omnivia_guard_evidence_event_references_delete;

CREATE TRIGGER omnivia_guard_evidence_event_references_delete
BEFORE DELETE ON omnivia_evidence_event_references
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_event_references is append-only; DELETE is never permitted')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_erasures e
        WHERE e.workspace_id = OLD.workspace_id AND e.evidence_id = OLD.evidence_id);
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_evidence_event_references')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

DROP TRIGGER omnivia_guard_normalized_source_records_delete;

CREATE TRIGGER omnivia_guard_normalized_source_records_delete
BEFORE DELETE ON omnivia_normalized_source_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_normalized_source_records is append-only; DELETE is never permitted')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_erasures e
        WHERE e.workspace_id = OLD.workspace_id AND e.evidence_id = OLD.evidence_id);
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_normalized_source_records')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

DROP TRIGGER omnivia_guard_normalized_source_spans_delete;

CREATE TRIGGER omnivia_guard_normalized_source_spans_delete
BEFORE DELETE ON omnivia_normalized_source_spans
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_normalized_source_spans is append-only; DELETE is never permitted')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_erasures e
        WHERE e.workspace_id = OLD.workspace_id AND e.evidence_id = OLD.evidence_id);
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_normalized_source_spans')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

DROP TRIGGER omnivia_guard_staged_sources_delete;

CREATE TRIGGER omnivia_guard_staged_sources_delete
BEFORE DELETE ON omnivia_staged_sources
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_staged_sources is append-only; DELETE is never permitted')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_erasures e
        WHERE e.workspace_id = OLD.workspace_id AND e.staged_source_ref = OLD.staged_source_ref);
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_staged_sources')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

DROP TRIGGER omnivia_guard_blob_objects_delete;

CREATE TRIGGER omnivia_guard_blob_objects_delete
BEFORE DELETE ON omnivia_blob_objects
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_blob_objects is append-only; DELETE is never permitted')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_erasures e
        WHERE e.workspace_id = OLD.workspace_id AND e.blob_content_digest = OLD.content_digest);
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_blob_objects')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

DROP TRIGGER omnivia_guard_blob_integrity_events_delete;

CREATE TRIGGER omnivia_guard_blob_integrity_events_delete
BEFORE DELETE ON omnivia_blob_integrity_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_blob_integrity_events is append-only; DELETE is never permitted')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_erasures e
        WHERE e.workspace_id = OLD.workspace_id AND e.blob_content_digest = OLD.content_digest);
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_blob_integrity_events')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;
