-- Durable decision evaluation records (ADR-042, plan PR-3; spec §14, §24).
--
-- Additive only, and read together with migrations 0044 (settings) and 0045
-- (definitions). The families:
--
--   omnivia_decision_evaluations   one admission: request identity, digest,
--                                  definition fence, lifecycle. The unique
--                                  (workspace, principal, operation,
--                                  idempotency key) pair is the spec §15.1
--                                  identity; the canonical request digest is
--                                  bound to it at write time, so replaying a
--                                  key with a different request is a conflict
--                                  at both the mutation coordinator and here.
--   omnivia_decision_attempts      one execution attempt: route, provider and
--                                  policy generation, timing, forward-pass
--                                  counters, terminal status. Unique per
--                                  (evaluation, attempt number), contiguous
--                                  from 1 by the writer's convention.
--   omnivia_decision_results       at most one terminal result envelope per
--                                  evaluation (UNIQUE), carrying the typed
--                                  prediction, disposition, quality and
--                                  execution facts plus the request/input
--                                  digests that bind it to what was asked.
--   omnivia_decision_outcomes      append-only corrections and observed
--                                  outcomes; an outcome may supersede exactly
--                                  one earlier outcome of its own evaluation.
--   omnivia_decision_subscriptions versioned opt-in settings with a
--                                  transactional cursor and bounded budgets.
--   omnivia_decision_outbox        the transactional event log, shaped like the
--                                  semantic outbox of migration 0037; the
--                                  terminal evaluation write and its event are
--                                  committed in one transaction (§14.5).
--
-- Lifecycle discipline: admission facts on evaluations and attempts are
-- immutable once written; only the lifecycle columns may settle, only forwards
-- in time, and a terminal state can never be revised -- which is what makes an
-- abstention or failure a durable product outcome rather than a retriable
-- error. Every row names the application audit event of the mutation that wrote
-- it, so retention follows the existing audit classes.

CREATE TABLE IF NOT EXISTS omnivia_decision_evaluations (
    workspace_id           TEXT    NOT NULL,
    evaluation_id          TEXT    NOT NULL,
    principal_id           TEXT    NOT NULL,
    operation              TEXT    NOT NULL,
    idempotency_key        TEXT    NOT NULL,
    request_digest         TEXT    NOT NULL,
    definition_id          TEXT    NOT NULL,
    definition_version     TEXT    NOT NULL,
    definition_digest      TEXT    NOT NULL,
    status                 TEXT    NOT NULL,
    mode                   TEXT    NOT NULL,
    subject_refs_json      TEXT    NOT NULL,
    source_snapshot_json   TEXT    NOT NULL,
    created_at_us          INTEGER NOT NULL,
    terminal_at_us         INTEGER,
    abstention_reasons_json TEXT,
    job_id                 TEXT    NOT NULL,
    audit_ref              TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, evaluation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(evaluation_id) = 'text' AND length(evaluation_id) BETWEEN 1 AND 128
           AND evaluation_id GLOB '[A-Za-z0-9]*'
           AND evaluation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(evaluation_id, char(0)) = 0),
    CHECK (typeof(job_id) = 'text' AND length(job_id) BETWEEN 1 AND 128
           AND job_id GLOB '[A-Za-z0-9]*'
           AND job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(job_id, char(0)) = 0),
    CHECK (typeof(principal_id) = 'text'
           AND length(principal_id) BETWEEN 1 AND 128),
    CHECK (typeof(operation) = 'text' AND operation = 'decision.evaluate'),
    CHECK (typeof(idempotency_key) = 'text'
           AND length(idempotency_key) BETWEEN 1 AND 256),
    CHECK (typeof(request_digest) = 'text' AND length(request_digest) = 71
           AND substr(request_digest, 1, 7) = 'sha256:'
           AND substr(request_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (definition_digest GLOB 'sha256:*'),
    CHECK (status IN ('pending', 'running', 'succeeded', 'abstained',
                      'failed', 'cancelled')),
    CHECK (mode IN ('advisory')),
    CHECK (typeof(subject_refs_json) = 'text'
           AND length(CAST(subject_refs_json AS BLOB)) BETWEEN 2 AND 262144
           AND json_valid(subject_refs_json) = 1
           AND json(subject_refs_json) = subject_refs_json),
    CHECK (typeof(source_snapshot_json) = 'text'
           AND length(CAST(source_snapshot_json AS BLOB)) BETWEEN 2 AND 262144
           AND json_valid(source_snapshot_json) = 1
           AND json(source_snapshot_json) = source_snapshot_json),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (terminal_at_us IS NULL
           OR (typeof(terminal_at_us) = 'integer' AND terminal_at_us > 0)),
    CHECK (abstention_reasons_json IS NULL
           OR (typeof(abstention_reasons_json) = 'text'
               AND length(CAST(abstention_reasons_json AS BLOB)) BETWEEN 2 AND 65536
               AND json_valid(abstention_reasons_json) = 1
               AND json(abstention_reasons_json) = abstention_reasons_json)),
    CHECK (typeof(job_id) = 'text' AND length(job_id) BETWEEN 1 AND 128),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK ((status IN ('pending', 'running')) = (terminal_at_us IS NULL)),

    UNIQUE (workspace_id, principal_id, operation, idempotency_key),

    FOREIGN KEY (workspace_id, definition_id, definition_version)
        REFERENCES omnivia_decision_definition_versions (
            workspace_id, definition_id, version),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_decision_attempts (
    workspace_id      TEXT    NOT NULL,
    attempt_id        TEXT    NOT NULL,
    evaluation_id     TEXT    NOT NULL,
    attempt_number    INTEGER NOT NULL,
    route             TEXT    NOT NULL,
    provider_id       TEXT    NOT NULL,
    profile_id        TEXT    NOT NULL,
    policy_generation TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    started_at_us     INTEGER NOT NULL,
    finished_at_us    INTEGER,
    duration_us       INTEGER,
    forward_passes    INTEGER NOT NULL,
    failure_code      TEXT,
    diagnostics_json  TEXT,
    audit_ref         TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, attempt_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(attempt_id) = 'text' AND length(attempt_id) BETWEEN 1 AND 128
           AND attempt_id GLOB '[A-Za-z0-9]*'
           AND attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(attempt_id, char(0)) = 0),
    CHECK (typeof(attempt_number) = 'integer' AND attempt_number > 0),
    CHECK (route IN ('deterministic', 'local_model', 'remote_model')),
    CHECK (typeof(provider_id) = 'text' AND length(provider_id) BETWEEN 1 AND 128
           AND provider_id GLOB '[A-Za-z0-9]*'
           AND provider_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_id, char(0)) = 0),
    CHECK (typeof(profile_id) = 'text' AND length(profile_id) BETWEEN 1 AND 128
           AND profile_id GLOB '[A-Za-z0-9]*'
           AND profile_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(profile_id, char(0)) = 0),
    CHECK (typeof(policy_generation) = 'text'
           AND length(policy_generation) BETWEEN 1 AND 128),
    CHECK (status IN ('claimed', 'succeeded', 'failed', 'discarded', 'timeout')),
    CHECK (typeof(started_at_us) = 'integer' AND started_at_us > 0),
    CHECK (finished_at_us IS NULL
           OR (typeof(finished_at_us) = 'integer' AND finished_at_us > 0)),
    CHECK (duration_us IS NULL OR (typeof(duration_us) = 'integer'
           AND duration_us >= 0)),
    CHECK (typeof(forward_passes) = 'integer' AND forward_passes >= 0),
    CHECK (failure_code IS NULL OR (typeof(failure_code) = 'text'
           AND length(failure_code) BETWEEN 1 AND 128
           AND failure_code GLOB '[A-Za-z0-9]*'
           AND failure_code NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(failure_code, char(0)) = 0)),
    CHECK (diagnostics_json IS NULL
           OR (typeof(diagnostics_json) = 'text'
               AND length(CAST(diagnostics_json AS BLOB)) BETWEEN 2 AND 65536
               AND json_valid(diagnostics_json) = 1
               AND json(diagnostics_json) = diagnostics_json)),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK ((status IN ('claimed')) = (finished_at_us IS NULL)),
    CHECK (finished_at_us IS NULL OR duration_us IS NOT NULL),

    UNIQUE (workspace_id, evaluation_id, attempt_number),

    FOREIGN KEY (workspace_id, evaluation_id)
        REFERENCES omnivia_decision_evaluations (workspace_id, evaluation_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_decision_results (
    workspace_id        TEXT    NOT NULL,
    result_id           TEXT    NOT NULL,
    evaluation_id       TEXT    NOT NULL,
    schema_version      TEXT    NOT NULL,
    status              TEXT    NOT NULL,
    prediction_json     TEXT,
    disposition_json    TEXT    NOT NULL,
    quality_json        TEXT    NOT NULL,
    execution_json      TEXT    NOT NULL,
    abstention_reasons_json TEXT,
    input_digest        TEXT    NOT NULL,
    created_at_us       INTEGER NOT NULL,
    audit_ref           TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, result_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(result_id) = 'text' AND length(result_id) BETWEEN 1 AND 128
           AND result_id GLOB '[A-Za-z0-9]*'
           AND result_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(result_id, char(0)) = 0),
    CHECK (schema_version = 'decision.1'),
    CHECK (status IN ('succeeded', 'abstained', 'failed')),
    CHECK (prediction_json IS NULL
           OR (typeof(prediction_json) = 'text'
               AND length(CAST(prediction_json AS BLOB)) BETWEEN 2 AND 262144
               AND json_valid(prediction_json) = 1
               AND json(prediction_json) = prediction_json)),
    CHECK (typeof(disposition_json) = 'text'
           AND length(CAST(disposition_json AS BLOB)) BETWEEN 2 AND 65536
           AND json_valid(disposition_json) = 1
           AND json(disposition_json) = disposition_json),
    CHECK (typeof(quality_json) = 'text'
           AND length(CAST(quality_json AS BLOB)) BETWEEN 2 AND 65536
           AND json_valid(quality_json) = 1 AND json(quality_json) = quality_json),
    CHECK (typeof(execution_json) = 'text'
           AND length(CAST(execution_json AS BLOB)) BETWEEN 2 AND 65536
           AND json_valid(execution_json) = 1 AND json(execution_json) = execution_json),
    CHECK (abstention_reasons_json IS NULL
           OR (typeof(abstention_reasons_json) = 'text'
               AND length(CAST(abstention_reasons_json AS BLOB)) BETWEEN 2 AND 65536
               AND json_valid(abstention_reasons_json) = 1
               AND json(abstention_reasons_json) = abstention_reasons_json)),
    CHECK (typeof(input_digest) = 'text' AND length(input_digest) = 71
           AND substr(input_digest, 1, 7) = 'sha256:'
           AND substr(input_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK ((status = 'succeeded') = (prediction_json IS NOT NULL)),
    CHECK ((status = 'abstained') = (abstention_reasons_json IS NOT NULL)),

    UNIQUE (workspace_id, evaluation_id),

    FOREIGN KEY (workspace_id, evaluation_id)
        REFERENCES omnivia_decision_evaluations (workspace_id, evaluation_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_decision_outcomes (
    workspace_id          TEXT    NOT NULL,
    outcome_id            TEXT    NOT NULL,
    evaluation_id         TEXT    NOT NULL,
    outcome               TEXT    NOT NULL,
    corrected_option_id   TEXT,
    note                  TEXT,
    evidence_json         TEXT    NOT NULL,
    actor_id              TEXT    NOT NULL,
    event_at_us           INTEGER NOT NULL,
    recorded_at_us        INTEGER NOT NULL,
    superseded_outcome_id TEXT,
    audit_ref             TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, outcome_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(outcome_id) = 'text' AND length(outcome_id) BETWEEN 1 AND 128
           AND outcome_id GLOB '[A-Za-z0-9]*'
           AND outcome_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(outcome_id, char(0)) = 0),
    CHECK (outcome IN ('confirmed', 'corrected', 'rejected', 'preference')),
    CHECK (corrected_option_id IS NULL
           OR (typeof(corrected_option_id) = 'text'
               AND length(corrected_option_id) BETWEEN 1 AND 128
               AND corrected_option_id GLOB '[A-Za-z0-9]*'
               AND corrected_option_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(corrected_option_id, char(0)) = 0)),
    CHECK (note IS NULL OR (typeof(note) = 'text'
           AND length(note) BETWEEN 1 AND 4096)),
    CHECK (typeof(evidence_json) = 'text'
           AND length(CAST(evidence_json AS BLOB)) BETWEEN 2 AND 1048576
           AND json_valid(evidence_json) = 1 AND json(evidence_json) = evidence_json),
    CHECK (typeof(actor_id) = 'text' AND length(actor_id) BETWEEN 1 AND 128),
    CHECK (typeof(event_at_us) = 'integer' AND event_at_us > 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (superseded_outcome_id IS NULL
           OR (typeof(superseded_outcome_id) = 'text'
               AND length(superseded_outcome_id) BETWEEN 1 AND 128
               AND superseded_outcome_id GLOB '[A-Za-z0-9]*'
               AND superseded_outcome_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(superseded_outcome_id, char(0)) = 0)),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, evaluation_id)
        REFERENCES omnivia_decision_evaluations (workspace_id, evaluation_id),
    FOREIGN KEY (workspace_id, superseded_outcome_id)
        REFERENCES omnivia_decision_outcomes (workspace_id, outcome_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_decision_subscriptions (
    workspace_id      TEXT    NOT NULL,
    subscription_id   TEXT    NOT NULL,
    definition_id     TEXT    NOT NULL,
    definition_version TEXT   NOT NULL,
    enabled           INTEGER NOT NULL,
    cursor            TEXT,
    daily_limit       INTEGER NOT NULL,
    rate_limit        INTEGER NOT NULL,
    updated_at_us     INTEGER NOT NULL,
    audit_ref         TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, subscription_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(subscription_id) = 'text' AND length(subscription_id) BETWEEN 1 AND 128
           AND subscription_id GLOB '[A-Za-z0-9]*'
           AND subscription_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(subscription_id, char(0)) = 0),
    CHECK (enabled IN (0, 1)),
    CHECK (cursor IS NULL OR (typeof(cursor) = 'text'
           AND length(cursor) BETWEEN 1 AND 256)),
    CHECK (typeof(daily_limit) = 'integer' AND daily_limit > 0),
    CHECK (typeof(rate_limit) = 'integer' AND rate_limit > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, definition_id, definition_version)
        REFERENCES omnivia_decision_definition_versions (
            workspace_id, definition_id, version),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_decision_outbox (
    workspace_id   TEXT    NOT NULL,
    aggregate_id   TEXT    NOT NULL,
    sequence       INTEGER NOT NULL,
    outbox_id      TEXT    NOT NULL,
    event_kind     TEXT    NOT NULL,
    payload_json   TEXT    NOT NULL,
    payload_digest TEXT    NOT NULL,
    created_at_us  INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, aggregate_id, sequence),
    UNIQUE (workspace_id, outbox_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(aggregate_id) = 'text' AND length(aggregate_id) BETWEEN 1 AND 128
           AND aggregate_id GLOB '[A-Za-z0-9]*'
           AND aggregate_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(aggregate_id, char(0)) = 0),
    CHECK (typeof(outbox_id) = 'text' AND length(outbox_id) BETWEEN 1 AND 128
           AND outbox_id GLOB '[A-Za-z0-9]*'
           AND outbox_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(outbox_id, char(0)) = 0),
    CHECK (typeof(sequence) = 'integer' AND sequence >= 0),
    CHECK (event_kind IN ('decision.completed.v1', 'decision.abstained.v1',
                          'decision.failed.v1', 'decision.outcome_recorded.v1',
                          'decision.qualification_revoked.v1')),
    CHECK (typeof(payload_json) = 'text'
           AND length(CAST(payload_json AS BLOB)) BETWEEN 2 AND 1048576
           AND json_valid(payload_json) = 1 AND json(payload_json) = payload_json),
    CHECK (typeof(payload_digest) = 'text' AND length(payload_digest) = 71
           AND substr(payload_digest, 1, 7) = 'sha256:'
           AND substr(payload_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0)
) WITHOUT ROWID;


CREATE INDEX IF NOT EXISTS omnivia_idx_decision_evaluations_created
    ON omnivia_decision_evaluations (workspace_id, created_at_us, evaluation_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_decision_evaluations_subject
    ON omnivia_decision_evaluations (workspace_id, definition_id, status);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_evaluations_insert
BEFORE INSERT ON omnivia_decision_evaluations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_decision_evaluations')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_evaluations_update
BEFORE UPDATE ON omnivia_decision_evaluations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_decision_evaluations')
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
    SELECT RAISE(ABORT, 'omnivia: decision evaluation admission facts are immutable; only the lifecycle may settle')
    WHERE NEW.principal_id IS NOT OLD.principal_id
       OR NEW.operation IS NOT OLD.operation
       OR NEW.idempotency_key IS NOT OLD.idempotency_key
       OR NEW.request_digest IS NOT OLD.request_digest
       OR NEW.definition_id IS NOT OLD.definition_id
       OR NEW.definition_version IS NOT OLD.definition_version
       OR NEW.definition_digest IS NOT OLD.definition_digest
       OR NEW.mode IS NOT OLD.mode
       OR NEW.subject_refs_json IS NOT OLD.subject_refs_json
       OR NEW.source_snapshot_json IS NOT OLD.source_snapshot_json
       OR NEW.created_at_us IS NOT OLD.created_at_us
       OR NEW.job_id IS NOT OLD.job_id
       OR NEW.audit_ref IS NOT OLD.audit_ref
       OR NEW.terminal_at_us < OLD.terminal_at_us
       OR (OLD.terminal_at_us IS NOT NULL
           AND (NEW.status IS NOT OLD.status
                OR NEW.terminal_at_us IS NOT OLD.terminal_at_us
                OR NEW.abstention_reasons_json IS NOT OLD.abstention_reasons_json));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_evaluations_delete
BEFORE DELETE ON omnivia_decision_evaluations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_evaluations is append-only; DELETE is never permitted');
END;


CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_attempts_insert
BEFORE INSERT ON omnivia_decision_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_decision_attempts')
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
    SELECT RAISE(ABORT, 'omnivia: a decision attempt number must be contiguous')
    WHERE NEW.attempt_number IS NOT (
        SELECT COALESCE(MAX(attempt_number), 0) + 1
        FROM omnivia_decision_attempts
        WHERE workspace_id = NEW.workspace_id
          AND evaluation_id = NEW.evaluation_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_attempts_update
BEFORE UPDATE ON omnivia_decision_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_decision_attempts')
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
    SELECT RAISE(ABORT, 'omnivia: decision attempt identity is immutable; only the outcome may settle')
    WHERE NEW.evaluation_id IS NOT OLD.evaluation_id
       OR NEW.attempt_number IS NOT OLD.attempt_number
       OR NEW.route IS NOT OLD.route
       OR NEW.provider_id IS NOT OLD.provider_id
       OR NEW.profile_id IS NOT OLD.profile_id
       OR NEW.policy_generation IS NOT OLD.policy_generation
       OR NEW.started_at_us IS NOT OLD.started_at_us
       OR NEW.forward_passes < OLD.forward_passes
       OR NEW.audit_ref IS NOT OLD.audit_ref
       OR (OLD.finished_at_us IS NOT NULL
           AND (NEW.status IS NOT OLD.status
                OR NEW.finished_at_us IS NOT OLD.finished_at_us
                OR NEW.duration_us IS NOT OLD.duration_us
                OR NEW.failure_code IS NOT OLD.failure_code
                OR NEW.diagnostics_json IS NOT OLD.diagnostics_json));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_attempts_delete
BEFORE DELETE ON omnivia_decision_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_attempts is append-only; DELETE is never permitted');
END;


CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_results_insert
BEFORE INSERT ON omnivia_decision_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_decision_results')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_results_update
BEFORE UPDATE ON omnivia_decision_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_decision_results')
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
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_results is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_results_delete
BEFORE DELETE ON omnivia_decision_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_results is append-only; DELETE is never permitted');
END;


CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_outcomes_insert
BEFORE INSERT ON omnivia_decision_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_decision_outcomes')
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
    SELECT RAISE(ABORT, 'omnivia: a superseded outcome reference must name an earlier outcome of its own evaluation')
    WHERE NEW.superseded_outcome_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_decision_outcomes o
        WHERE o.workspace_id = NEW.workspace_id
          AND o.outcome_id = NEW.superseded_outcome_id
          AND o.evaluation_id = NEW.evaluation_id
          AND o.recorded_at_us <= NEW.recorded_at_us);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_outcomes_update
BEFORE UPDATE ON omnivia_decision_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_decision_outcomes')
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
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_outcomes is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_outcomes_delete
BEFORE DELETE ON omnivia_decision_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_outcomes is append-only; DELETE is never permitted');
END;


CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_subscriptions_insert
BEFORE INSERT ON omnivia_decision_subscriptions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_decision_subscriptions')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_subscriptions_update
BEFORE UPDATE ON omnivia_decision_subscriptions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_decision_subscriptions')
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
    SELECT RAISE(ABORT, 'omnivia: decision subscription identity is immutable; only settings may change')
    WHERE NEW.definition_id IS NOT OLD.definition_id
       OR NEW.definition_version IS NOT OLD.definition_version
       OR (NEW.updated_at_us < OLD.updated_at_us);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_subscriptions_delete
BEFORE DELETE ON omnivia_decision_subscriptions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_subscriptions is append-only; DELETE is never permitted');
END;


CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_outbox_insert
BEFORE INSERT ON omnivia_decision_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_decision_outbox')
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
    SELECT RAISE(ABORT, 'omnivia: a decision outbox sequence must be contiguous')
    WHERE NEW.sequence IS NOT (
        SELECT COALESCE(MAX(sequence), 0) + 1
        FROM omnivia_decision_outbox
        WHERE workspace_id = NEW.workspace_id
          AND aggregate_id = NEW.aggregate_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_outbox_update
BEFORE UPDATE ON omnivia_decision_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_decision_outbox')
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
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_outbox is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_outbox_delete
BEFORE DELETE ON omnivia_decision_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_outbox is append-only; DELETE is never permitted');
END;
