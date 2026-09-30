-- Optional semantic assessment evidence for engineering relation candidates.
--
-- The provider boundary is deliberately split across two append-only records.
-- A request is staged only after deterministic discovery has committed, and its
-- terminal reconciliation is appended after the provider call.  The request
-- carries exact endpoint-input and provider-version provenance; the result is
-- evidence about a still-pending relation candidate and grants no governance
-- authority.

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_relation_candidates_assessment
    ON omnivia_engineering_relation_candidates
        (workspace_id, status, recorded_at_us, relation_candidate_id);

CREATE TABLE IF NOT EXISTS omnivia_engineering_relation_assessment_requests (
    workspace_id             TEXT    NOT NULL,
    assessment_request_id    TEXT    NOT NULL,
    relation_candidate_id    TEXT    NOT NULL,
    configuration_digest     TEXT    NOT NULL,
    provider_id              TEXT    NOT NULL,
    model_id                 TEXT    NOT NULL,
    prompt_version           TEXT    NOT NULL,
    request_schema_version   TEXT    NOT NULL,
    response_schema_version  TEXT    NOT NULL,
    input_digest             TEXT    NOT NULL,
    input_byte_count         INTEGER NOT NULL,
    input_token_count        INTEGER NOT NULL,
    tokenizer_id             TEXT    NOT NULL,
    timeout_ms               INTEGER NOT NULL,
    maximum_calls            INTEGER NOT NULL,
    maximum_concurrency      INTEGER NOT NULL,
    requested_at_us          INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, assessment_request_id),
    UNIQUE (workspace_id, relation_candidate_id, configuration_digest),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(assessment_request_id) = 'text'
           AND length(assessment_request_id) BETWEEN 1 AND 128
           AND assessment_request_id GLOB '[A-Za-z0-9]*'
           AND assessment_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(assessment_request_id, char(0)) = 0),
    CHECK (typeof(relation_candidate_id) = 'text'
           AND length(relation_candidate_id) BETWEEN 1 AND 128
           AND instr(relation_candidate_id, char(0)) = 0),
    CHECK (typeof(configuration_digest) = 'text'
           AND length(configuration_digest) = 71
           AND substr(configuration_digest, 1, 7) = 'sha256:'
           AND substr(configuration_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(provider_id) = 'text' AND length(provider_id) BETWEEN 1 AND 128
           AND instr(provider_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND instr(model_id, char(0)) = 0),
    CHECK (typeof(prompt_version) = 'text'
           AND length(prompt_version) BETWEEN 1 AND 64
           AND instr(prompt_version, char(0)) = 0),
    CHECK (typeof(request_schema_version) = 'text'
           AND length(request_schema_version) BETWEEN 1 AND 64
           AND instr(request_schema_version, char(0)) = 0),
    CHECK (typeof(response_schema_version) = 'text'
           AND length(response_schema_version) BETWEEN 1 AND 64
           AND instr(response_schema_version, char(0)) = 0),
    CHECK (typeof(input_digest) = 'text' AND length(input_digest) = 71
           AND substr(input_digest, 1, 7) = 'sha256:'
           AND substr(input_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(input_byte_count) = 'integer'
           AND input_byte_count BETWEEN 1 AND 65536),
    CHECK (typeof(input_token_count) = 'integer'
           AND input_token_count BETWEEN 1 AND 16384),
    CHECK (tokenizer_id = 'engineering.assessment.alnum-run-or-char.v1'),
    CHECK (typeof(timeout_ms) = 'integer' AND timeout_ms BETWEEN 1 AND 30000),
    CHECK (typeof(maximum_calls) = 'integer' AND maximum_calls BETWEEN 1 AND 50),
    CHECK (typeof(maximum_concurrency) = 'integer'
           AND maximum_concurrency = 1),
    CHECK (typeof(requested_at_us) = 'integer' AND requested_at_us > 0),

    FOREIGN KEY (workspace_id, relation_candidate_id)
        REFERENCES omnivia_engineering_relation_candidates
            (workspace_id, relation_candidate_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_relation_assessment_pending
    ON omnivia_engineering_relation_assessment_requests
        (workspace_id, configuration_digest, requested_at_us, assessment_request_id);

CREATE TABLE IF NOT EXISTS omnivia_engineering_relation_assessment_results (
    workspace_id                  TEXT    NOT NULL,
    assessment_request_id         TEXT    NOT NULL,
    relation_candidate_id         TEXT    NOT NULL,
    result_id                     TEXT    NOT NULL,
    status                        TEXT    NOT NULL,
    assessed_relation             TEXT,
    evidence_refs_json            TEXT,
    self_reported_confidence_ppm  INTEGER,
    response_digest               TEXT,
    failure_code                  TEXT,
    reconciled_at_us              INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, assessment_request_id),
    UNIQUE (workspace_id, result_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(assessment_request_id) = 'text'
           AND length(assessment_request_id) BETWEEN 1 AND 128
           AND instr(assessment_request_id, char(0)) = 0),
    CHECK (typeof(relation_candidate_id) = 'text'
           AND length(relation_candidate_id) BETWEEN 1 AND 128
           AND instr(relation_candidate_id, char(0)) = 0),
    CHECK (typeof(result_id) = 'text' AND length(result_id) BETWEEN 1 AND 128
           AND result_id GLOB '[A-Za-z0-9]*'
           AND result_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(result_id, char(0)) = 0),
    CHECK (status IN ('assessed', 'unavailable', 'failed')),
    CHECK (assessed_relation IS NULL OR assessed_relation IN
           ('related', 'compatible', 'scoped_difference', 'conflicts_with',
            'supersedes', 'not_conflict')),
    CHECK (evidence_refs_json IS NULL
           OR (typeof(evidence_refs_json) = 'text'
               AND length(CAST(evidence_refs_json AS BLOB)) BETWEEN 2 AND 32768
               AND json_valid(evidence_refs_json) = 1
               AND json_type(evidence_refs_json) = 'array'
               AND json(evidence_refs_json) = evidence_refs_json)),
    CHECK (self_reported_confidence_ppm IS NULL
           OR (typeof(self_reported_confidence_ppm) = 'integer'
               AND self_reported_confidence_ppm BETWEEN 0 AND 1000000)),
    CHECK (response_digest IS NULL
           OR (typeof(response_digest) = 'text' AND length(response_digest) = 71
               AND substr(response_digest, 1, 7) = 'sha256:'
               AND substr(response_digest, 8) NOT GLOB '*[^0-9a-f]*')),
    CHECK (failure_code IS NULL
           OR (typeof(failure_code) = 'text' AND length(failure_code) BETWEEN 1 AND 64
               AND failure_code GLOB '[a-z]*'
               AND failure_code NOT GLOB '*[^a-z0-9_.]*'
               AND instr(failure_code, char(0)) = 0)),
    CHECK (typeof(reconciled_at_us) = 'integer' AND reconciled_at_us > 0),
    CHECK ((status = 'assessed'
            AND assessed_relation IS NOT NULL
            AND evidence_refs_json IS NOT NULL
            AND self_reported_confidence_ppm IS NOT NULL
            AND response_digest IS NOT NULL
            AND failure_code IS NULL)
           OR (status IN ('unavailable', 'failed')
               AND assessed_relation IS NULL
               AND evidence_refs_json IS NULL
               AND self_reported_confidence_ppm IS NULL
               AND response_digest IS NULL
               AND failure_code IS NOT NULL)),

    FOREIGN KEY (workspace_id, assessment_request_id)
        REFERENCES omnivia_engineering_relation_assessment_requests
            (workspace_id, assessment_request_id),
    FOREIGN KEY (workspace_id, relation_candidate_id)
        REFERENCES omnivia_engineering_relation_candidates
            (workspace_id, relation_candidate_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_relation_assessment_results_candidate
    ON omnivia_engineering_relation_assessment_results
        (workspace_id, relation_candidate_id, reconciled_at_us, result_id);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_relation_assessment_requests_insert
BEFORE INSERT ON omnivia_engineering_relation_assessment_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_relation_assessment_requests')
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
    SELECT RAISE(ABORT, 'omnivia: relation assessment requires a committed pending discovery candidate')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_relation_candidates c
        JOIN omnivia_engineering_discovery_run_events terminal
          ON terminal.workspace_id = c.workspace_id
         AND terminal.discovery_run_id = c.first_discovery_run_id
         AND terminal.event_sequence = 2
         AND terminal.state = 'completed'
        WHERE c.workspace_id = NEW.workspace_id
          AND c.relation_candidate_id = NEW.relation_candidate_id
          AND c.status = 'pending'
          AND NEW.requested_at_us >= c.recorded_at_us);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_relation_assessment_requests_update
BEFORE UPDATE ON omnivia_engineering_relation_assessment_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_relation_assessment_requests is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_relation_assessment_requests_delete
BEFORE DELETE ON omnivia_engineering_relation_assessment_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_relation_assessment_requests is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_relation_assessment_results_insert
BEFORE INSERT ON omnivia_engineering_relation_assessment_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_relation_assessment_results')
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
    SELECT RAISE(ABORT, 'omnivia: relation assessment result does not match its staged request')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_relation_assessment_requests request
        WHERE request.workspace_id = NEW.workspace_id
          AND request.assessment_request_id = NEW.assessment_request_id
          AND request.relation_candidate_id = NEW.relation_candidate_id
          AND NEW.reconciled_at_us >= request.requested_at_us);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_relation_assessment_results_update
BEFORE UPDATE ON omnivia_engineering_relation_assessment_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_relation_assessment_results is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_relation_assessment_results_delete
BEFORE DELETE ON omnivia_engineering_relation_assessment_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_relation_assessment_results is append-only; DELETE is never permitted');
END;
