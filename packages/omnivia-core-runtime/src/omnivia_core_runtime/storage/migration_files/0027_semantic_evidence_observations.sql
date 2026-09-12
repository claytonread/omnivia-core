-- Phase 2 Semantic Registry: evidence, observations, governed assertions and
-- deterministic candidates (WP-SEM-06).
--
-- Content bytes remain in the protected blob substrate.  These tables retain
-- only immutable content references, canonical metadata and digests.  Every
-- row is workspace scoped, append-only and protected by the Generation-1
-- writer fence. Corrections are successor facts, never UPDATEs.

CREATE TABLE IF NOT EXISTS omnivia_semantic_evidence_sources (
    workspace_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    locator_scheme TEXT NOT NULL,
    locator TEXT NOT NULL,
    source_version TEXT NOT NULL,
    classification TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, source_id),
    CHECK (length(workspace_id) BETWEEN 1 AND 128),
    CHECK (length(source_id) BETWEEN 1 AND 128),
    CHECK (source_kind IN ('manual','document','record','event')),
    CHECK (locator_scheme IN ('urn','file','https','opaque')),
    CHECK (length(locator) BETWEEN 1 AND 4096),
    CHECK (length(source_version) BETWEEN 1 AND 256),
    CHECK (classification IN ('public','internal','confidential','restricted')),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_evidence_items (
    workspace_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    integrity_digest TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    classification TEXT NOT NULL,
    retention_class TEXT NOT NULL,
    captured_at_us INTEGER NOT NULL,
    captured_at_precision TEXT NOT NULL,
    captured_at_provenance TEXT NOT NULL,
    source_time_us INTEGER,
    source_time_precision TEXT,
    source_time_provenance TEXT,
    schema_version TEXT NOT NULL,
    record_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, evidence_id),
    UNIQUE (workspace_id, content_digest),
    CHECK (length(content_ref) BETWEEN 1 AND 4096),
    CHECK (length(content_digest) = 71 AND substr(content_digest,1,7) = 'sha256:'
           AND substr(content_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(integrity_digest) = 71 AND substr(integrity_digest,1,7) = 'sha256:'
           AND substr(integrity_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(record_digest) = 71 AND substr(record_digest,1,7) = 'sha256:'
           AND substr(record_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(mime_type) BETWEEN 1 AND 255),
    CHECK (classification IN ('public','internal','confidential','restricted')),
    CHECK (length(retention_class) BETWEEN 1 AND 128),
    CHECK (typeof(captured_at_us) = 'integer' AND captured_at_us > 0),
    CHECK (captured_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (captured_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK ((source_time_us IS NULL AND source_time_precision IS NULL AND source_time_provenance IS NULL)
        OR (typeof(source_time_us) = 'integer' AND source_time_us > 0
            AND source_time_precision IN ('year','month','day','hour','minute','second')
            AND source_time_provenance IN ('stated','evidence_attested','ingestion_fallback'))),
    CHECK (length(schema_version) BETWEEN 1 AND 32),
    FOREIGN KEY (workspace_id, source_id)
        REFERENCES omnivia_semantic_evidence_sources (workspace_id, source_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_evidence_spans (
    workspace_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    page_number INTEGER,
    section_ref TEXT,
    span_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, evidence_id, span_id),
    CHECK (typeof(start_offset) = 'integer' AND start_offset >= 0),
    CHECK (typeof(end_offset) = 'integer' AND end_offset > start_offset),
    CHECK (page_number IS NULL OR (typeof(page_number) = 'integer' AND page_number > 0)),
    CHECK (section_ref IS NULL OR length(section_ref) BETWEEN 1 AND 512),
    CHECK (length(span_digest) = 71 AND substr(span_digest,1,7) = 'sha256:'
           AND substr(span_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, evidence_id)
        REFERENCES omnivia_semantic_evidence_items (workspace_id, evidence_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_evidence_extractions (
    workspace_id TEXT NOT NULL,
    extraction_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    worker_version TEXT NOT NULL,
    model_version TEXT,
    template_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    output_digest TEXT NOT NULL,
    raw_completion_ref TEXT,
    confidence_ppm INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    extraction_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, extraction_id),
    CHECK (length(input_digest) = 71 AND substr(input_digest,1,7) = 'sha256:'
           AND substr(input_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(output_digest) = 71 AND substr(output_digest,1,7) = 'sha256:'
           AND substr(output_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(confidence_ppm) = 'integer' AND confidence_ppm BETWEEN 0 AND 1000000),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (length(extraction_digest) = 71 AND substr(extraction_digest,1,7) = 'sha256:'
           AND substr(extraction_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, evidence_id)
        REFERENCES omnivia_semantic_evidence_items (workspace_id, evidence_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_observations (
    workspace_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    observation_kind TEXT NOT NULL,
    value_kind TEXT NOT NULL,
    original_form_ref TEXT NOT NULL,
    normalized_form TEXT NOT NULL,
    proposed_semantic_role TEXT NOT NULL,
    classification TEXT NOT NULL,
    generation TEXT NOT NULL,
    status TEXT NOT NULL,
    source_time_us INTEGER,
    source_time_precision TEXT,
    source_time_provenance TEXT,
    recorded_at_us INTEGER NOT NULL,
    recorded_at_precision TEXT NOT NULL,
    recorded_at_provenance TEXT NOT NULL,
    supersedes_observation_id TEXT,
    rule_version TEXT,
    normalization_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    observation_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, observation_id),
    CHECK (value_kind IN ('text','identifier','relationship','constraint')),
    CHECK (classification IN ('public','internal','confidential','restricted')),
    CHECK (generation IN ('manual','deterministic_rule')),
    CHECK (status IN ('recorded','superseded','retracted')),
    CHECK ((generation = 'manual' AND rule_version IS NULL)
        OR (generation = 'deterministic_rule' AND rule_version IS NOT NULL
            AND length(rule_version) > 0)),
    CHECK ((source_time_us IS NULL AND source_time_precision IS NULL AND source_time_provenance IS NULL)
        OR (typeof(source_time_us) = 'integer' AND source_time_us > 0
            AND source_time_precision IN ('year','month','day','hour','minute','second')
            AND source_time_provenance IN ('stated','evidence_attested','ingestion_fallback'))),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (recorded_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (recorded_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (length(observation_digest) = 71 AND substr(observation_digest,1,7) = 'sha256:'
           AND substr(observation_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, supersedes_observation_id)
        REFERENCES omnivia_semantic_observations (workspace_id, observation_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_observation_evidence (
    workspace_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    span_id TEXT,
    support_role TEXT NOT NULL,
    confidence_ppm INTEGER NOT NULL,
    link_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, observation_id, evidence_id, support_role),
    CHECK (support_role IN ('support','contradict')),
    CHECK (typeof(confidence_ppm) = 'integer' AND confidence_ppm BETWEEN 0 AND 1000000),
    CHECK (length(link_digest) = 71 AND substr(link_digest,1,7) = 'sha256:'
           AND substr(link_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, observation_id)
        REFERENCES omnivia_semantic_observations (workspace_id, observation_id),
    FOREIGN KEY (workspace_id, evidence_id)
        REFERENCES omnivia_semantic_evidence_items (workspace_id, evidence_id),
    FOREIGN KEY (workspace_id, evidence_id, span_id)
        REFERENCES omnivia_semantic_evidence_spans (workspace_id, evidence_id, span_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_observation_features (
    workspace_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    feature_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, observation_id, feature_name, policy_version, calculation_version),
    CHECK (json_valid(feature_json) = 1 AND json(feature_json) = feature_json),
    CHECK (length(feature_digest) = 71 AND substr(feature_digest,1,7) = 'sha256:'
           AND substr(feature_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, observation_id)
        REFERENCES omnivia_semantic_observations (workspace_id, observation_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_assertions (
    workspace_id TEXT NOT NULL,
    assertion_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    predicate_element_id TEXT NOT NULL,
    model_version_id TEXT NOT NULL,
    object_kind TEXT NOT NULL,
    object_id TEXT,
    literal_json TEXT,
    confidence_ppm INTEGER NOT NULL,
    classification TEXT NOT NULL,
    valid_from_us INTEGER,
    valid_from_precision TEXT,
    valid_from_provenance TEXT,
    valid_to_state TEXT NOT NULL,
    valid_to_us INTEGER,
    valid_to_precision TEXT,
    valid_to_provenance TEXT,
    attested_from_us INTEGER NOT NULL,
    attested_from_precision TEXT NOT NULL,
    attested_from_provenance TEXT NOT NULL,
    attested_to_us INTEGER,
    attested_to_precision TEXT,
    attested_to_provenance TEXT,
    recorded_at_us INTEGER NOT NULL,
    recorded_at_precision TEXT NOT NULL,
    recorded_at_provenance TEXT NOT NULL,
    recorded_until_us INTEGER,
    recorded_until_precision TEXT,
    recorded_until_provenance TEXT,
    schema_version TEXT NOT NULL,
    assertion_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, assertion_id),
    CHECK (object_kind IN ('entity','literal')),
    CHECK ((object_kind = 'entity' AND object_id IS NOT NULL AND literal_json IS NULL)
        OR (object_kind = 'literal' AND object_id IS NULL AND literal_json IS NOT NULL
            AND json_valid(literal_json) = 1 AND json(literal_json) = literal_json)),
    CHECK (typeof(confidence_ppm) = 'integer' AND confidence_ppm BETWEEN 0 AND 1000000),
    CHECK (classification IN ('public','internal','confidential','restricted')),
    CHECK ((valid_from_us IS NULL AND valid_from_precision IS NULL AND valid_from_provenance IS NULL)
        OR (typeof(valid_from_us) = 'integer' AND valid_from_us > 0
            AND valid_from_precision IN ('year','month','day','hour','minute','second')
            AND valid_from_provenance IN ('stated','evidence_attested','ingestion_fallback'))),
    CHECK (valid_to_state IN ('stated','unknown','open')),
    CHECK ((valid_to_state = 'stated' AND valid_to_us IS NOT NULL
            AND valid_to_precision IN ('year','month','day','hour','minute','second')
            AND valid_to_provenance IN ('stated','evidence_attested','ingestion_fallback')
            AND attested_to_us IS NULL)
        OR (valid_to_state = 'unknown' AND valid_to_us IS NULL
            AND valid_to_precision IS NULL AND valid_to_provenance IS NULL
            AND attested_to_us IS NOT NULL)
        OR (valid_to_state = 'open' AND valid_to_us IS NULL
            AND valid_to_precision IS NULL AND valid_to_provenance IS NULL
            AND attested_to_us IS NULL)),
    CHECK (typeof(attested_from_us) = 'integer' AND attested_from_us > 0),
    CHECK (attested_from_precision IN ('year','month','day','hour','minute','second')),
    CHECK (attested_from_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK ((attested_to_us IS NULL AND attested_to_precision IS NULL
            AND attested_to_provenance IS NULL)
        OR (attested_to_us IS NOT NULL
            AND attested_to_precision IN ('year','month','day','hour','minute','second')
            AND attested_to_provenance IN ('stated','evidence_attested','ingestion_fallback'))),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (recorded_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (recorded_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (recorded_until_us IS NULL OR recorded_until_us > recorded_at_us),
    CHECK ((recorded_until_us IS NULL AND recorded_until_precision IS NULL
            AND recorded_until_provenance IS NULL)
        OR (recorded_until_us IS NOT NULL
            AND recorded_until_precision IN ('year','month','day','hour','minute','second')
            AND recorded_until_provenance IN ('stated','evidence_attested','ingestion_fallback'))),
    CHECK (length(assertion_digest) = 71 AND substr(assertion_digest,1,7) = 'sha256:'
           AND substr(assertion_digest,8) NOT GLOB '*[^0-9a-f]*')
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_assertion_evidence (
    workspace_id TEXT NOT NULL,
    assertion_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    span_id TEXT,
    support_role TEXT NOT NULL,
    confidence_ppm INTEGER NOT NULL,
    evidence_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, assertion_id, evidence_id, support_role),
    CHECK (support_role IN ('support','contradict')),
    CHECK (typeof(confidence_ppm) = 'integer' AND confidence_ppm BETWEEN 0 AND 1000000),
    CHECK (length(evidence_digest) = 71 AND substr(evidence_digest,1,7) = 'sha256:'
           AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, assertion_id)
        REFERENCES omnivia_semantic_assertions (workspace_id, assertion_id),
    FOREIGN KEY (workspace_id, evidence_id)
        REFERENCES omnivia_semantic_evidence_items (workspace_id, evidence_id),
    FOREIGN KEY (workspace_id, evidence_id, span_id)
        REFERENCES omnivia_semantic_evidence_spans (workspace_id, evidence_id, span_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_assertion_supersessions (
    workspace_id TEXT NOT NULL,
    supersession_id TEXT NOT NULL,
    prior_assertion_id TEXT NOT NULL,
    successor_assertion_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    recorded_at_us INTEGER NOT NULL,
    recorded_at_precision TEXT NOT NULL,
    recorded_at_provenance TEXT NOT NULL,
    supersession_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, supersession_id),
    UNIQUE (workspace_id, prior_assertion_id),
    UNIQUE (workspace_id, successor_assertion_id),
    CHECK (prior_assertion_id <> successor_assertion_id),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (recorded_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (recorded_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (length(supersession_digest) = 71 AND substr(supersession_digest,1,7) = 'sha256:'
           AND substr(supersession_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, prior_assertion_id)
        REFERENCES omnivia_semantic_assertions (workspace_id, assertion_id),
    FOREIGN KEY (workspace_id, successor_assertion_id)
        REFERENCES omnivia_semantic_assertions (workspace_id, assertion_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_assertion_retractions (
    workspace_id TEXT NOT NULL,
    retraction_id TEXT NOT NULL,
    assertion_id TEXT NOT NULL,
    retracted_at_us INTEGER NOT NULL,
    retracted_at_precision TEXT NOT NULL,
    retracted_at_provenance TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    actor_principal_id TEXT NOT NULL,
    retraction_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, retraction_id),
    UNIQUE (workspace_id, assertion_id),
    CHECK (typeof(retracted_at_us) = 'integer' AND retracted_at_us > 0),
    CHECK (retracted_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (retracted_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (length(retraction_digest) = 71 AND substr(retraction_digest,1,7) = 'sha256:'
           AND substr(retraction_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, assertion_id)
        REFERENCES omnivia_semantic_assertions (workspace_id, assertion_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_candidates (
    workspace_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_kind TEXT NOT NULL,
    target_model_id TEXT NOT NULL,
    proposed_operation_json TEXT NOT NULL,
    support_band TEXT NOT NULL,
    novelty_band TEXT NOT NULL,
    risk_band TEXT NOT NULL,
    candidate_state TEXT NOT NULL,
    aggregation_version TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    base_version_id TEXT NOT NULL,
    evidence_snapshot_digest TEXT NOT NULL,
    equivalence_signature TEXT NOT NULL,
    rejection_signature TEXT,
    schema_version TEXT NOT NULL,
    candidate_digest TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    created_at_precision TEXT NOT NULL,
    created_at_provenance TEXT NOT NULL,
    PRIMARY KEY (workspace_id, candidate_id),
    CHECK (json_valid(proposed_operation_json) = 1
           AND json(proposed_operation_json) = proposed_operation_json),
    CHECK (support_band IN ('low','medium','high')),
    CHECK (novelty_band IN ('low','medium','high')),
    CHECK (risk_band IN ('low','standard','high','critical')),
    CHECK (candidate_state IN ('draft','active','proposed','rejected','suppressed','reconsidered')),
    CHECK ((candidate_state IN ('rejected','suppressed') AND rejection_signature IS NOT NULL)
        OR (candidate_state NOT IN ('rejected','suppressed') AND rejection_signature IS NULL)),
    CHECK (length(evidence_snapshot_digest) = 71
           AND substr(evidence_snapshot_digest,1,7) = 'sha256:'
           AND substr(evidence_snapshot_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(equivalence_signature) = 71
           AND substr(equivalence_signature,1,7) = 'sha256:'
           AND substr(equivalence_signature,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(candidate_digest) = 71 AND substr(candidate_digest,1,7) = 'sha256:'
           AND substr(candidate_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (created_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (created_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    FOREIGN KEY (workspace_id, target_model_id)
        REFERENCES omnivia_semantic_models (workspace_id, model_id),
    FOREIGN KEY (workspace_id, target_model_id, base_version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_candidate_contributions (
    workspace_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    contribution_role TEXT NOT NULL,
    weight INTEGER NOT NULL,
    observation_digest TEXT NOT NULL,
    contribution_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, candidate_id, observation_id),
    CHECK (contribution_role IN ('support','contradict','novelty')),
    CHECK (typeof(weight) = 'integer' AND weight BETWEEN 0 AND 10000),
    CHECK (length(observation_digest) = 71 AND substr(observation_digest,1,7) = 'sha256:'
           AND substr(observation_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(contribution_digest) = 71 AND substr(contribution_digest,1,7) = 'sha256:'
           AND substr(contribution_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, candidate_id)
        REFERENCES omnivia_semantic_candidates (workspace_id, candidate_id),
    FOREIGN KEY (workspace_id, observation_id)
        REFERENCES omnivia_semantic_observations (workspace_id, observation_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_candidate_suppressions (
    workspace_id TEXT NOT NULL,
    suppression_id TEXT NOT NULL,
    equivalence_signature TEXT NOT NULL,
    rejection_ref TEXT NOT NULL,
    suppression_rule_version TEXT NOT NULL,
    evidence_snapshot_digest TEXT NOT NULL,
    aggregation_version TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    created_at_precision TEXT NOT NULL,
    created_at_provenance TEXT NOT NULL,
    expires_at_us INTEGER,
    expires_at_precision TEXT,
    expires_at_provenance TEXT,
    suppression_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, suppression_id),
    UNIQUE (workspace_id, equivalence_signature, suppression_rule_version),
    CHECK (length(equivalence_signature) = 71
           AND substr(equivalence_signature,1,7) = 'sha256:'
           AND substr(equivalence_signature,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(evidence_snapshot_digest) = 71
           AND substr(evidence_snapshot_digest,1,7) = 'sha256:'
           AND substr(evidence_snapshot_digest,8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (created_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (created_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (expires_at_us IS NULL OR expires_at_us > created_at_us),
    CHECK ((expires_at_us IS NULL AND expires_at_precision IS NULL
            AND expires_at_provenance IS NULL)
        OR (expires_at_us IS NOT NULL
            AND expires_at_precision IN ('year','month','day','hour','minute','second')
            AND expires_at_provenance IN ('stated','evidence_attested','ingestion_fallback'))),
    CHECK (length(suppression_digest) = 71 AND substr(suppression_digest,1,7) = 'sha256:'
           AND substr(suppression_digest,8) NOT GLOB '*[^0-9a-f]*')
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_candidate_reconsiderations (
    workspace_id TEXT NOT NULL,
    reconsideration_id TEXT NOT NULL,
    suppression_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    previous_evidence_digest TEXT,
    new_evidence_digest TEXT,
    previous_rule_version TEXT,
    new_rule_version TEXT,
    actor_principal_id TEXT,
    recorded_at_us INTEGER NOT NULL,
    recorded_at_precision TEXT NOT NULL,
    recorded_at_provenance TEXT NOT NULL,
    reconsideration_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, reconsideration_id),
    CHECK (reason IN ('new_evidence','rule_version_changed','expired','human_override')),
    CHECK ((reason = 'new_evidence' AND previous_evidence_digest IS NOT NULL
            AND new_evidence_digest IS NOT NULL
            AND previous_evidence_digest <> new_evidence_digest
            AND previous_rule_version IS NULL AND new_rule_version IS NULL
            AND actor_principal_id IS NULL)
        OR (reason = 'rule_version_changed' AND previous_rule_version IS NOT NULL
            AND new_rule_version IS NOT NULL AND previous_rule_version <> new_rule_version
            AND previous_evidence_digest IS NULL AND new_evidence_digest IS NULL
            AND actor_principal_id IS NULL)
        OR (reason = 'expired' AND previous_evidence_digest IS NULL
            AND new_evidence_digest IS NULL AND previous_rule_version IS NULL
            AND new_rule_version IS NULL AND actor_principal_id IS NULL)
        OR (reason = 'human_override' AND actor_principal_id IS NOT NULL
            AND previous_evidence_digest IS NULL AND new_evidence_digest IS NULL
            AND previous_rule_version IS NULL AND new_rule_version IS NULL)),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (recorded_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (recorded_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (length(reconsideration_digest) = 71
           AND substr(reconsideration_digest,1,7) = 'sha256:'
           AND substr(reconsideration_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, suppression_id)
        REFERENCES omnivia_semantic_candidate_suppressions (workspace_id, suppression_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_semantic_evidence_items_source_idx
    ON omnivia_semantic_evidence_items (workspace_id, source_id, captured_at_us);
CREATE INDEX IF NOT EXISTS omnivia_semantic_observations_time_idx
    ON omnivia_semantic_observations (workspace_id, source_time_us, recorded_at_us);
CREATE INDEX IF NOT EXISTS omnivia_semantic_assertions_valid_idx
    ON omnivia_semantic_assertions (workspace_id, subject_id, predicate_element_id,
                                    valid_from_us, valid_to_us, attested_to_us);
CREATE INDEX IF NOT EXISTS omnivia_semantic_assertions_recorded_idx
    ON omnivia_semantic_assertions (workspace_id, recorded_at_us, recorded_until_us);
CREATE INDEX IF NOT EXISTS omnivia_semantic_candidates_state_idx
    ON omnivia_semantic_candidates (workspace_id, candidate_state, created_at_us);
CREATE INDEX IF NOT EXISTS omnivia_semantic_candidates_equivalence_idx
    ON omnivia_semantic_candidates (workspace_id, equivalence_signature);
CREATE INDEX IF NOT EXISTS omnivia_semantic_candidate_contributions_observation_idx
    ON omnivia_semantic_candidate_contributions (workspace_id, observation_id);

-- The fence is repeated per table deliberately. SQLite has no statement-level
-- policy object, so omitting one trigger would create an unfenced write path.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_sources_insert
BEFORE INSERT ON omnivia_semantic_evidence_sources BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_evidence_sources')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_sources_update BEFORE UPDATE ON omnivia_semantic_evidence_sources BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_sources is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_sources_delete BEFORE DELETE ON omnivia_semantic_evidence_sources BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_sources is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_items_insert
BEFORE INSERT ON omnivia_semantic_evidence_items BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_evidence_items')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_items_update BEFORE UPDATE ON omnivia_semantic_evidence_items BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_items is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_items_delete BEFORE DELETE ON omnivia_semantic_evidence_items BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_items is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_spans_insert
BEFORE INSERT ON omnivia_semantic_evidence_spans BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_evidence_spans')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_spans_update BEFORE UPDATE ON omnivia_semantic_evidence_spans BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_spans is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_spans_delete BEFORE DELETE ON omnivia_semantic_evidence_spans BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_spans is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_extractions_insert
BEFORE INSERT ON omnivia_semantic_evidence_extractions BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_evidence_extractions')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_extractions_update BEFORE UPDATE ON omnivia_semantic_evidence_extractions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_extractions is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_extractions_delete BEFORE DELETE ON omnivia_semantic_evidence_extractions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_extractions is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_observations_insert
BEFORE INSERT ON omnivia_semantic_observations BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_observations')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_observations_update BEFORE UPDATE ON omnivia_semantic_observations BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_observations is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_observations_delete BEFORE DELETE ON omnivia_semantic_observations BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_observations is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_observation_evidence_insert
BEFORE INSERT ON omnivia_semantic_observation_evidence BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_observation_evidence')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_observation_evidence_update BEFORE UPDATE ON omnivia_semantic_observation_evidence BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_observation_evidence is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_observation_evidence_delete BEFORE DELETE ON omnivia_semantic_observation_evidence BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_observation_evidence is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_observation_features_insert
BEFORE INSERT ON omnivia_semantic_observation_features BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_observation_features')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_observation_features_update BEFORE UPDATE ON omnivia_semantic_observation_features BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_observation_features is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_observation_features_delete BEFORE DELETE ON omnivia_semantic_observation_features BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_observation_features is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertions_insert
BEFORE INSERT ON omnivia_semantic_assertions BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_assertions')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertions_update BEFORE UPDATE ON omnivia_semantic_assertions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_assertions is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertions_delete BEFORE DELETE ON omnivia_semantic_assertions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_assertions is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertion_evidence_insert
BEFORE INSERT ON omnivia_semantic_assertion_evidence BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_assertion_evidence')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertion_evidence_update BEFORE UPDATE ON omnivia_semantic_assertion_evidence BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_assertion_evidence is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertion_evidence_delete BEFORE DELETE ON omnivia_semantic_assertion_evidence BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_assertion_evidence is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertion_supersessions_insert
BEFORE INSERT ON omnivia_semantic_assertion_supersessions BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_assertion_supersessions')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertion_supersessions_update BEFORE UPDATE ON omnivia_semantic_assertion_supersessions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_assertion_supersessions is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertion_supersessions_delete BEFORE DELETE ON omnivia_semantic_assertion_supersessions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_assertion_supersessions is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertion_retractions_insert
BEFORE INSERT ON omnivia_semantic_assertion_retractions BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_assertion_retractions')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertion_retractions_update BEFORE UPDATE ON omnivia_semantic_assertion_retractions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_assertion_retractions is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_assertion_retractions_delete BEFORE DELETE ON omnivia_semantic_assertion_retractions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_assertion_retractions is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidates_insert
BEFORE INSERT ON omnivia_semantic_candidates BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_candidates')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidates_update BEFORE UPDATE ON omnivia_semantic_candidates BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_candidates is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidates_delete BEFORE DELETE ON omnivia_semantic_candidates BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_candidates is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidate_contributions_insert
BEFORE INSERT ON omnivia_semantic_candidate_contributions BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_candidate_contributions')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidate_contributions_update BEFORE UPDATE ON omnivia_semantic_candidate_contributions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_candidate_contributions is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidate_contributions_delete BEFORE DELETE ON omnivia_semantic_candidate_contributions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_candidate_contributions is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidate_suppressions_insert
BEFORE INSERT ON omnivia_semantic_candidate_suppressions BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_candidate_suppressions')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidate_suppressions_update BEFORE UPDATE ON omnivia_semantic_candidate_suppressions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_candidate_suppressions is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidate_suppressions_delete BEFORE DELETE ON omnivia_semantic_candidate_suppressions BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_candidate_suppressions is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidate_reconsiderations_insert
BEFORE INSERT ON omnivia_semantic_candidate_reconsiderations BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_candidate_reconsiderations')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidate_reconsiderations_update BEFORE UPDATE ON omnivia_semantic_candidate_reconsiderations BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_candidate_reconsiderations is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_candidate_reconsiderations_delete BEFORE DELETE ON omnivia_semantic_candidate_reconsiderations BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_candidate_reconsiderations is append-only; DELETE is never permitted'); END;
