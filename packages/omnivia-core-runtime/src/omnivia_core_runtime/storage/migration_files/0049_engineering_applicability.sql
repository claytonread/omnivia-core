-- Engineering applicability, priorities and review attestations
-- (SPEC-CORE-ENGMEM-001, plan PR-E/PR-G; spec §13, §14, §15).
--
-- Additive only; this is the file the reservation 0049 was held for. The families:
--
--   omnivia_engineering_dependencies            one recorded source dependency of
--                                               one exact record version: a
--                                               selector, its meaning, and its
--                                               producer. Written by the
--                                               observation path; read by the
--                                               applicability assessment.
--   omnivia_engineering_assessments             one target-specific applicability
--                                               statement for one exact record
--                                               version at one snapshot,
--                                               append-only: the current value is
--                                               the latest row per
--                                               (record, version, snapshot), and
--                                               no later row can be rewritten
--                                               into an earlier one (§15.1: no
--                                               single `active` flag).
--   omnivia_engineering_context_priorities      one principal's own selection
--                                               preference per exact target, an
--                                               audited upsert that never changes
--                                               governed state (§13.3).
--   omnivia_engineering_review_attestations     one recorded review or
--                                               deterministic-validation event,
--                                               append-only. An attestation
--                                               cannot accept knowledge and
--                                               cannot clear a stale or unknown
--                                               target without evidence; the
--                                               enforcement lives in the
--                                               handler, the row only proves
--                                               what was recorded (§15.5).
--
-- No change-event producer exists yet: assessments are written by review
-- recording and by whoever registers a newer snapshot for a dependent record,
-- and every search preview reports the latest assessment it actually has.

CREATE TABLE IF NOT EXISTS omnivia_engineering_dependencies (
    workspace_id   TEXT    NOT NULL,
    dependency_id  TEXT    NOT NULL,
    record_id      TEXT    NOT NULL,
    version        TEXT    NOT NULL,
    selector_type  TEXT    NOT NULL,
    selector       TEXT    NOT NULL,
    meaning        TEXT    NOT NULL,
    producer       TEXT    NOT NULL,
    recorded_at_us INTEGER NOT NULL,
    audit_ref      TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, dependency_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(dependency_id) = 'text' AND length(dependency_id) BETWEEN 1 AND 128
           AND dependency_id GLOB '[A-Za-z0-9]*'
           AND dependency_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(dependency_id, char(0)) = 0),
    CHECK (typeof(record_id) = 'text' AND length(record_id) BETWEEN 1 AND 128
           AND record_id GLOB '[A-Za-z0-9]*'
           AND record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(record_id, char(0)) = 0),
    CHECK (typeof(version) = 'text' AND length(version) BETWEEN 1 AND 128
           AND version GLOB '[A-Za-z0-9]*'
           AND version NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(version, char(0)) = 0),
    CHECK (selector_type IN ('whole_file', 'source_span', 'symbol', 'config_key',
                             'schema_contract', 'external_evidence')),
    CHECK (typeof(selector) = 'text' AND length(selector) BETWEEN 1 AND 512
           AND instr(selector, char(0)) = 0),
    CHECK (meaning IN ('must_match', 'requires_revalidation_on_change', 'context_only')),
    CHECK (typeof(producer) = 'text' AND length(producer) BETWEEN 1 AND 128),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_assessments (
    workspace_id       TEXT    NOT NULL,
    assessment_id      TEXT    NOT NULL,
    record_id          TEXT    NOT NULL,
    version            TEXT    NOT NULL,
    target_snapshot_id TEXT    NOT NULL,
    status             TEXT    NOT NULL,
    basis              TEXT    NOT NULL,
    assessed_at_us     INTEGER NOT NULL,
    audit_ref          TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, assessment_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(assessment_id) = 'text' AND length(assessment_id) BETWEEN 1 AND 128
           AND assessment_id GLOB '[A-Za-z0-9]*'
           AND assessment_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(assessment_id, char(0)) = 0),
    CHECK (typeof(record_id) = 'text' AND length(record_id) BETWEEN 1 AND 128
           AND record_id GLOB '[A-Za-z0-9]*'
           AND record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(record_id, char(0)) = 0),
    CHECK (typeof(version) = 'text' AND length(version) BETWEEN 1 AND 128
           AND version GLOB '[A-Za-z0-9]*'
           AND version NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(version, char(0)) = 0),
    CHECK (typeof(target_snapshot_id) = 'text'
           AND length(target_snapshot_id) BETWEEN 1 AND 128
           AND target_snapshot_id GLOB '[A-Za-z0-9]*'
           AND target_snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(target_snapshot_id, char(0)) = 0),
    CHECK (status IN ('matched', 'potentially_stale', 'invalid', 'unknown')),
    CHECK (basis IN ('deterministic', 'review')),
    CHECK (typeof(assessed_at_us) = 'integer' AND assessed_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_context_priorities (
    workspace_id     TEXT    NOT NULL,
    principal_id     TEXT    NOT NULL,
    target_record_id TEXT    NOT NULL,
    target_version   TEXT    NOT NULL,
    priority         TEXT    NOT NULL,
    expires_at_us    INTEGER,
    updated_at_us    INTEGER NOT NULL,
    audit_ref        TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, principal_id, target_record_id, target_version),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(principal_id) = 'text'
           AND length(principal_id) BETWEEN 1 AND 128),
    CHECK (typeof(target_record_id) = 'text' AND length(target_record_id) BETWEEN 1 AND 128
           AND target_record_id GLOB '[A-Za-z0-9]*'
           AND target_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(target_record_id, char(0)) = 0),
    CHECK (typeof(target_version) = 'text' AND length(target_version) BETWEEN 1 AND 128
           AND target_version GLOB '[A-Za-z0-9]*'
           AND target_version NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(target_version, char(0)) = 0),
    CHECK (priority IN ('normal', 'preferred')),
    CHECK (expires_at_us IS NULL
           OR (typeof(expires_at_us) = 'integer' AND expires_at_us > 0)),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_review_attestations (
    workspace_id       TEXT    NOT NULL,
    attestation_id     TEXT    NOT NULL,
    record_id          TEXT    NOT NULL,
    version            TEXT    NOT NULL,
    target_snapshot_id TEXT    NOT NULL,
    outcome            TEXT    NOT NULL,
    review_evidence_id TEXT,
    recorded_at_us     INTEGER NOT NULL,
    audit_ref          TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, attestation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(attestation_id) = 'text' AND length(attestation_id) BETWEEN 1 AND 128
           AND attestation_id GLOB '[A-Za-z0-9]*'
           AND attestation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(attestation_id, char(0)) = 0),
    CHECK (typeof(record_id) = 'text' AND length(record_id) BETWEEN 1 AND 128
           AND record_id GLOB '[A-Za-z0-9]*'
           AND record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(record_id, char(0)) = 0),
    CHECK (typeof(version) = 'text' AND length(version) BETWEEN 1 AND 128
           AND version GLOB '[A-Za-z0-9]*'
           AND version NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(version, char(0)) = 0),
    CHECK (typeof(target_snapshot_id) = 'text'
           AND length(target_snapshot_id) BETWEEN 1 AND 128
           AND target_snapshot_id GLOB '[A-Za-z0-9]*'
           AND target_snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(target_snapshot_id, char(0)) = 0),
    CHECK (outcome IN ('acknowledged', 'evidence_attached', 'revision_proposed')),
    CHECK (review_evidence_id IS NULL
           OR (typeof(review_evidence_id) = 'text'
               AND length(review_evidence_id) BETWEEN 1 AND 128
               AND review_evidence_id GLOB '[A-Za-z0-9]*'
               AND review_evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(review_evidence_id, char(0)) = 0)),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_assessments_target
    ON omnivia_engineering_assessments (workspace_id, record_id, version, target_snapshot_id, assessed_at_us);
CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_priorities_principal
    ON omnivia_engineering_context_priorities (workspace_id, principal_id, updated_at_us);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_dependencies_insert
BEFORE INSERT ON omnivia_engineering_dependencies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_dependencies')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_dependencies_update
BEFORE UPDATE ON omnivia_engineering_dependencies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_dependencies is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_dependencies_delete
BEFORE DELETE ON omnivia_engineering_dependencies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_dependencies is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_assessments_insert
BEFORE INSERT ON omnivia_engineering_assessments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_assessments')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_assessments_update
BEFORE UPDATE ON omnivia_engineering_assessments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_assessments is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_assessments_delete
BEFORE DELETE ON omnivia_engineering_assessments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_assessments is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_context_priorities_insert
BEFORE INSERT ON omnivia_engineering_context_priorities
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_context_priorities')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_context_priorities_update
BEFORE UPDATE ON omnivia_engineering_context_priorities
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_engineering_context_priorities')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a context preference targets one immutable key; restate it rather than retargeting')
    WHERE NEW.target_record_id IS NOT OLD.target_record_id
       OR NEW.target_version IS NOT OLD.target_version
       OR NEW.principal_id IS NOT OLD.principal_id;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_context_priorities_delete
BEFORE DELETE ON omnivia_engineering_context_priorities
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_context_priorities is never deleted; a normal preference supersedes a preferred one');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_review_attestations_insert
BEFORE INSERT ON omnivia_engineering_review_attestations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_review_attestations')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_review_attestations_update
BEFORE UPDATE ON omnivia_engineering_review_attestations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_review_attestations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_review_attestations_delete
BEFORE DELETE ON omnivia_engineering_review_attestations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_review_attestations is append-only; DELETE is never permitted');
END;
