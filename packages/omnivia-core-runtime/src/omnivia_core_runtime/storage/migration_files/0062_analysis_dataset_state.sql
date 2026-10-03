-- Structured-data DatasetState observations (SPEC-CORE-DATA-001, PLAN-CORE-DATA-001
-- WP07 PR 1).
--
-- Additive only; allocation 0062 (Structured Data, predecessor 0061). A storage
-- component and nothing more: no producer, operation, handler or worker writes or
-- reads these rows yet, and `analysis.start` remains the milestone-1
-- `dependency_unavailable` refusal. The families:
--
--   omnivia_analysis_dataset_state_observations  one immutable observation of one
--       dataset's state. Every accepted observation is that dataset's next
--       `state_generation` -- one contiguous counter per (workspace, dataset), the
--       DatasetState resource revision -- so the log is append-only and gap-free.
--   omnivia_analysis_dataset_state_current       the current-state projection: the
--       highest generation of each dataset. A view over the log, so it is derived
--       on every read and there is nothing to rebuild, drift or repair.
--
-- The dimensions stay independent. Initial readiness, completeness, continuity,
-- operational health, schema compatibility, evidence availability and content are
-- each one closed vocabulary, and no CHECK couples one to another: `empty` content
-- is not `complete` coverage, and a healthy source with available evidence is not
-- thereby fresh. Consumers combine the dimensions; storage never folds them into a
-- ready or error flag.
--
-- Freshness is evidence, not a verdict. A row carries the scope digest, the source
-- observation, an optional freshness deadline and the instants it was verified and
-- recorded; nothing here turns a clock value into currentness. `recorded_at_us` is
-- the instant of the row's own successful application audit event, so a writer
-- brings no clock of its own. `observed_authority_epoch` is the authority epoch the
-- producer observed: evidence for a later evaluation to compare, never a grant.
--
-- Coverage and source-observation evidence are canonical JSON objects of at most
-- 8192 bytes with their `sha256:` digests beside them. The digests and the canonical
-- form are computed and verified above this layer; the checks here police shape. A
-- document holds no escape sequence -- canonical evidence never needs one, and
-- SQLite before 3.45 decodes `\u0000` by cutting the string short -- and the INSERT
-- guard confines every key and string to a bounded identifier and every number to a
-- signed 64-bit integer. That keeps whitespace, quotes, slashes, `@`, fractions and
-- escapes, and with them prose, SQL, URLs and row dumps, out of evidence. A shape
-- cannot tell a sensitive single token or integer from an innocent one: keeping
-- source values out of evidence stays the producer's obligation until the
-- specification fixes each document's schema.
--
-- Every write runs inside the caller's `fenced_transaction`. The INSERT guard carries
-- the complete connection-authority, guard, workspace-state and lease predicate and
-- the workspace binding, then the contiguous-generation rule, the audit binding and
-- the evidence profile. UPDATE and DELETE are refused unconditionally, for the fenced
-- owner too.
--
-- No DML, and no comment sits inside a statement below, so the migrator's statement
-- splitter and `executescript` store the same schema.

CREATE TABLE IF NOT EXISTS omnivia_analysis_dataset_state_observations (
    workspace_id              TEXT    NOT NULL,
    dataset_id                TEXT    NOT NULL,
    state_generation          INTEGER NOT NULL,
    dataset_revision          TEXT    NOT NULL,
    dataset_incarnation       TEXT    NOT NULL,
    manifest_id               TEXT,
    manifest_revision         TEXT,
    manifest_digest           TEXT,
    initial_readiness         TEXT    NOT NULL,
    completeness              TEXT    NOT NULL,
    continuity                TEXT    NOT NULL,
    operational_health        TEXT    NOT NULL,
    schema_compatibility      TEXT    NOT NULL,
    content_observation       TEXT    NOT NULL,
    evidence_availability     TEXT    NOT NULL,
    observed_authority_epoch  TEXT    NOT NULL,
    scope_digest              TEXT    NOT NULL,
    coverage_json             TEXT    NOT NULL,
    coverage_digest           TEXT    NOT NULL,
    source_observation_json   TEXT    NOT NULL,
    source_observation_digest TEXT    NOT NULL,
    freshness_deadline_at_us  INTEGER,
    verified_at_us            INTEGER NOT NULL,
    recorded_at_us            INTEGER NOT NULL,
    audit_ref                 TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, dataset_id, state_generation),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(dataset_id) = 'text' AND length(dataset_id) BETWEEN 1 AND 128
           AND dataset_id GLOB '[A-Za-z0-9]*'
           AND dataset_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(dataset_id, char(0)) = 0),
    CHECK (typeof(state_generation) = 'integer' AND state_generation > 0),
    CHECK (typeof(dataset_revision) = 'text' AND length(dataset_revision) BETWEEN 1 AND 128
           AND dataset_revision GLOB '[A-Za-z0-9]*'
           AND dataset_revision NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(dataset_revision, char(0)) = 0),
    CHECK (typeof(dataset_incarnation) = 'text'
           AND length(dataset_incarnation) BETWEEN 1 AND 128
           AND dataset_incarnation GLOB '[A-Za-z0-9]*'
           AND dataset_incarnation NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(dataset_incarnation, char(0)) = 0),
    CHECK ((manifest_id IS NULL) = (manifest_revision IS NULL)
           AND (manifest_id IS NULL) = (manifest_digest IS NULL)),
    CHECK (manifest_id IS NULL
           OR (typeof(manifest_id) = 'text' AND length(manifest_id) BETWEEN 1 AND 128
               AND manifest_id GLOB '[A-Za-z0-9]*'
               AND manifest_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(manifest_id, char(0)) = 0)),
    CHECK (manifest_revision IS NULL
           OR (typeof(manifest_revision) = 'text'
               AND length(manifest_revision) BETWEEN 1 AND 128
               AND manifest_revision GLOB '[A-Za-z0-9]*'
               AND manifest_revision NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(manifest_revision, char(0)) = 0)),
    CHECK (manifest_digest IS NULL
           OR (typeof(manifest_digest) = 'text' AND length(manifest_digest) = 71
               AND substr(manifest_digest, 1, 7) = 'sha256:'
               AND substr(manifest_digest, 8) NOT GLOB '*[^0-9a-f]*'
               AND instr(manifest_digest, char(0)) = 0)),
    CHECK (initial_readiness IN
           ('not_started', 'initialising', 'catching_up', 'ready', 'blocked')),
    CHECK (completeness IN ('complete', 'partial', 'unknown')),
    CHECK (continuity IN ('verified', 'gap_detected', 'unknown', 'not_applicable')),
    CHECK (operational_health IN
           ('healthy', 'degraded', 'unavailable', 'error', 'unknown')),
    CHECK (schema_compatibility IN
           ('compatible', 'requires_review', 'incompatible', 'unknown')),
    CHECK (content_observation IN ('empty', 'nonempty', 'unknown')),
    CHECK (evidence_availability IN ('available', 'limited', 'unavailable')),
    CHECK (typeof(observed_authority_epoch) = 'text'
           AND length(observed_authority_epoch) BETWEEN 1 AND 128
           AND observed_authority_epoch GLOB '[A-Za-z0-9]*'
           AND observed_authority_epoch NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(observed_authority_epoch, char(0)) = 0),
    CHECK (typeof(scope_digest) = 'text' AND length(scope_digest) = 71
           AND substr(scope_digest, 1, 7) = 'sha256:'
           AND substr(scope_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(scope_digest, char(0)) = 0),
    CHECK (typeof(coverage_json) = 'text'
           AND length(CAST(coverage_json AS BLOB)) BETWEEN 2 AND 8192
           AND instr(coverage_json, char(0)) = 0
           AND instr(coverage_json, char(92)) = 0
           AND json_valid(coverage_json) = 1
           AND json_type(coverage_json) = 'object'
           AND json(coverage_json) = coverage_json),
    CHECK (typeof(coverage_digest) = 'text' AND length(coverage_digest) = 71
           AND substr(coverage_digest, 1, 7) = 'sha256:'
           AND substr(coverage_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(coverage_digest, char(0)) = 0),
    CHECK (typeof(source_observation_json) = 'text'
           AND length(CAST(source_observation_json AS BLOB)) BETWEEN 2 AND 8192
           AND instr(source_observation_json, char(0)) = 0
           AND instr(source_observation_json, char(92)) = 0
           AND json_valid(source_observation_json) = 1
           AND json_type(source_observation_json) = 'object'
           AND json(source_observation_json) = source_observation_json),
    CHECK (typeof(source_observation_digest) = 'text'
           AND length(source_observation_digest) = 71
           AND substr(source_observation_digest, 1, 7) = 'sha256:'
           AND substr(source_observation_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(source_observation_digest, char(0)) = 0),
    CHECK (freshness_deadline_at_us IS NULL
           OR (typeof(freshness_deadline_at_us) = 'integer'
               AND freshness_deadline_at_us > 0)),
    CHECK (typeof(verified_at_us) = 'integer' AND verified_at_us > 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- The current state of every dataset: its observation at the highest generation.
-- The correlated maximum is one seek on the log's own primary key.
CREATE VIEW IF NOT EXISTS omnivia_analysis_dataset_state_current AS
SELECT
    o.workspace_id,
    o.dataset_id,
    o.state_generation,
    o.dataset_revision,
    o.dataset_incarnation,
    o.manifest_id,
    o.manifest_revision,
    o.manifest_digest,
    o.initial_readiness,
    o.completeness,
    o.continuity,
    o.operational_health,
    o.schema_compatibility,
    o.content_observation,
    o.evidence_availability,
    o.observed_authority_epoch,
    o.scope_digest,
    o.coverage_json,
    o.coverage_digest,
    o.source_observation_json,
    o.source_observation_digest,
    o.freshness_deadline_at_us,
    o.verified_at_us,
    o.recorded_at_us,
    o.audit_ref
FROM omnivia_analysis_dataset_state_observations o
WHERE o.state_generation = (
    SELECT MAX(h.state_generation)
    FROM omnivia_analysis_dataset_state_observations h
    WHERE h.workspace_id = o.workspace_id
      AND h.dataset_id = o.dataset_id
);

-- Three statement triggers, one per statement class. INSERT checks authority first,
-- then that the row is its dataset's next generation, that it names its own
-- successful audit event at exactly its recorded instant, and that both evidence
-- documents hold only identifiers, integers, booleans and nulls. UPDATE and DELETE
-- carry no predicate: there is no condition under which rewriting or removing an
-- observation is correct.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_analysis_dataset_state_observations_insert
BEFORE INSERT ON omnivia_analysis_dataset_state_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_analysis_dataset_state_observations')
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

    SELECT RAISE(ABORT, 'omnivia: a dataset state generation must advance by exactly one')
    WHERE NEW.state_generation IS NOT (
        SELECT COALESCE(MAX(state_generation), 0) + 1
        FROM omnivia_analysis_dataset_state_observations
        WHERE workspace_id = NEW.workspace_id AND dataset_id = NEW.dataset_id);

    SELECT RAISE(ABORT, 'omnivia: a dataset state observation requires its exact successful audit')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events a
        WHERE a.workspace_id = NEW.workspace_id
          AND a.audit_ref = NEW.audit_ref
          AND a.recorded_at_us = NEW.recorded_at_us
          AND a.outcome_class = 'succeeded');

    SELECT RAISE(ABORT, 'omnivia: dataset state evidence holds only identifiers, integers, booleans and nulls')
    WHERE EXISTS (
        SELECT 1
        FROM (SELECT "key", "type", atom FROM json_tree(NEW.coverage_json)
              UNION ALL
              SELECT "key", "type", atom FROM json_tree(NEW.source_observation_json)) e
        WHERE e."type" = 'real'
           OR (e."type" = 'integer' AND typeof(e.atom) <> 'integer')
           OR (typeof(e."key") = 'text'
               AND (length(e."key") NOT BETWEEN 1 AND 128
                    OR e."key" NOT GLOB '[A-Za-z0-9]*'
                    OR e."key" GLOB '*[^A-Za-z0-9._:-]*'
                    OR instr(e."key", char(0)) > 0))
           OR (e."type" = 'text'
               AND (length(e.atom) NOT BETWEEN 1 AND 128
                    OR e.atom NOT GLOB '[A-Za-z0-9]*'
                    OR e.atom GLOB '*[^A-Za-z0-9._:-]*'
                    OR instr(e.atom, char(0)) > 0)));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_analysis_dataset_state_observations_update
BEFORE UPDATE ON omnivia_analysis_dataset_state_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_analysis_dataset_state_observations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_analysis_dataset_state_observations_delete
BEFORE DELETE ON omnivia_analysis_dataset_state_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_analysis_dataset_state_observations is append-only; DELETE is never permitted');
END;
