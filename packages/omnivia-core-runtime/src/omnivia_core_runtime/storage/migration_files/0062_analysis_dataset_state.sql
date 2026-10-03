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
-- Coverage and source-observation evidence are closed shapes, not free JSON. Each is
-- a canonical object of at most 8192 bytes with its `sha256:` digest beside it, and
-- these fields are the whole vocabulary:
--
--   coverage            scope_digest, accepted_rows, rejected_rows, conflicting_rows,
--                       deduplicated_rows, expected_source_rows, proof_kind, proof_refs
--   source_observation  source_ref {id, revision_id}, source_incarnation,
--                       observation_interval {start_inclusive_at_us, end_exclusive_at_us},
--                       source_cutoff_at_us, verification_at_us, evidence_kind,
--                       snapshot_token_ref, applied_checkpoint_ref, scope_digest,
--                       evidence_refs
--
-- The INSERT guard enforces each shape exactly: no missing, extra or unsorted key, no
-- value of the wrong type, no integer outside its range, no word outside its
-- vocabulary, no identifier outside the Core Identifier grammar, no list beyond 64
-- unique entries, and no interval that does not run forward. Each document's scope
-- digest, and the source observation's verification instant, must agree with the row
-- they describe. An identifier is an opaque reference, and the grammar is what storage
-- checks. Free text, endpoints, credentials, row data and amounts have no field to sit
-- in, so a payload carrying one is refused for its keys before any value is read.
--
-- The CHECKs police the text form: at most 8192 bytes, no NUL, no escape sequence,
-- minified, no negative number. Canonical evidence never needs an escape, and SQLite
-- before 3.45 decodes `\u0000` by cutting the string short. The sign check exists
-- because `json()` keeps `-0` as written while the reader's canonical form is `0`.
-- The guard checks member order by rebuilding each document with its keys in canonical
-- order and comparing the result with the stored text. SQLite runs a BEFORE trigger ahead
-- of the table's CHECKs, so that comparison is made only on text the CHECKs already admit
-- in form: any other text is refused by its CHECK, under the message it had before. Member
-- position is never read from a JSON table's `id`: SQLite documents it as housekeeping with
-- no order. The reader verifies the digest and the canonical form. A
-- depth walk over both documents, along the parent links of `json_tree`, keeps them within
-- 32 levels as defence in depth; the closed shapes are far shallower than that.
--
-- Every write runs inside the caller's `fenced_transaction`. The INSERT guard carries
-- the complete connection-authority, guard, workspace-state and lease predicate and
-- the workspace binding, then the contiguous-generation rule, the audit binding and
-- the evidence shapes. UPDATE and DELETE are refused unconditionally, for the fenced
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
           AND json(coverage_json) = coverage_json
           AND instr(coverage_json, '":-') = 0
           AND instr(coverage_json, '[-') = 0
           AND instr(coverage_json, ',-') = 0),
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
           AND json(source_observation_json) = source_observation_json
           AND instr(source_observation_json, '":-') = 0
           AND instr(source_observation_json, '[-') = 0
           AND instr(source_observation_json, ',-') = 0),
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
-- successful audit event at exactly its recorded instant, and then the evidence:
-- both documents are JSON objects within the depth ceiling, each holds exactly its
-- closed shape with sorted keys, and the scope digests and verification instant agree
-- with the row. The evidence statements test only non-NULL text, so a missing document
-- is reported by its NOT NULL constraint as before. UPDATE and DELETE carry no
-- predicate: there is no condition under which rewriting or removing an observation
-- is correct.

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

    SELECT RAISE(ABORT, 'omnivia: dataset state evidence must be valid JSON')
    WHERE (typeof(NEW.coverage_json) = 'text' AND json_valid(NEW.coverage_json) = 0)
       OR (typeof(NEW.source_observation_json) = 'text'
           AND json_valid(NEW.source_observation_json) = 0);

    SELECT RAISE(ABORT, 'omnivia: dataset state evidence must be a JSON object')
    WHERE (typeof(NEW.coverage_json) = 'text'
           AND json_type(NEW.coverage_json) IS NOT 'object')
       OR (typeof(NEW.source_observation_json) = 'text'
           AND json_type(NEW.source_observation_json) IS NOT 'object');

    SELECT RAISE(ABORT, 'omnivia: coverage evidence is nested deeper than 32 levels')
    WHERE typeof(NEW.coverage_json) = 'text'
      AND (WITH RECURSIVE coverage_walk(id, depth) AS (
               SELECT t.id, 0 FROM json_tree(NEW.coverage_json) t WHERE t.parent IS NULL
               UNION ALL
               SELECT c.id, w.depth + 1
               FROM coverage_walk w JOIN json_tree(NEW.coverage_json) c ON c.parent = w.id
               WHERE w.depth <= 32
           ) SELECT MAX(depth) FROM coverage_walk) > 32;

    SELECT RAISE(ABORT, 'omnivia: source observation evidence is nested deeper than 32 levels')
    WHERE typeof(NEW.source_observation_json) = 'text'
      AND (WITH RECURSIVE source_walk(id, depth) AS (
               SELECT t.id, 0 FROM json_tree(NEW.source_observation_json) t WHERE t.parent IS NULL
               UNION ALL
               SELECT c.id, w.depth + 1
               FROM source_walk w JOIN json_tree(NEW.source_observation_json) c ON c.parent = w.id
               WHERE w.depth <= 32
           ) SELECT MAX(depth) FROM source_walk) > 32;

    SELECT RAISE(ABORT, 'omnivia: coverage evidence is outside its closed shape')
    WHERE typeof(NEW.coverage_json) = 'text'
      AND ((SELECT COUNT(*) FROM json_each(NEW.coverage_json)) IS NOT 8
           OR (SELECT COUNT(DISTINCT key) FROM json_each(NEW.coverage_json)
               WHERE key IN ('accepted_rows', 'conflicting_rows', 'deduplicated_rows',
                             'expected_source_rows', 'proof_kind', 'proof_refs',
                             'rejected_rows', 'scope_digest')) IS NOT 8
           OR (instr(NEW.coverage_json, char(92)) = 0
               AND instr(NEW.coverage_json, '":-') = 0
               AND instr(NEW.coverage_json, '[-') = 0
               AND instr(NEW.coverage_json, ',-') = 0
               AND json(NEW.coverage_json) = NEW.coverage_json
               AND json_object(
                       'accepted_rows', json_extract(NEW.coverage_json, '$.accepted_rows'),
                       'conflicting_rows', json_extract(NEW.coverage_json, '$.conflicting_rows'),
                       'deduplicated_rows', json_extract(NEW.coverage_json, '$.deduplicated_rows'),
                       'expected_source_rows', json_extract(NEW.coverage_json, '$.expected_source_rows'),
                       'proof_kind', json_extract(NEW.coverage_json, '$.proof_kind'),
                       'proof_refs', json(json_extract(NEW.coverage_json, '$.proof_refs')),
                       'rejected_rows', json_extract(NEW.coverage_json, '$.rejected_rows'),
                       'scope_digest', json_extract(NEW.coverage_json, '$.scope_digest'))
                   IS NOT NEW.coverage_json)
           OR json_type(NEW.coverage_json, '$.scope_digest') IS NOT 'text'
           OR length(json_extract(NEW.coverage_json, '$.scope_digest')) IS NOT 71
           OR substr(json_extract(NEW.coverage_json, '$.scope_digest'), 1, 7) IS NOT 'sha256:'
           OR substr(json_extract(NEW.coverage_json, '$.scope_digest'), 8) GLOB '*[^0-9a-f]*'
           OR instr(json_extract(NEW.coverage_json, '$.scope_digest'), char(0)) > 0
           OR json_type(NEW.coverage_json, '$.accepted_rows') IS NOT 'integer'
           OR typeof(json_extract(NEW.coverage_json, '$.accepted_rows')) IS NOT 'integer'
           OR json_extract(NEW.coverage_json, '$.accepted_rows') < 0
           OR json_type(NEW.coverage_json, '$.rejected_rows') IS NOT 'integer'
           OR typeof(json_extract(NEW.coverage_json, '$.rejected_rows')) IS NOT 'integer'
           OR json_extract(NEW.coverage_json, '$.rejected_rows') < 0
           OR json_type(NEW.coverage_json, '$.conflicting_rows') IS NOT 'integer'
           OR typeof(json_extract(NEW.coverage_json, '$.conflicting_rows')) IS NOT 'integer'
           OR json_extract(NEW.coverage_json, '$.conflicting_rows') < 0
           OR json_type(NEW.coverage_json, '$.deduplicated_rows') IS NOT 'integer'
           OR typeof(json_extract(NEW.coverage_json, '$.deduplicated_rows')) IS NOT 'integer'
           OR json_extract(NEW.coverage_json, '$.deduplicated_rows') < 0
           OR (json_type(NEW.coverage_json, '$.expected_source_rows') IS NOT 'null'
               AND (json_type(NEW.coverage_json, '$.expected_source_rows') IS NOT 'integer'
                    OR typeof(json_extract(NEW.coverage_json, '$.expected_source_rows')) IS NOT 'integer'
                    OR json_extract(NEW.coverage_json, '$.expected_source_rows') < 0))
           OR json_type(NEW.coverage_json, '$.proof_kind') IS NOT 'text'
           OR json_extract(NEW.coverage_json, '$.proof_kind') NOT IN
              ('complete_enumeration', 'consistent_snapshot', 'contiguous_log',
               'bounded_observation', 'none')
           OR json_type(NEW.coverage_json, '$.proof_refs') IS NOT 'array'
           OR (SELECT COUNT(*) FROM json_each(NEW.coverage_json, '$.proof_refs')) > 64
           OR EXISTS (
                SELECT 1 FROM json_each(NEW.coverage_json, '$.proof_refs') r
                WHERE r.type IS NOT 'text'
                   OR length(r.value) NOT BETWEEN 1 AND 128
                   OR r.value NOT GLOB '[A-Za-z0-9]*'
                   OR r.value GLOB '*[^A-Za-z0-9._:-]*'
                   OR instr(r.value, char(0)) > 0)
           OR (SELECT COUNT(DISTINCT r.value) FROM json_each(NEW.coverage_json, '$.proof_refs') r)
              IS NOT (SELECT COUNT(*) FROM json_each(NEW.coverage_json, '$.proof_refs')));

    SELECT RAISE(ABORT, 'omnivia: source observation evidence is outside its closed shape')
    WHERE typeof(NEW.source_observation_json) = 'text'
      AND ((SELECT COUNT(*) FROM json_each(NEW.source_observation_json)) IS NOT 10
           OR (SELECT COUNT(DISTINCT key) FROM json_each(NEW.source_observation_json)
               WHERE key IN ('applied_checkpoint_ref', 'evidence_kind', 'evidence_refs',
                             'observation_interval', 'scope_digest', 'snapshot_token_ref',
                             'source_cutoff_at_us', 'source_incarnation', 'source_ref',
                             'verification_at_us')) IS NOT 10
           OR (instr(NEW.source_observation_json, char(92)) = 0
               AND instr(NEW.source_observation_json, '":-') = 0
               AND instr(NEW.source_observation_json, '[-') = 0
               AND instr(NEW.source_observation_json, ',-') = 0
               AND json(NEW.source_observation_json) = NEW.source_observation_json
               AND json_object(
                       'applied_checkpoint_ref',
                           json_extract(NEW.source_observation_json, '$.applied_checkpoint_ref'),
                       'evidence_kind', json_extract(NEW.source_observation_json, '$.evidence_kind'),
                       'evidence_refs',
                           json(json_extract(NEW.source_observation_json, '$.evidence_refs')),
                       'observation_interval', json(json_object(
                           'end_exclusive_at_us', json_extract(NEW.source_observation_json,
                               '$.observation_interval.end_exclusive_at_us'),
                           'start_inclusive_at_us', json_extract(NEW.source_observation_json,
                               '$.observation_interval.start_inclusive_at_us'))),
                       'scope_digest', json_extract(NEW.source_observation_json, '$.scope_digest'),
                       'snapshot_token_ref',
                           json_extract(NEW.source_observation_json, '$.snapshot_token_ref'),
                       'source_cutoff_at_us',
                           json_extract(NEW.source_observation_json, '$.source_cutoff_at_us'),
                       'source_incarnation',
                           json_extract(NEW.source_observation_json, '$.source_incarnation'),
                       'source_ref', json(json_object(
                           'id', json_extract(NEW.source_observation_json, '$.source_ref.id'),
                           'revision_id',
                               json_extract(NEW.source_observation_json, '$.source_ref.revision_id'))),
                       'verification_at_us',
                           json_extract(NEW.source_observation_json, '$.verification_at_us'))
                   IS NOT NEW.source_observation_json)
           OR json_type(NEW.source_observation_json, '$.source_ref') IS NOT 'object'
           OR (SELECT COUNT(*) FROM json_each(NEW.source_observation_json, '$.source_ref')) IS NOT 2
           OR (SELECT COUNT(DISTINCT key) FROM json_each(NEW.source_observation_json, '$.source_ref')
               WHERE key IN ('id', 'revision_id')) IS NOT 2
           OR json_type(NEW.source_observation_json, '$.source_ref.id') IS NOT 'text'
           OR length(json_extract(NEW.source_observation_json, '$.source_ref.id')) NOT BETWEEN 1 AND 128
           OR json_extract(NEW.source_observation_json, '$.source_ref.id') NOT GLOB '[A-Za-z0-9]*'
           OR json_extract(NEW.source_observation_json, '$.source_ref.id') GLOB '*[^A-Za-z0-9._:-]*'
           OR instr(json_extract(NEW.source_observation_json, '$.source_ref.id'), char(0)) > 0
           OR json_type(NEW.source_observation_json, '$.source_ref.revision_id') IS NOT 'text'
           OR length(json_extract(NEW.source_observation_json, '$.source_ref.revision_id')) NOT BETWEEN 1 AND 128
           OR json_extract(NEW.source_observation_json, '$.source_ref.revision_id') NOT GLOB '[A-Za-z0-9]*'
           OR json_extract(NEW.source_observation_json, '$.source_ref.revision_id') GLOB '*[^A-Za-z0-9._:-]*'
           OR instr(json_extract(NEW.source_observation_json, '$.source_ref.revision_id'), char(0)) > 0
           OR json_type(NEW.source_observation_json, '$.observation_interval') IS NOT 'object'
           OR (SELECT COUNT(*) FROM json_each(NEW.source_observation_json, '$.observation_interval')) IS NOT 2
           OR (SELECT COUNT(DISTINCT key) FROM json_each(NEW.source_observation_json, '$.observation_interval')
               WHERE key IN ('start_inclusive_at_us', 'end_exclusive_at_us')) IS NOT 2
           OR json_type(NEW.source_observation_json, '$.observation_interval.start_inclusive_at_us') IS NOT 'integer'
           OR typeof(json_extract(NEW.source_observation_json, '$.observation_interval.start_inclusive_at_us')) IS NOT 'integer'
           OR json_extract(NEW.source_observation_json, '$.observation_interval.start_inclusive_at_us') < 1
           OR json_type(NEW.source_observation_json, '$.observation_interval.end_exclusive_at_us') IS NOT 'integer'
           OR typeof(json_extract(NEW.source_observation_json, '$.observation_interval.end_exclusive_at_us')) IS NOT 'integer'
           OR json_extract(NEW.source_observation_json, '$.observation_interval.end_exclusive_at_us') < 1
           OR json_extract(NEW.source_observation_json, '$.observation_interval.end_exclusive_at_us')
              <= json_extract(NEW.source_observation_json, '$.observation_interval.start_inclusive_at_us')
           OR (json_type(NEW.source_observation_json, '$.source_incarnation') IS NOT 'null'
               AND (json_type(NEW.source_observation_json, '$.source_incarnation') IS NOT 'text'
                    OR length(json_extract(NEW.source_observation_json, '$.source_incarnation')) NOT BETWEEN 1 AND 128
                    OR json_extract(NEW.source_observation_json, '$.source_incarnation') NOT GLOB '[A-Za-z0-9]*'
                    OR json_extract(NEW.source_observation_json, '$.source_incarnation') GLOB '*[^A-Za-z0-9._:-]*'
                    OR instr(json_extract(NEW.source_observation_json, '$.source_incarnation'), char(0)) > 0))
           OR (json_type(NEW.source_observation_json, '$.source_cutoff_at_us') IS NOT 'null'
               AND (json_type(NEW.source_observation_json, '$.source_cutoff_at_us') IS NOT 'integer'
                    OR typeof(json_extract(NEW.source_observation_json, '$.source_cutoff_at_us')) IS NOT 'integer'
                    OR json_extract(NEW.source_observation_json, '$.source_cutoff_at_us') < 1))
           OR json_type(NEW.source_observation_json, '$.verification_at_us') IS NOT 'integer'
           OR typeof(json_extract(NEW.source_observation_json, '$.verification_at_us')) IS NOT 'integer'
           OR json_extract(NEW.source_observation_json, '$.verification_at_us') < 1
           OR json_type(NEW.source_observation_json, '$.evidence_kind') IS NOT 'text'
           OR json_extract(NEW.source_observation_json, '$.evidence_kind') NOT IN
              ('snapshot', 'stream_caught_up', 'cursor_poll', 'complete_reconcile',
               'captured_query', 'none')
           OR (json_type(NEW.source_observation_json, '$.snapshot_token_ref') IS NOT 'null'
               AND (json_type(NEW.source_observation_json, '$.snapshot_token_ref') IS NOT 'text'
                    OR length(json_extract(NEW.source_observation_json, '$.snapshot_token_ref')) NOT BETWEEN 1 AND 128
                    OR json_extract(NEW.source_observation_json, '$.snapshot_token_ref') NOT GLOB '[A-Za-z0-9]*'
                    OR json_extract(NEW.source_observation_json, '$.snapshot_token_ref') GLOB '*[^A-Za-z0-9._:-]*'
                    OR instr(json_extract(NEW.source_observation_json, '$.snapshot_token_ref'), char(0)) > 0))
           OR (json_type(NEW.source_observation_json, '$.applied_checkpoint_ref') IS NOT 'null'
               AND (json_type(NEW.source_observation_json, '$.applied_checkpoint_ref') IS NOT 'text'
                    OR length(json_extract(NEW.source_observation_json, '$.applied_checkpoint_ref')) NOT BETWEEN 1 AND 128
                    OR json_extract(NEW.source_observation_json, '$.applied_checkpoint_ref') NOT GLOB '[A-Za-z0-9]*'
                    OR json_extract(NEW.source_observation_json, '$.applied_checkpoint_ref') GLOB '*[^A-Za-z0-9._:-]*'
                    OR instr(json_extract(NEW.source_observation_json, '$.applied_checkpoint_ref'), char(0)) > 0))
           OR json_type(NEW.source_observation_json, '$.scope_digest') IS NOT 'text'
           OR length(json_extract(NEW.source_observation_json, '$.scope_digest')) IS NOT 71
           OR substr(json_extract(NEW.source_observation_json, '$.scope_digest'), 1, 7) IS NOT 'sha256:'
           OR substr(json_extract(NEW.source_observation_json, '$.scope_digest'), 8) GLOB '*[^0-9a-f]*'
           OR instr(json_extract(NEW.source_observation_json, '$.scope_digest'), char(0)) > 0
           OR json_type(NEW.source_observation_json, '$.evidence_refs') IS NOT 'array'
           OR (SELECT COUNT(*) FROM json_each(NEW.source_observation_json, '$.evidence_refs')) > 64
           OR EXISTS (
                SELECT 1 FROM json_each(NEW.source_observation_json, '$.evidence_refs') r
                WHERE r.type IS NOT 'text'
                   OR length(r.value) NOT BETWEEN 1 AND 128
                   OR r.value NOT GLOB '[A-Za-z0-9]*'
                   OR r.value GLOB '*[^A-Za-z0-9._:-]*'
                   OR instr(r.value, char(0)) > 0)
           OR (SELECT COUNT(DISTINCT r.value) FROM json_each(NEW.source_observation_json, '$.evidence_refs') r)
              IS NOT (SELECT COUNT(*) FROM json_each(NEW.source_observation_json, '$.evidence_refs')));

    SELECT RAISE(ABORT, 'omnivia: dataset state evidence is not bound to its row')
    WHERE (typeof(NEW.scope_digest) = 'text' AND typeof(NEW.coverage_json) = 'text'
           AND json_extract(NEW.coverage_json, '$.scope_digest') IS NOT NEW.scope_digest)
       OR (typeof(NEW.scope_digest) = 'text' AND typeof(NEW.source_observation_json) = 'text'
           AND json_extract(NEW.source_observation_json, '$.scope_digest') IS NOT NEW.scope_digest)
       OR (typeof(NEW.verified_at_us) = 'integer' AND typeof(NEW.source_observation_json) = 'text'
           AND json_extract(NEW.source_observation_json, '$.verification_at_us') IS NOT NEW.verified_at_us);
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
