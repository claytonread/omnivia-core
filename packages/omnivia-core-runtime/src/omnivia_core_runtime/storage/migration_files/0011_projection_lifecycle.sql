-- Projection lifecycle evidence and atomic activation (V06-1 M5).
--
-- The existing projection ledger remains the sole mutable active pointer.  This
-- migration adds append-preserved build evidence and an activation statement
-- whose AFTER trigger atomically supersedes the previous run and advances that
-- pointer.  It intentionally builds no projection and adds no Runtime API.

ALTER TABLE omnivia_projection_ledger ADD COLUMN projection_kind TEXT
    CHECK (projection_kind IS NULL OR (
        typeof(projection_kind) = 'text' AND length(projection_kind) BETWEEN 1 AND 128
        AND projection_kind GLOB '[a-z]*'
        AND projection_kind NOT GLOB '*[^a-z0-9_.]*'
        AND projection_kind NOT GLOB '*.'
        AND projection_kind NOT GLOB '*.[^a-z]*'
        AND instr(projection_kind, char(0)) = 0));
ALTER TABLE omnivia_projection_ledger ADD COLUMN schema_version TEXT
    CHECK (schema_version IS NULL OR (
        typeof(schema_version) = 'text' AND length(schema_version) BETWEEN 1 AND 128
        AND schema_version GLOB '[A-Za-z0-9]*'
        AND schema_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(schema_version, char(0)) = 0));
ALTER TABLE omnivia_projection_ledger ADD COLUMN profile_version TEXT
    CHECK (profile_version IS NULL OR (
        typeof(profile_version) = 'text' AND length(profile_version) BETWEEN 1 AND 128
        AND profile_version GLOB '[A-Za-z0-9]*'
        AND profile_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(profile_version, char(0)) = 0));
ALTER TABLE omnivia_projection_ledger ADD COLUMN active_run_id TEXT
    CHECK (active_run_id IS NULL OR (
        typeof(active_run_id) = 'text' AND length(active_run_id) BETWEEN 1 AND 128
        AND active_run_id GLOB '[A-Za-z0-9]*'
        AND active_run_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
        AND instr(active_run_id, char(0)) = 0));
ALTER TABLE omnivia_projection_ledger ADD COLUMN active_epoch INTEGER
    CHECK (active_epoch IS NULL OR (typeof(active_epoch) = 'integer' AND active_epoch > 0));
ALTER TABLE omnivia_projection_ledger ADD COLUMN active_source_checkpoint TEXT
    CHECK (active_source_checkpoint IS NULL OR (
        typeof(active_source_checkpoint) = 'text'
        AND length(active_source_checkpoint) BETWEEN 1 AND 512
        AND instr(active_source_checkpoint, char(0)) = 0));
ALTER TABLE omnivia_projection_ledger ADD COLUMN active_build_digest TEXT
    CHECK (active_build_digest IS NULL OR (
        typeof(active_build_digest) = 'text' AND length(active_build_digest) = 71
        AND substr(active_build_digest, 1, 7) = 'sha256:'
        AND substr(active_build_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(active_build_digest, char(0)) = 0));
ALTER TABLE omnivia_projection_ledger ADD COLUMN active_validation_digest TEXT
    CHECK (active_validation_digest IS NULL OR (
        typeof(active_validation_digest) = 'text' AND length(active_validation_digest) = 71
        AND substr(active_validation_digest, 1, 7) = 'sha256:'
        AND substr(active_validation_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(active_validation_digest, char(0)) = 0));
ALTER TABLE omnivia_projection_ledger ADD COLUMN activated_at_us INTEGER
    CHECK (activated_at_us IS NULL OR (
        typeof(activated_at_us) = 'integer' AND activated_at_us > 0));

CREATE TABLE IF NOT EXISTS omnivia_projection_runs (
    workspace_id                     TEXT    NOT NULL,
    projection_id                    TEXT    NOT NULL,
    run_id                           TEXT    NOT NULL,
    projection_kind                  TEXT    NOT NULL,
    schema_version                   TEXT    NOT NULL,
    profile_version                  TEXT    NOT NULL,
    source_checkpoint                TEXT    NOT NULL,
    target_epoch                     INTEGER NOT NULL,
    started_at_us                    INTEGER NOT NULL,
    validation_started_at_us         INTEGER,
    finished_at_us                   INTEGER,
    state                            TEXT    NOT NULL,
    input_record_count               INTEGER,
    output_record_count              INTEGER,
    build_digest                     TEXT,
    error_json                       TEXT,
    resumed_from_run_id              TEXT,
    resumed_from_checkpoint_sequence INTEGER,

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
    CHECK (typeof(projection_kind) = 'text' AND length(projection_kind) BETWEEN 1 AND 128
           AND projection_kind GLOB '[a-z]*'
           AND projection_kind NOT GLOB '*[^a-z0-9_.]*'
           AND projection_kind NOT GLOB '*.'
           AND projection_kind NOT GLOB '*.[^a-z]*'
        AND instr(projection_kind, char(0)) = 0),
    CHECK (typeof(schema_version) = 'text' AND length(schema_version) BETWEEN 1 AND 128
           AND schema_version GLOB '[A-Za-z0-9]*'
           AND schema_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(schema_version, char(0)) = 0),
    CHECK (typeof(profile_version) = 'text' AND length(profile_version) BETWEEN 1 AND 128
           AND profile_version GLOB '[A-Za-z0-9]*'
           AND profile_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(profile_version, char(0)) = 0),
    CHECK (typeof(source_checkpoint) = 'text'
           AND length(source_checkpoint) BETWEEN 1 AND 512
           AND instr(source_checkpoint, char(0)) = 0),
    CHECK (typeof(target_epoch) = 'integer' AND target_epoch > 0),
    CHECK (typeof(started_at_us) = 'integer' AND started_at_us > 0),
    CHECK (validation_started_at_us IS NULL OR (
           typeof(validation_started_at_us) = 'integer'
           AND validation_started_at_us >= started_at_us)),
    CHECK (finished_at_us IS NULL OR (
           typeof(finished_at_us) = 'integer' AND finished_at_us >= started_at_us
           AND (validation_started_at_us IS NULL
                OR finished_at_us >= validation_started_at_us))),
    CHECK (state IN ('running', 'validating', 'succeeded', 'failed', 'superseded')),
    CHECK (input_record_count IS NULL OR (
           typeof(input_record_count) = 'integer' AND input_record_count >= 0)),
    CHECK (output_record_count IS NULL OR (
           typeof(output_record_count) = 'integer' AND output_record_count >= 0)),
    CHECK (build_digest IS NULL OR (
           typeof(build_digest) = 'text' AND length(build_digest) = 71
           AND substr(build_digest, 1, 7) = 'sha256:'
           AND substr(build_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(build_digest, char(0)) = 0)),
    CHECK (error_json IS NULL OR (
           typeof(error_json) = 'text'
           AND length(CAST(error_json AS BLOB)) BETWEEN 2 AND 65536)),
    CHECK ((resumed_from_run_id IS NULL) =
           (resumed_from_checkpoint_sequence IS NULL)),
    CHECK (resumed_from_run_id IS NULL OR (
           typeof(resumed_from_run_id) = 'text'
           AND length(resumed_from_run_id) BETWEEN 1 AND 128
           AND resumed_from_run_id GLOB '[A-Za-z0-9]*'
           AND resumed_from_run_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(resumed_from_run_id, char(0)) = 0)),
    CHECK (resumed_from_checkpoint_sequence IS NULL OR (
           typeof(resumed_from_checkpoint_sequence) = 'integer'
           AND resumed_from_checkpoint_sequence >= 0)),
    CHECK (
        (state = 'running' AND validation_started_at_us IS NULL
         AND finished_at_us IS NULL AND input_record_count IS NULL
         AND output_record_count IS NULL AND build_digest IS NULL
         AND error_json IS NULL)
        OR (state = 'validating' AND validation_started_at_us IS NOT NULL
            AND finished_at_us IS NULL AND input_record_count IS NULL
            AND output_record_count IS NULL AND build_digest IS NULL
            AND error_json IS NULL)
        OR (state IN ('succeeded', 'superseded')
            AND validation_started_at_us IS NOT NULL
            AND finished_at_us IS NOT NULL
            AND input_record_count IS NOT NULL
            AND output_record_count IS NOT NULL
            AND build_digest IS NOT NULL AND error_json IS NULL)
        OR (state = 'failed' AND finished_at_us IS NOT NULL
            AND error_json IS NOT NULL)
    ),

    FOREIGN KEY (projection_id) REFERENCES omnivia_projection_ledger (projection_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_projection_run_checkpoints (
    workspace_id        TEXT    NOT NULL,
    projection_id       TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    checkpoint_sequence INTEGER NOT NULL,
    created_at_us       INTEGER NOT NULL,
    source_checkpoint   TEXT    NOT NULL,
    cursor_json         TEXT    NOT NULL,
    checkpoint_digest   TEXT    NOT NULL,
    input_record_count  INTEGER NOT NULL,
    output_record_count INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, projection_id, run_id, checkpoint_sequence),

    CHECK (typeof(checkpoint_sequence) = 'integer' AND checkpoint_sequence >= 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(source_checkpoint) = 'text'
           AND length(source_checkpoint) BETWEEN 1 AND 512
           AND instr(source_checkpoint, char(0)) = 0),
    CHECK (typeof(cursor_json) = 'text'
           AND length(CAST(cursor_json AS BLOB)) BETWEEN 2 AND 1048576),
    CHECK (typeof(checkpoint_digest) = 'text' AND length(checkpoint_digest) = 71
           AND substr(checkpoint_digest, 1, 7) = 'sha256:'
           AND substr(checkpoint_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(checkpoint_digest, char(0)) = 0),
    CHECK (typeof(input_record_count) = 'integer' AND input_record_count >= 0),
    CHECK (typeof(output_record_count) = 'integer' AND output_record_count >= 0),

    FOREIGN KEY (workspace_id, projection_id, run_id)
        REFERENCES omnivia_projection_runs (workspace_id, projection_id, run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_projection_record_failures (
    workspace_id     TEXT    NOT NULL,
    projection_id    TEXT    NOT NULL,
    run_id           TEXT    NOT NULL,
    failure_sequence INTEGER NOT NULL,
    record_id        TEXT    NOT NULL,
    phase            TEXT    NOT NULL,
    error_code       TEXT    NOT NULL,
    sanitized_detail TEXT,
    occurred_at_us   INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, projection_id, run_id, failure_sequence),

    CHECK (typeof(failure_sequence) = 'integer' AND failure_sequence >= 0),
    CHECK (typeof(record_id) = 'text' AND length(record_id) BETWEEN 1 AND 128
           AND record_id GLOB '[A-Za-z0-9]*'
           AND record_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(record_id, char(0)) = 0),
    CHECK (typeof(phase) = 'text' AND length(phase) BETWEEN 1 AND 128
           AND phase GLOB '[a-z]*' AND phase NOT GLOB '*[^a-z0-9_.]*'
           AND phase NOT GLOB '*.' AND phase NOT GLOB '*.[^a-z]*'
           AND instr(phase, char(0)) = 0),
    CHECK (typeof(error_code) = 'text' AND length(error_code) BETWEEN 1 AND 128
           AND error_code GLOB '[a-z]*' AND error_code NOT GLOB '*[^a-z0-9_.]*'
           AND error_code NOT GLOB '*.' AND error_code NOT GLOB '*.[^a-z]*'
           AND instr(error_code, char(0)) = 0),
    CHECK (sanitized_detail IS NULL OR (
           typeof(sanitized_detail) = 'text' AND length(sanitized_detail) <= 2048
           AND instr(sanitized_detail, char(0)) = 0)),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),

    FOREIGN KEY (workspace_id, projection_id, run_id)
        REFERENCES omnivia_projection_runs (workspace_id, projection_id, run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_projection_validations (
    workspace_id       TEXT    NOT NULL,
    projection_id      TEXT    NOT NULL,
    run_id             TEXT    NOT NULL,
    validated_at_us    INTEGER NOT NULL,
    accepted           INTEGER NOT NULL,
    input_record_count INTEGER NOT NULL,
    output_record_count INTEGER NOT NULL,
    build_digest       TEXT    NOT NULL,
    report_json        TEXT    NOT NULL,
    validation_digest  TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, projection_id, run_id),

    CHECK (typeof(validated_at_us) = 'integer' AND validated_at_us > 0),
    CHECK (typeof(accepted) = 'integer' AND accepted IN (0, 1)),
    CHECK (typeof(input_record_count) = 'integer' AND input_record_count >= 0),
    CHECK (typeof(output_record_count) = 'integer' AND output_record_count >= 0),
    CHECK (typeof(build_digest) = 'text' AND length(build_digest) = 71
           AND substr(build_digest, 1, 7) = 'sha256:'
           AND substr(build_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(build_digest, char(0)) = 0),
    CHECK (typeof(report_json) = 'text'
           AND length(CAST(report_json AS BLOB)) BETWEEN 2 AND 1048576),
    CHECK (typeof(validation_digest) = 'text' AND length(validation_digest) = 71
           AND substr(validation_digest, 1, 7) = 'sha256:'
           AND substr(validation_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(validation_digest, char(0)) = 0),

    FOREIGN KEY (workspace_id, projection_id, run_id)
        REFERENCES omnivia_projection_runs (workspace_id, projection_id, run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_projection_activations (
    workspace_id       TEXT    NOT NULL,
    projection_id      TEXT    NOT NULL,
    activation_sequence INTEGER NOT NULL,
    run_id             TEXT    NOT NULL,
    target_epoch       INTEGER NOT NULL,
    previous_run_id    TEXT,
    activated_at_us    INTEGER NOT NULL,
    source_checkpoint  TEXT    NOT NULL,
    build_digest       TEXT    NOT NULL,
    validation_digest  TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, projection_id, activation_sequence),
    UNIQUE (workspace_id, projection_id, run_id),

    CHECK (typeof(activation_sequence) = 'integer' AND activation_sequence >= 0),
    CHECK (typeof(target_epoch) = 'integer' AND target_epoch > 0),
    CHECK (previous_run_id IS NULL OR (
           typeof(previous_run_id) = 'text' AND length(previous_run_id) BETWEEN 1 AND 128
           AND previous_run_id GLOB '[A-Za-z0-9]*'
           AND previous_run_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(previous_run_id, char(0)) = 0)),
    CHECK (typeof(activated_at_us) = 'integer' AND activated_at_us > 0),
    CHECK (typeof(source_checkpoint) = 'text'
           AND length(source_checkpoint) BETWEEN 1 AND 512
           AND instr(source_checkpoint, char(0)) = 0),
    CHECK (typeof(build_digest) = 'text' AND length(build_digest) = 71
           AND substr(build_digest, 1, 7) = 'sha256:'
           AND substr(build_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(build_digest, char(0)) = 0),
    CHECK (typeof(validation_digest) = 'text' AND length(validation_digest) = 71
           AND substr(validation_digest, 1, 7) = 'sha256:'
           AND substr(validation_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(validation_digest, char(0)) = 0),

    FOREIGN KEY (workspace_id, projection_id, run_id)
        REFERENCES omnivia_projection_runs (workspace_id, projection_id, run_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_projection_runs_state_epoch
    ON omnivia_projection_runs (workspace_id, projection_id, state, target_epoch DESC);
CREATE INDEX IF NOT EXISTS omnivia_idx_projection_checkpoints_latest
    ON omnivia_projection_run_checkpoints
       (workspace_id, projection_id, run_id, checkpoint_sequence DESC);
CREATE INDEX IF NOT EXISTS omnivia_idx_projection_failures_record_code
    ON omnivia_projection_record_failures
       (workspace_id, projection_id, record_id, error_code, failure_sequence);
CREATE INDEX IF NOT EXISTS omnivia_idx_projection_validations_outcome
    ON omnivia_projection_validations
       (workspace_id, projection_id, accepted, validated_at_us DESC);
CREATE INDEX IF NOT EXISTS omnivia_idx_projection_activations_history
    ON omnivia_projection_activations
       (workspace_id, projection_id, activation_sequence DESC);

-- Every M5 write is bound to the current service writer, workspace and lease.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_runs_insert
BEFORE INSERT ON omnivia_projection_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_projection_runs')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: projection run must start in running state')
    WHERE NEW.state <> 'running';
    SELECT RAISE(ABORT, 'omnivia: projection run declaration must match ledger identity')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_ledger
        WHERE projection_id = NEW.projection_id
          AND (projection_kind IS NULL OR projection_kind = NEW.projection_kind)
    );
    SELECT RAISE(ABORT, 'omnivia: projection target epoch must advance')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_projection_ledger
        WHERE projection_id = NEW.projection_id
          AND active_epoch IS NOT NULL AND NEW.target_epoch <= active_epoch
    );
    SELECT RAISE(ABORT, 'omnivia: projection target epoch is already declared')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_projection_runs
        WHERE workspace_id = NEW.workspace_id
          AND projection_id = NEW.projection_id
          AND target_epoch = NEW.target_epoch
          AND state <> 'failed'
    );
    SELECT RAISE(ABORT, 'omnivia: projection resume checkpoint does not match declaration')
    WHERE NEW.resumed_from_run_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_projection_run_checkpoints c
        JOIN omnivia_projection_runs r
          ON r.workspace_id = c.workspace_id
         AND r.projection_id = c.projection_id AND r.run_id = c.run_id
        WHERE c.workspace_id = NEW.workspace_id
          AND c.projection_id = NEW.projection_id
          AND c.run_id = NEW.resumed_from_run_id
          AND c.checkpoint_sequence = NEW.resumed_from_checkpoint_sequence
          AND r.target_epoch = NEW.target_epoch
          AND r.source_checkpoint = NEW.source_checkpoint
          AND r.started_at_us < NEW.started_at_us
      );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_runs_update
BEFORE UPDATE ON omnivia_projection_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_projection_runs')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT OLD.workspace_id;
    SELECT RAISE(ABORT, 'omnivia: projection run declaration is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.projection_id IS NOT OLD.projection_id
       OR NEW.run_id IS NOT OLD.run_id
       OR NEW.projection_kind IS NOT OLD.projection_kind
       OR NEW.schema_version IS NOT OLD.schema_version
       OR NEW.profile_version IS NOT OLD.profile_version
       OR NEW.source_checkpoint IS NOT OLD.source_checkpoint
       OR NEW.target_epoch IS NOT OLD.target_epoch
       OR NEW.started_at_us IS NOT OLD.started_at_us
       OR NEW.resumed_from_run_id IS NOT OLD.resumed_from_run_id
       OR NEW.resumed_from_checkpoint_sequence IS NOT OLD.resumed_from_checkpoint_sequence;
    SELECT RAISE(ABORT, 'omnivia: illegal projection run lifecycle transition')
    WHERE NOT (
        (OLD.state = 'running' AND NEW.state IN ('validating', 'failed'))
        OR (OLD.state = 'validating' AND NEW.state IN ('succeeded', 'failed'))
        OR (OLD.state = 'succeeded' AND NEW.state = 'superseded')
    );
    SELECT RAISE(ABORT, 'omnivia: projection validation start is immutable')
    WHERE OLD.validation_started_at_us IS NOT NULL
      AND NEW.validation_started_at_us IS NOT OLD.validation_started_at_us;
    SELECT RAISE(ABORT, 'omnivia: projection supersession may change only state')
    WHERE OLD.state = 'succeeded'
      AND (NEW.validation_started_at_us IS NOT OLD.validation_started_at_us
           OR NEW.finished_at_us IS NOT OLD.finished_at_us
           OR NEW.input_record_count IS NOT OLD.input_record_count
           OR NEW.output_record_count IS NOT OLD.output_record_count
           OR NEW.build_digest IS NOT OLD.build_digest
           OR NEW.error_json IS NOT OLD.error_json);
    SELECT RAISE(ABORT, 'omnivia: validation start must follow run evidence')
    WHERE NEW.state = 'validating'
      AND (
        NEW.validation_started_at_us IS NULL
        OR NEW.validation_started_at_us < COALESCE((
            SELECT MAX(created_at_us) FROM omnivia_projection_run_checkpoints
            WHERE workspace_id = OLD.workspace_id
              AND projection_id = OLD.projection_id AND run_id = OLD.run_id
        ), OLD.started_at_us)
        OR NEW.validation_started_at_us < COALESCE((
            SELECT MAX(occurred_at_us) FROM omnivia_projection_record_failures
            WHERE workspace_id = OLD.workspace_id
              AND projection_id = OLD.projection_id AND run_id = OLD.run_id
        ), OLD.started_at_us)
      );
    SELECT RAISE(ABORT, 'omnivia: succeeded projection requires accepted matching validation')
    WHERE NEW.state = 'succeeded'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_projection_validations v
        WHERE v.workspace_id = OLD.workspace_id
          AND v.projection_id = OLD.projection_id AND v.run_id = OLD.run_id
          AND v.accepted = 1
          AND v.input_record_count = NEW.input_record_count
          AND v.output_record_count = NEW.output_record_count
          AND v.build_digest = NEW.build_digest
          AND v.validated_at_us >= NEW.validation_started_at_us
          AND NEW.finished_at_us >= v.validated_at_us
          AND v.validated_at_us >= COALESCE((
            SELECT MAX(created_at_us) FROM omnivia_projection_run_checkpoints
            WHERE workspace_id = OLD.workspace_id
              AND projection_id = OLD.projection_id AND run_id = OLD.run_id
          ), OLD.started_at_us)
          AND v.validated_at_us >= COALESCE((
            SELECT MAX(occurred_at_us) FROM omnivia_projection_record_failures
            WHERE workspace_id = OLD.workspace_id
              AND projection_id = OLD.projection_id AND run_id = OLD.run_id
          ), OLD.started_at_us)
      );
    SELECT RAISE(ABORT, 'omnivia: failed projection requires sanitized canonical error JSON')
    WHERE NEW.state = 'failed'
      AND (NEW.error_json IS NULL OR json_valid(NEW.error_json) IS NOT 1
           OR json(NEW.error_json) <> NEW.error_json);
    SELECT RAISE(ABORT, 'omnivia: failed projection closeout precedes validation decision')
    WHERE NEW.state = 'failed'
      AND EXISTS (
        SELECT 1 FROM omnivia_projection_validations v
        WHERE v.workspace_id = OLD.workspace_id
          AND v.projection_id = OLD.projection_id AND v.run_id = OLD.run_id
          AND NEW.finished_at_us < v.validated_at_us
      );
    SELECT RAISE(ABORT, 'omnivia: projection supersession requires later activation')
    WHERE NEW.state = 'superseded'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_projection_activations a
        WHERE a.workspace_id = OLD.workspace_id
          AND a.projection_id = OLD.projection_id
          AND a.previous_run_id = OLD.run_id
          AND a.activated_at_us >= OLD.finished_at_us
      );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_runs_delete
BEFORE DELETE ON omnivia_projection_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_projection_runs is append-preserved; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_run_checkpoints_insert
BEFORE INSERT ON omnivia_projection_run_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_projection_run_checkpoints')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
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
    SELECT RAISE(ABORT, 'omnivia: checkpoint requires running or validating projection')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_runs
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id AND state IN ('running', 'validating')
          AND source_checkpoint = NEW.source_checkpoint
          AND NEW.created_at_us >= started_at_us);
    SELECT RAISE(ABORT, 'omnivia: validation closes projection checkpoint evidence')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_projection_validations
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: projection checkpoint sequence must be contiguous')
    WHERE NEW.checkpoint_sequence IS NOT (
        SELECT COALESCE(MAX(checkpoint_sequence), -1) + 1
        FROM omnivia_projection_run_checkpoints
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: projection checkpoint regressed')
    WHERE NEW.checkpoint_sequence > 0 AND NOT EXISTS (
        SELECT 1 FROM omnivia_projection_run_checkpoints
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id
          AND checkpoint_sequence = NEW.checkpoint_sequence - 1
          AND created_at_us <= NEW.created_at_us
          AND input_record_count <= NEW.input_record_count
          AND output_record_count <= NEW.output_record_count);
    SELECT RAISE(ABORT, 'omnivia: projection cursor must be canonical JSON')
    WHERE json_valid(NEW.cursor_json) IS NOT 1 OR json(NEW.cursor_json) <> NEW.cursor_json;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_run_checkpoints_update
BEFORE UPDATE ON omnivia_projection_run_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_projection_run_checkpoints is append-only; UPDATE is never permitted');
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_run_checkpoints_delete
BEFORE DELETE ON omnivia_projection_run_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_projection_run_checkpoints is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_record_failures_insert
BEFORE INSERT ON omnivia_projection_record_failures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_projection_record_failures')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
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
    SELECT RAISE(ABORT, 'omnivia: record failure requires running or validating projection')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_runs
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id AND state IN ('running', 'validating')
          AND NEW.occurred_at_us >= started_at_us);
    SELECT RAISE(ABORT, 'omnivia: validation closes projection failure evidence')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_projection_validations
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: projection failure sequence must be contiguous')
    WHERE NEW.failure_sequence IS NOT (
        SELECT COALESCE(MAX(failure_sequence), -1) + 1
        FROM omnivia_projection_record_failures
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: projection failure time must not regress')
    WHERE NEW.failure_sequence > 0
      AND NEW.occurred_at_us < (
        SELECT occurred_at_us FROM omnivia_projection_record_failures
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id
          AND failure_sequence = NEW.failure_sequence - 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_record_failures_update
BEFORE UPDATE ON omnivia_projection_record_failures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_projection_record_failures is append-only; UPDATE is never permitted');
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_record_failures_delete
BEFORE DELETE ON omnivia_projection_record_failures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_projection_record_failures is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_validations_insert
BEFORE INSERT ON omnivia_projection_validations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_projection_validations')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
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
    SELECT RAISE(ABORT, 'omnivia: validation requires validating projection and correlated time')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_runs r
        WHERE r.workspace_id = NEW.workspace_id AND r.projection_id = NEW.projection_id
          AND r.run_id = NEW.run_id AND r.state = 'validating'
          AND NEW.validated_at_us >= r.validation_started_at_us
          AND NEW.validated_at_us >= COALESCE((
            SELECT MAX(created_at_us) FROM omnivia_projection_run_checkpoints
            WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
              AND run_id = NEW.run_id), r.started_at_us)
          AND NEW.validated_at_us >= COALESCE((
            SELECT MAX(occurred_at_us) FROM omnivia_projection_record_failures
            WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
              AND run_id = NEW.run_id), r.started_at_us));
    SELECT RAISE(ABORT, 'omnivia: validation report must be canonical JSON')
    WHERE json_valid(NEW.report_json) IS NOT 1 OR json(NEW.report_json) <> NEW.report_json;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_validations_update
BEFORE UPDATE ON omnivia_projection_validations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_projection_validations is append-only; UPDATE is never permitted');
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_validations_delete
BEFORE DELETE ON omnivia_projection_validations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_projection_validations is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_activations_insert
BEFORE INSERT ON omnivia_projection_activations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_projection_activations')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
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
    SELECT RAISE(ABORT, 'omnivia: activation sequence must be contiguous')
    WHERE NEW.activation_sequence IS NOT (
        SELECT COALESCE(MAX(activation_sequence), -1) + 1
        FROM omnivia_projection_activations
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id);
    SELECT RAISE(ABORT, 'omnivia: activation predecessor does not match active pointer')
    WHERE NEW.previous_run_id IS NOT (
        SELECT active_run_id FROM omnivia_projection_ledger
        WHERE projection_id = NEW.projection_id);
    SELECT RAISE(ABORT, 'omnivia: activation must advance time and epoch')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_projection_ledger
        WHERE projection_id = NEW.projection_id
          AND ((active_epoch IS NOT NULL AND NEW.target_epoch <= active_epoch)
               OR (activated_at_us IS NOT NULL AND NEW.activated_at_us < activated_at_us)));
    SELECT RAISE(ABORT, 'omnivia: activation requires succeeded run and accepted matching validation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_runs r
        JOIN omnivia_projection_validations v
          ON v.workspace_id = r.workspace_id AND v.projection_id = r.projection_id
         AND v.run_id = r.run_id
        WHERE r.workspace_id = NEW.workspace_id AND r.projection_id = NEW.projection_id
          AND r.run_id = NEW.run_id AND r.state = 'succeeded' AND v.accepted = 1
          AND r.target_epoch = NEW.target_epoch
          AND r.source_checkpoint = NEW.source_checkpoint
          AND r.build_digest = NEW.build_digest
          AND v.build_digest = NEW.build_digest
          AND v.validation_digest = NEW.validation_digest
          AND v.validated_at_us >= r.validation_started_at_us
          AND r.finished_at_us >= v.validated_at_us
          AND r.input_record_count = v.input_record_count
          AND r.output_record_count = v.output_record_count
          AND v.validated_at_us >= COALESCE((
            SELECT MAX(created_at_us) FROM omnivia_projection_run_checkpoints
            WHERE workspace_id = r.workspace_id
              AND projection_id = r.projection_id AND run_id = r.run_id
          ), r.started_at_us)
          AND v.validated_at_us >= COALESCE((
            SELECT MAX(occurred_at_us) FROM omnivia_projection_record_failures
            WHERE workspace_id = r.workspace_id
              AND projection_id = r.projection_id AND run_id = r.run_id
          ), r.started_at_us)
          AND NEW.activated_at_us >= r.finished_at_us
          AND NEW.activated_at_us >= v.validated_at_us);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_apply_projection_activation
AFTER INSERT ON omnivia_projection_activations
BEGIN
    SELECT 'paired BEFORE INSERT ON omnivia_projection_activations guard' WHERE 0;
    UPDATE omnivia_projection_runs
       SET state = 'superseded'
     WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
       AND run_id = NEW.previous_run_id AND state = 'succeeded';
    SELECT RAISE(ABORT, 'omnivia: activation predecessor was not superseded')
    WHERE NEW.previous_run_id IS NOT NULL AND changes() <> 1;
    UPDATE omnivia_projection_ledger
       SET projection_kind = (
               SELECT projection_kind FROM omnivia_projection_runs
               WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
                 AND run_id = NEW.run_id),
           schema_version = (
               SELECT schema_version FROM omnivia_projection_runs
               WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
                 AND run_id = NEW.run_id),
           profile_version = (
               SELECT profile_version FROM omnivia_projection_runs
               WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
                 AND run_id = NEW.run_id),
           active_run_id = NEW.run_id,
           active_epoch = NEW.target_epoch,
           active_source_checkpoint = NEW.source_checkpoint,
           active_build_digest = NEW.build_digest,
           active_validation_digest = NEW.validation_digest,
           activated_at_us = NEW.activated_at_us
     WHERE projection_id = NEW.projection_id;
    SELECT RAISE(ABORT, 'omnivia: activation pointer was not updated')
    WHERE changes() <> 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_activations_update
BEFORE UPDATE ON omnivia_projection_activations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_projection_activations is append-only; UPDATE is never permitted');
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_activations_delete
BEFORE DELETE ON omnivia_projection_activations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_projection_activations is append-only; DELETE is never permitted');
END;

-- The activation row is the only authority for changing an established M5
-- pointer.  The AFTER INSERT trigger above can satisfy this proof because the
-- row already exists within the same SQLite statement transaction.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_ledger_active_pointer_update
BEFORE UPDATE ON omnivia_projection_ledger
WHEN NEW.projection_kind IS NOT OLD.projection_kind
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.profile_version IS NOT OLD.profile_version
  OR NEW.active_run_id IS NOT OLD.active_run_id
  OR NEW.active_epoch IS NOT OLD.active_epoch
  OR NEW.active_source_checkpoint IS NOT OLD.active_source_checkpoint
  OR NEW.active_build_digest IS NOT OLD.active_build_digest
  OR NEW.active_validation_digest IS NOT OLD.active_validation_digest
  OR NEW.activated_at_us IS NOT OLD.activated_at_us
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on projection active pointer')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1
            FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1
              AND g.workspace_id = s.workspace_id
              AND g.fencing_generation = s.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       );
    SELECT RAISE(ABORT, 'omnivia: projection active pointer requires matching activation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_activations a
        JOIN omnivia_projection_runs r
          ON r.workspace_id = a.workspace_id AND r.projection_id = a.projection_id
         AND r.run_id = a.run_id
        WHERE a.projection_id = NEW.projection_id
          AND a.workspace_id = (
              SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
          )
          AND a.run_id = NEW.active_run_id
          AND a.target_epoch = NEW.active_epoch
          AND a.source_checkpoint = NEW.active_source_checkpoint
          AND a.build_digest = NEW.active_build_digest
          AND a.validation_digest = NEW.active_validation_digest
          AND a.activated_at_us = NEW.activated_at_us
          AND r.projection_kind = NEW.projection_kind
          AND r.schema_version = NEW.schema_version
          AND r.profile_version = NEW.profile_version
          AND a.activation_sequence = (
              SELECT MAX(activation_sequence)
              FROM omnivia_projection_activations
              WHERE workspace_id = a.workspace_id
                AND projection_id = a.projection_id));
END;
