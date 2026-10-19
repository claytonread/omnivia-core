-- Model-neutral semantic vector projection (semantic retrieval foundation).
--
-- Reuses 0011's generic projection lifecycle: runs, checkpoints, validations, activations
-- and the single mutable ledger pointer.  No second lifecycle ledger is created and nothing
-- here moves the activation pointer; activation stays the AFTER trigger of the
-- activations INSERT.  The lexical `evidence.search.fts5` projection is untouched and does
-- not depend on anything declared here.
--
-- Four tables, each with a separate job:
--   omnivia_semantic_profiles         WHAT the components mean (append-only identity).
--   omnivia_semantic_run_executions   HOW one run produced them, plus the run's stored
--                                     representation and an explicit compatibility
--                                     decision (one row per run, append-preserved).
--   omnivia_semantic_vectors          one vector per exact canonical model-input digest
--                                     per run, bound to the run that wrote it.
--   omnivia_semantic_vector_occurrences
--                                     one row per evidence artifact per run, naming the
--                                     input digest whose vector represents it.
--
-- Vectors and occurrences deliberately carry no ACL labels, tombstone state, citation or
-- source-authority state.  Those are live facts read from the authoritative evidence rows
-- at request time; a copy here could only ever go stale in the permissive direction.
--
-- The profile digest is the run's `profile_version`.  A run built under one profile is
-- therefore distinguishable from one built under another without a second column on the
-- generic run table.
--
-- Every comment in this file sits between statements and never inside one: the migrator
-- strips comments while the canonical schema fingerprint replays the text verbatim.

CREATE TABLE IF NOT EXISTS omnivia_semantic_profiles (
    workspace_id            TEXT    NOT NULL,
    profile_digest          TEXT    NOT NULL,
    dimensions              INTEGER NOT NULL,
    weights_id              TEXT    NOT NULL,
    tokenizer_id            TEXT    NOT NULL,
    transform_id            TEXT    NOT NULL,
    pooling_id              TEXT    NOT NULL,
    normalization_id        TEXT    NOT NULL,
    canonical_input_version TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, profile_digest),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(profile_digest) = 'text' AND length(profile_digest) = 71
           AND substr(profile_digest, 1, 7) = 'sha256:'
           AND substr(profile_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(profile_digest, char(0)) = 0),
    CHECK (typeof(dimensions) = 'integer' AND dimensions BETWEEN 1 AND 8192),
    CHECK (typeof(weights_id) = 'text' AND length(weights_id) BETWEEN 1 AND 128
           AND weights_id GLOB '[A-Za-z0-9]*'
           AND weights_id NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(weights_id, char(0)) = 0),
    CHECK (typeof(tokenizer_id) = 'text' AND length(tokenizer_id) BETWEEN 1 AND 128
           AND tokenizer_id GLOB '[A-Za-z0-9]*'
           AND tokenizer_id NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(tokenizer_id, char(0)) = 0),
    CHECK (typeof(transform_id) = 'text' AND length(transform_id) BETWEEN 1 AND 128
           AND transform_id GLOB '[A-Za-z0-9]*'
           AND transform_id NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(transform_id, char(0)) = 0),
    CHECK (typeof(pooling_id) = 'text' AND length(pooling_id) BETWEEN 1 AND 128
           AND pooling_id GLOB '[A-Za-z0-9]*'
           AND pooling_id NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(pooling_id, char(0)) = 0),
    CHECK (normalization_id = 'core_l2_unit_v1'),
    CHECK (typeof(canonical_input_version) = 'text' AND length(canonical_input_version) BETWEEN 1 AND 128
           AND canonical_input_version GLOB '[A-Za-z0-9]*'
           AND canonical_input_version NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(canonical_input_version, char(0)) = 0)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_run_executions (
    workspace_id          TEXT NOT NULL,
    projection_id         TEXT NOT NULL,
    run_id                TEXT NOT NULL,
    profile_digest        TEXT NOT NULL,
    provider_id           TEXT NOT NULL,
    runtime_id            TEXT NOT NULL,
    build_id              TEXT NOT NULL,
    hardware_id           TEXT NOT NULL,
    inference_precision   TEXT NOT NULL,
    compatibility_status  TEXT NOT NULL,
    compatibility_reason  TEXT NOT NULL,
    scalar_encoding       TEXT NOT NULL,
    byte_order            TEXT NOT NULL,
    metric                TEXT NOT NULL,
    codec_version         TEXT NOT NULL,
    index_version         TEXT NOT NULL,
    execution_digest      TEXT NOT NULL,
    representation_digest TEXT NOT NULL,
    generation_digest     TEXT NOT NULL,

    PRIMARY KEY (workspace_id, projection_id, run_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(projection_id) = 'text' AND length(projection_id) BETWEEN 1 AND 128
           AND projection_id GLOB '[A-Za-z0-9]*'
           AND projection_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(projection_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(profile_digest) = 'text' AND length(profile_digest) = 71
           AND substr(profile_digest, 1, 7) = 'sha256:'
           AND substr(profile_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(profile_digest, char(0)) = 0),
    CHECK (typeof(provider_id) = 'text' AND length(provider_id) BETWEEN 1 AND 128
           AND provider_id GLOB '[A-Za-z0-9]*'
           AND provider_id NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(provider_id, char(0)) = 0),
    CHECK (typeof(runtime_id) = 'text' AND length(runtime_id) BETWEEN 1 AND 128
           AND runtime_id GLOB '[A-Za-z0-9]*'
           AND runtime_id NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(runtime_id, char(0)) = 0),
    CHECK (typeof(build_id) = 'text' AND length(build_id) BETWEEN 1 AND 128
           AND build_id GLOB '[A-Za-z0-9]*'
           AND build_id NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(build_id, char(0)) = 0),
    CHECK (typeof(hardware_id) = 'text' AND length(hardware_id) BETWEEN 1 AND 128
           AND hardware_id GLOB '[A-Za-z0-9]*'
           AND hardware_id NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(hardware_id, char(0)) = 0),
    CHECK (typeof(inference_precision) = 'text' AND length(inference_precision) BETWEEN 1 AND 128
           AND inference_precision GLOB '[A-Za-z0-9]*'
           AND inference_precision NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(inference_precision, char(0)) = 0),
    CHECK (compatibility_status IN ('compatible', 'incompatible')),
    CHECK (typeof(compatibility_reason) = 'text' AND length(compatibility_reason) BETWEEN 1 AND 128
           AND compatibility_reason GLOB '[A-Za-z0-9]*'
           AND compatibility_reason NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(compatibility_reason, char(0)) = 0),
    CHECK (scalar_encoding = 'float32_le'),
    CHECK (byte_order = 'little'),
    CHECK (metric IN ('cosine', 'dot')),
    CHECK (typeof(codec_version) = 'text' AND length(codec_version) BETWEEN 1 AND 128
           AND codec_version GLOB '[A-Za-z0-9]*'
           AND codec_version NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(codec_version, char(0)) = 0),
    CHECK (typeof(index_version) = 'text' AND length(index_version) BETWEEN 1 AND 128
           AND index_version GLOB '[A-Za-z0-9]*'
           AND index_version NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(index_version, char(0)) = 0),
    CHECK (typeof(execution_digest) = 'text' AND length(execution_digest) = 71
           AND substr(execution_digest, 1, 7) = 'sha256:'
           AND substr(execution_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(execution_digest, char(0)) = 0),
    CHECK (typeof(representation_digest) = 'text' AND length(representation_digest) = 71
           AND substr(representation_digest, 1, 7) = 'sha256:'
           AND substr(representation_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(representation_digest, char(0)) = 0),
    CHECK (typeof(generation_digest) = 'text' AND length(generation_digest) = 71
           AND substr(generation_digest, 1, 7) = 'sha256:'
           AND substr(generation_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(generation_digest, char(0)) = 0),

    FOREIGN KEY (workspace_id, projection_id, run_id)
        REFERENCES omnivia_projection_runs (workspace_id, projection_id, run_id),
    FOREIGN KEY (workspace_id, profile_digest)
        REFERENCES omnivia_semantic_profiles (workspace_id, profile_digest)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_vectors (
    workspace_id  TEXT    NOT NULL,
    projection_id TEXT    NOT NULL,
    run_id        TEXT    NOT NULL,
    input_digest  TEXT    NOT NULL,
    dimensions    INTEGER NOT NULL,
    representation TEXT   NOT NULL,
    codec_version TEXT    NOT NULL,
    vector        BLOB    NOT NULL,

    PRIMARY KEY (workspace_id, projection_id, run_id, input_digest),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(projection_id) = 'text' AND length(projection_id) BETWEEN 1 AND 128
           AND projection_id GLOB '[A-Za-z0-9]*'
           AND projection_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(projection_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(input_digest) = 'text' AND length(input_digest) = 71
           AND substr(input_digest, 1, 7) = 'sha256:'
           AND substr(input_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(input_digest, char(0)) = 0),
    CHECK (typeof(dimensions) = 'integer' AND dimensions BETWEEN 1 AND 8192),
    CHECK (representation = 'float32_le'),
    CHECK (typeof(codec_version) = 'text' AND length(codec_version) BETWEEN 1 AND 128
           AND codec_version GLOB '[A-Za-z0-9]*'
           AND codec_version NOT GLOB '*[^A-Za-z0-9._:/@+-]*'
           AND instr(codec_version, char(0)) = 0),
    CHECK (typeof(vector) = 'blob' AND length(vector) = dimensions * 4),

    FOREIGN KEY (workspace_id, projection_id, run_id)
        REFERENCES omnivia_projection_runs (workspace_id, projection_id, run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_vector_occurrences (
    workspace_id  TEXT NOT NULL,
    projection_id TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    evidence_id   TEXT NOT NULL,
    input_digest  TEXT NOT NULL,

    PRIMARY KEY (workspace_id, projection_id, run_id, evidence_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(projection_id) = 'text' AND length(projection_id) BETWEEN 1 AND 128
           AND projection_id GLOB '[A-Za-z0-9]*'
           AND projection_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(projection_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(evidence_id) = 'text' AND length(evidence_id) BETWEEN 1 AND 128
           AND evidence_id GLOB '[A-Za-z0-9]*'
           AND evidence_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(evidence_id, char(0)) = 0),
    CHECK (typeof(input_digest) = 'text' AND length(input_digest) = 71
           AND substr(input_digest, 1, 7) = 'sha256:'
           AND substr(input_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(input_digest, char(0)) = 0),

    FOREIGN KEY (workspace_id, projection_id, run_id)
        REFERENCES omnivia_projection_runs (workspace_id, projection_id, run_id),
    FOREIGN KEY (workspace_id, projection_id, run_id, input_digest)
        REFERENCES omnivia_semantic_vectors (workspace_id, projection_id, run_id, input_digest)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_semantic_occurrences_input
    ON omnivia_semantic_vector_occurrences (workspace_id, projection_id, run_id, input_digest);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_profiles_insert
BEFORE INSERT ON omnivia_semantic_profiles
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_profiles')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: semantic profile identity already exists')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_semantic_profiles
        WHERE workspace_id = NEW.workspace_id AND profile_digest = NEW.profile_digest);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_profiles_update
BEFORE UPDATE ON omnivia_semantic_profiles
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_profiles is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_profiles_delete
BEFORE DELETE ON omnivia_semantic_profiles
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_profiles is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_run_executions_insert
BEFORE INSERT ON omnivia_semantic_run_executions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_run_executions')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: semantic run execution already recorded')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_semantic_run_executions
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: semantic run execution requires a running run of the same profile')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_runs
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id AND state = 'running'
          AND profile_version = NEW.profile_digest);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_run_executions_update
BEFORE UPDATE ON omnivia_semantic_run_executions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_run_executions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_run_executions_delete
BEFORE DELETE ON omnivia_semantic_run_executions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_run_executions is append-preserved; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_vectors_insert
BEFORE INSERT ON omnivia_semantic_vectors
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_vectors')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: semantic vector identity already exists')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_semantic_vectors
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id AND input_digest = NEW.input_digest);
    SELECT RAISE(ABORT, 'omnivia: semantic rows require a running projection run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_runs
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id AND state = 'running');
    SELECT RAISE(ABORT, 'omnivia: semantic vector must match its run profile and representation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_run_executions e
        JOIN omnivia_semantic_profiles p
          ON p.workspace_id = e.workspace_id AND p.profile_digest = e.profile_digest
        WHERE e.workspace_id = NEW.workspace_id AND e.projection_id = NEW.projection_id
          AND e.run_id = NEW.run_id AND p.dimensions = NEW.dimensions
          AND e.scalar_encoding = NEW.representation
          AND e.codec_version = NEW.codec_version);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_vectors_update
BEFORE UPDATE ON omnivia_semantic_vectors
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_vectors is append-only; UPDATE is never permitted');
END;


CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_vectors_delete
BEFORE DELETE ON omnivia_semantic_vectors
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_semantic_vectors')
    WHERE omnivia_service_writer() IS NOT 1
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: only a failed or superseded semantic run may be reclaimed')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_runs
        WHERE workspace_id = OLD.workspace_id AND projection_id = OLD.projection_id
          AND run_id = OLD.run_id AND state IN ('failed', 'superseded'));
    SELECT RAISE(ABORT, 'omnivia: the active semantic generation may not be deleted')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_projection_ledger
        WHERE projection_id = OLD.projection_id AND active_run_id = OLD.run_id);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_vector_occurrences_insert
BEFORE INSERT ON omnivia_semantic_vector_occurrences
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_vector_occurrences')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: semantic occurrence identity already exists')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_semantic_vector_occurrences
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id AND evidence_id = NEW.evidence_id);
    SELECT RAISE(ABORT, 'omnivia: semantic rows require a running projection run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_runs
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id AND state = 'running');
    SELECT RAISE(ABORT, 'omnivia: semantic occurrence requires a known evidence artifact')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts
        WHERE workspace_id = NEW.workspace_id AND evidence_id = NEW.evidence_id);
    SELECT RAISE(ABORT, 'omnivia: semantic occurrence requires a stored vector of the same run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_vectors
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id AND input_digest = NEW.input_digest);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_vector_occurrences_update
BEFORE UPDATE ON omnivia_semantic_vector_occurrences
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_vector_occurrences is append-only; UPDATE is never permitted');
END;


CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_vector_occurrences_delete
BEFORE DELETE ON omnivia_semantic_vector_occurrences
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_semantic_vector_occurrences')
    WHERE omnivia_service_writer() IS NOT 1
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: only a failed or superseded semantic run may be reclaimed')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_runs
        WHERE workspace_id = OLD.workspace_id AND projection_id = OLD.projection_id
          AND run_id = OLD.run_id AND state IN ('failed', 'superseded'));
    SELECT RAISE(ABORT, 'omnivia: the active semantic generation may not be deleted')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_projection_ledger
        WHERE projection_id = OLD.projection_id AND active_run_id = OLD.run_id);
END;
