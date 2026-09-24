-- Immutable decision definition versions and their qualification evidence
-- (ADR-042, plan PR-3; spec §7.1 and §14.1).
--
-- Additive only. `omnivia_decision_definition_versions` holds one immutable row
-- per published definition version: a semantic change to question, rubric,
-- options or recipe is a new version, and the definition digest is the canonical
-- document's own content hash, so two rows with the same digest are the same
-- definition. `enabled` is the one mutable column -- activation and disablement
-- are versioned settings, not a revision of the definition -- and the UPDATE
-- trigger refuses any other column change, for the fenced writer too.
--
-- `required_sources` is the minimum number of authorised source snapshots the
-- recipe needs; `min_source_count` (the definition's own declared floor) is
-- checked to be at least that at write time, so the admission path can never
-- read two disagreeing answers to "how much evidence is enough".
--
-- `omnivia_decision_qualifications` records qualification grants and their
-- revocation. A grant is immutable; revocation is a one-way transition recorded
-- on the same row, so the activation evidence and its retraction are one
-- lineage rather than two competing facts.

CREATE TABLE IF NOT EXISTS omnivia_decision_definition_versions (
    workspace_id      TEXT    NOT NULL,
    definition_id     TEXT    NOT NULL,
    version           TEXT    NOT NULL,
    title             TEXT    NOT NULL,
    kind              TEXT    NOT NULL,
    purpose           TEXT    NOT NULL,
    options_json      TEXT    NOT NULL,
    recipe_json       TEXT    NOT NULL,
    required_sources  INTEGER NOT NULL,
    min_source_count  INTEGER NOT NULL,
    definition_digest TEXT    NOT NULL,
    definition_json   TEXT    NOT NULL,
    enabled           INTEGER NOT NULL,
    published_by      TEXT    NOT NULL,
    published_at_us   INTEGER NOT NULL,
    audit_ref         TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, definition_id, version),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(definition_id) = 'text' AND length(definition_id) BETWEEN 1 AND 128
           AND definition_id GLOB '[A-Za-z0-9]*'
           AND definition_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(definition_id, char(0)) = 0),
    CHECK (typeof(version) = 'text' AND length(version) BETWEEN 1 AND 128
           AND version GLOB '[A-Za-z0-9]*'
           AND version NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(version, char(0)) = 0),
    CHECK (typeof(title) = 'text' AND length(title) BETWEEN 1 AND 256),
    CHECK (kind IN ('boolean', 'choice', 'ordinal')),
    CHECK (typeof(purpose) = 'text' AND length(purpose) BETWEEN 1 AND 128
           AND purpose GLOB '[a-z]*' AND purpose NOT GLOB '*[^a-z0-9_.]*'
           AND purpose NOT GLOB '*.' AND purpose NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(options_json) = 'text'
           AND length(CAST(options_json AS BLOB)) BETWEEN 2 AND 1048576
           AND json_valid(options_json) = 1 AND json(options_json) = options_json),
    CHECK (typeof(recipe_json) = 'text'
           AND length(CAST(recipe_json AS BLOB)) BETWEEN 2 AND 1048576
           AND json_valid(recipe_json) = 1 AND json(recipe_json) = recipe_json),
    CHECK (typeof(required_sources) = 'integer' AND required_sources >= 0),
    CHECK (typeof(min_source_count) = 'integer' AND min_source_count >= 0
           AND min_source_count >= required_sources),
    CHECK (typeof(definition_digest) = 'text' AND length(definition_digest) = 71
           AND substr(definition_digest, 1, 7) = 'sha256:'
           AND substr(definition_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(definition_json) = 'text'
           AND length(CAST(definition_json AS BLOB)) BETWEEN 2 AND 1048576
           AND json_valid(definition_json) = 1 AND json(definition_json) = definition_json),
    CHECK (enabled IN (0, 1)),
    CHECK (typeof(published_by) = 'text'
           AND length(published_by) BETWEEN 1 AND 128),
    CHECK (typeof(published_at_us) = 'integer' AND published_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0)
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_decision_qualifications (
    workspace_id       TEXT    NOT NULL,
    qualification_id   TEXT    NOT NULL,
    definition_id      TEXT    NOT NULL,
    definition_version TEXT    NOT NULL,
    profile            TEXT    NOT NULL,
    evidence_digest    TEXT    NOT NULL,
    state              TEXT    NOT NULL,
    granted_at_us      INTEGER NOT NULL,
    revoked_at_us      INTEGER,
    audit_ref          TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, qualification_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(qualification_id) = 'text' AND length(qualification_id) BETWEEN 1 AND 128
           AND qualification_id GLOB '[A-Za-z0-9]*'
           AND qualification_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(qualification_id, char(0)) = 0),
    CHECK (typeof(profile) = 'text' AND length(profile) BETWEEN 1 AND 128
           AND profile GLOB '[A-Za-z0-9]*'
           AND profile NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(profile, char(0)) = 0),
    CHECK (typeof(evidence_digest) = 'text' AND length(evidence_digest) = 71
           AND substr(evidence_digest, 1, 7) = 'sha256:'
           AND substr(evidence_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (state IN ('granted', 'revoked')),
    CHECK (typeof(granted_at_us) = 'integer' AND granted_at_us > 0),
    CHECK (revoked_at_us IS NULL
           OR (typeof(revoked_at_us) = 'integer' AND revoked_at_us > 0)),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK (state = 'granted' OR revoked_at_us IS NOT NULL),
    CHECK (revoked_at_us IS NULL OR revoked_at_us >= granted_at_us),

    FOREIGN KEY (workspace_id, definition_id, definition_version)
        REFERENCES omnivia_decision_definition_versions (
            workspace_id, definition_id, version)
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_definition_versions_insert
BEFORE INSERT ON omnivia_decision_definition_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_decision_definition_versions')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_definition_versions_update
BEFORE UPDATE ON omnivia_decision_definition_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_decision_definition_versions')
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
    SELECT RAISE(ABORT, 'omnivia: decision definition versions are immutable; only the enabled flag may change')
    WHERE NEW.title IS NOT OLD.title
       OR NEW.kind IS NOT OLD.kind
       OR NEW.purpose IS NOT OLD.purpose
       OR NEW.options_json IS NOT OLD.options_json
       OR NEW.recipe_json IS NOT OLD.recipe_json
       OR NEW.required_sources IS NOT OLD.required_sources
       OR NEW.min_source_count IS NOT OLD.min_source_count
       OR NEW.definition_digest IS NOT OLD.definition_digest
       OR NEW.definition_json IS NOT OLD.definition_json
       OR NEW.published_by IS NOT OLD.published_by
       OR NEW.published_at_us IS NOT OLD.published_at_us
       OR NEW.audit_ref IS NOT OLD.audit_ref;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_definition_versions_delete
BEFORE DELETE ON omnivia_decision_definition_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_definition_versions is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_qualifications_insert
BEFORE INSERT ON omnivia_decision_qualifications
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_decision_qualifications')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_qualifications_update
BEFORE UPDATE ON omnivia_decision_qualifications
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_decision_qualifications')
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
    SELECT RAISE(ABORT, 'omnivia: decision qualification identity is immutable; only revocation may change')
    WHERE NEW.definition_id IS NOT OLD.definition_id
       OR NEW.definition_version IS NOT OLD.definition_version
       OR NEW.profile IS NOT OLD.profile
       OR NEW.evidence_digest IS NOT OLD.evidence_digest
       OR NEW.granted_at_us IS NOT OLD.granted_at_us
       OR NEW.audit_ref IS NOT OLD.audit_ref
       OR (OLD.state = 'revoked'
           AND (NEW.state IS NOT OLD.state
                OR NEW.revoked_at_us IS NOT OLD.revoked_at_us))
       OR (OLD.state = 'granted' AND NEW.state = 'revoked'
           AND NEW.revoked_at_us IS NULL);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_qualifications_delete
BEFORE DELETE ON omnivia_decision_qualifications
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_qualifications is append-only; DELETE is never permitted');
END;
