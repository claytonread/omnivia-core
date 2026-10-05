-- Runtime-owned completion decisions for final run settlement (DEV-REQ-137, Agent Runtime).
--
-- Additive only; allocation 0066 (Agent Runtime, predecessor 0065). One append-only table, the
-- unique parents its lineage keys reference, and the guard triggers below. A row is the Runtime's own
-- accepted decision that one run's final step may terminalize: the exact accepted criteria it proved,
-- and the raw evidence identity each criterion rests on. Nothing else can make a run succeed.
--
-- Lineage. The row names its workspace, run, durable job, run step, runtime attempt and application
-- attempt, and each is a foreign key to the canonical record it names, so a decision for a missing or
-- foreign record is refused by the database. The application attempt's terminal observation and the
-- run's succeeded event are DEFERRABLE INITIALLY DEFERRED foreign keys: a decision that is not closed
-- by both fails at COMMIT, and the fenced transaction rolls back everything it wrote. The insert
-- trigger adds what a key cannot say: the lineage is the current one, the final step succeeded at the
-- decision instant, the latest run event is the one that opened this attempt, and the body agrees
-- with every column.
--
-- Closure. A final success is written in exactly one order: the attempt and its step settle, then this
-- decision, then the run's succeeded event, then the application job's terminal observation. The
-- triggers on the run event stream and on the observation table admit only that order and only the
-- exact pairing. The event trigger is scoped to scheduler-owned work: a run whose durable job has any
-- application attempt, which only a claim creates. The scope does not lapse when that attempt is
-- terminalized, so terminalizing first does not open a route around the guard. The decision's
-- deferred key to the observation names the succeeded closure exactly, so a failed or cancelled
-- observation of the same attempt cannot stand in for it. A legacy repository write of a run event
-- on a job the scheduler never claimed is not governed here.
--
-- Identity. `decision_digest` is `sha256:` over the canonical decision body, which excludes the time
-- it was recorded. The insert trigger recomputes it with `omnivia_sha256_hex`, a connection function
-- the service registers, so a stored digest that does not name its body is refused by the database.
-- `(workspace_id, run_id)` is unique, so a run carries at most one decision; an exact replay dedups in
-- the storage layer and a different body is refused there.
--
-- Authority. A run settled by a decision admits no workflow completion record, and a workflow
-- completion record admits no decision, so the two authorities cannot both settle one run.
--
-- Every write runs inside the caller's `fenced_transaction`. The INSERT guard carries the same
-- connection-authority, guard, workspace-state and lease predicate as the other guarded tables.
-- UPDATE and DELETE are refused for the fenced owner too.
--
-- No DML, and no comment sits inside a statement below, for the migrator's statement splitter.

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_runtime_runs_lineage
    ON omnivia_runtime_runs (workspace_id, run_id, job_id);

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_runtime_run_steps_lineage
    ON omnivia_runtime_run_steps (workspace_id, run_id, run_step_id);

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_runtime_attempts_lineage
    ON omnivia_runtime_attempts (workspace_id, run_id, run_step_id, attempt_id);

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_job_terminal_observations_parent
    ON omnivia_job_terminal_observations (workspace_id, job_id, attempt_number);

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_job_terminal_observations_closure
    ON omnivia_job_terminal_observations (workspace_id, job_id, attempt_number, terminal_state);

CREATE TABLE IF NOT EXISTS omnivia_runtime_completion_decisions (
    workspace_id                TEXT    NOT NULL,
    decision_digest             TEXT    NOT NULL,
    run_id                      TEXT    NOT NULL,
    job_id                      TEXT    NOT NULL,
    run_step_id                 TEXT    NOT NULL,
    runtime_attempt_id          TEXT    NOT NULL,
    application_attempt_number  INTEGER NOT NULL,
    closure_state               TEXT    NOT NULL,
    settled_sequence            INTEGER NOT NULL,
    decision                    TEXT    NOT NULL,
    decision_body               TEXT    NOT NULL,
    decided_under_generation    INTEGER NOT NULL,
    decided_at_us               INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(decision_digest) = 'text' AND length(decision_digest) = 71
           AND substr(decision_digest, 1, 7) = 'sha256:'
           AND substr(decision_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(job_id) = 'text' AND length(job_id) BETWEEN 1 AND 128
           AND job_id GLOB '[A-Za-z0-9]*'
           AND job_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(job_id, char(0)) = 0),
    CHECK (typeof(run_step_id) = 'text' AND length(run_step_id) BETWEEN 1 AND 128
           AND run_step_id GLOB '[A-Za-z0-9]*'
           AND run_step_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_step_id, char(0)) = 0),
    CHECK (typeof(runtime_attempt_id) = 'text' AND length(runtime_attempt_id) BETWEEN 1 AND 128
           AND runtime_attempt_id GLOB '[A-Za-z0-9]*'
           AND runtime_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(runtime_attempt_id, char(0)) = 0),
    CHECK (typeof(application_attempt_number) = 'integer'
           AND application_attempt_number BETWEEN 1 AND 256),
    CHECK (closure_state = 'succeeded'),
    CHECK (typeof(settled_sequence) = 'integer' AND settled_sequence BETWEEN 1 AND 999),
    CHECK (decision = 'accepted'),
    CHECK (typeof(decision_body) = 'text' AND length(decision_body) BETWEEN 2 AND 1048576
           AND substr(decision_body, 1, 1) = '{'),
    CHECK (typeof(decided_under_generation) = 'integer'
           AND decided_under_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(decided_at_us) = 'integer' AND decided_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, decision_digest),
    UNIQUE (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_id, job_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id, job_id),
    FOREIGN KEY (workspace_id, run_id, run_step_id)
        REFERENCES omnivia_runtime_run_steps (workspace_id, run_id, run_step_id),
    FOREIGN KEY (workspace_id, run_id, run_step_id, runtime_attempt_id)
        REFERENCES omnivia_runtime_attempts (workspace_id, run_id, run_step_id, attempt_id),
    FOREIGN KEY (workspace_id, job_id, application_attempt_number)
        REFERENCES omnivia_job_attempts (workspace_id, job_id, attempt_number),
    FOREIGN KEY (workspace_id, job_id, application_attempt_number, closure_state)
        REFERENCES omnivia_job_terminal_observations (workspace_id, job_id, attempt_number, terminal_state)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (workspace_id, run_id, settled_sequence)
        REFERENCES omnivia_runtime_events (workspace_id, run_id, sequence)
        DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_completion_decisions_insert
BEFORE INSERT ON omnivia_runtime_completion_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_completion_decisions')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: a completion decision must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a completion decision must bind the current fencing generation')
    WHERE NEW.decided_under_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a completion decision body must be well-formed JSON')
    WHERE json_valid(NEW.decision_body) IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: a completion decision body must agree with its lineage columns')
    WHERE json_extract(NEW.decision_body, '$.workspace_id') IS NOT NEW.workspace_id
       OR json_extract(NEW.decision_body, '$.run_id') IS NOT NEW.run_id
       OR json_extract(NEW.decision_body, '$.job_id') IS NOT NEW.job_id
       OR json_extract(NEW.decision_body, '$.run_step_id') IS NOT NEW.run_step_id
       OR json_extract(NEW.decision_body, '$.runtime_attempt_id') IS NOT NEW.runtime_attempt_id
       OR json_type(NEW.decision_body, '$.application_attempt_number') IS NOT 'integer'
       OR json_extract(NEW.decision_body, '$.application_attempt_number') IS NOT NEW.application_attempt_number
       OR json_type(NEW.decision_body, '$.settled_sequence') IS NOT 'integer'
       OR json_extract(NEW.decision_body, '$.settled_sequence') IS NOT NEW.settled_sequence
       OR json_type(NEW.decision_body, '$.decided_under_generation') IS NOT 'integer'
       OR json_extract(NEW.decision_body, '$.decided_under_generation') IS NOT NEW.decided_under_generation
       OR json_extract(NEW.decision_body, '$.decision') IS NOT NEW.decision;
    SELECT RAISE(ABORT, 'omnivia: a completion decision must name the digest of its own body')
    WHERE NEW.decision_digest IS NOT ('sha256:' || omnivia_sha256_hex(NEW.decision_body));
    SELECT RAISE(ABORT, 'omnivia: a completion decision must settle the next event of its run')
    WHERE NEW.settled_sequence IS NOT (
        SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a completion decision must settle a run whose latest event is its running step')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_events e
        WHERE e.workspace_id = NEW.workspace_id AND e.run_id = NEW.run_id
          AND e.sequence = NEW.settled_sequence - 1
          AND e.run_status = 'running' AND e.run_step_id IS NEW.run_step_id
          AND json_valid(e.details_json) = 1
          AND json_extract(e.details_json, '$.job_id') IS NEW.job_id
          AND json_extract(e.details_json, '$.run_step_id') IS NEW.run_step_id
          AND json_extract(e.details_json, '$.runtime_attempt_id') IS NEW.runtime_attempt_id
          AND json_type(e.details_json, '$.application_attempt_number') = 'integer'
          AND json_extract(e.details_json, '$.application_attempt_number') IS NEW.application_attempt_number);
    SELECT RAISE(ABORT, 'omnivia: a completion decision must name the latest attempt of its step')
    WHERE NEW.runtime_attempt_id IS NOT (
        SELECT attempt_id FROM omnivia_runtime_attempts
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
        ORDER BY attempt_number DESC LIMIT 1);
    SELECT RAISE(ABORT, 'omnivia: a completion decision must rest on an attempt that succeeded at the decision instant')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_attempt_outcomes o
        WHERE o.workspace_id = NEW.workspace_id AND o.attempt_id = NEW.runtime_attempt_id
          AND o.status = 'succeeded' AND o.finished_at_us = NEW.decided_at_us);
    SELECT RAISE(ABORT, 'omnivia: a completion decision must settle the final step of its run')
    WHERE NEW.run_step_id IS NOT (
        SELECT run_step_id FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
        ORDER BY ordinal DESC LIMIT 1);
    SELECT RAISE(ABORT, 'omnivia: a completion decision must rest on a step that succeeded at the decision instant')
    WHERE NOT EXISTS (
        SELECT 1 FROM (
            SELECT status, observed_at_us FROM omnivia_runtime_run_step_states
            WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
            ORDER BY state_sequence DESC LIMIT 1) t
        WHERE t.status = 'succeeded' AND t.observed_at_us = NEW.decided_at_us);
    SELECT RAISE(ABORT, 'omnivia: every step of a decided run must have succeeded or been skipped')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_run_steps s
        WHERE s.workspace_id = NEW.workspace_id AND s.run_id = NEW.run_id
          AND COALESCE((
                SELECT t.status FROM omnivia_runtime_run_step_states t
                WHERE t.workspace_id = s.workspace_id AND t.run_step_id = s.run_step_id
                ORDER BY t.state_sequence DESC LIMIT 1), '')
              NOT IN ('succeeded', 'skipped'));
    SELECT RAISE(ABORT, 'omnivia: a completion decision must name the latest running application attempt of its job')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_job_attempts a
        WHERE a.workspace_id = NEW.workspace_id AND a.job_id = NEW.job_id
          AND a.attempt_number = NEW.application_attempt_number AND a.state = 'running'
          AND NOT EXISTS (
            SELECT 1 FROM omnivia_job_attempts b
            WHERE b.workspace_id = a.workspace_id AND b.job_id = a.job_id
              AND b.attempt_number > a.attempt_number));
    SELECT RAISE(ABORT, 'omnivia: a completion decision must settle a job claimed by the current writer')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs j
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE j.job_id = NEW.job_id AND j.state = 'claimed'
          AND j.fencing_generation = NEW.decided_under_generation
          AND j.claimed_by_service_instance = l.service_instance_id
          AND l.fencing_generation = NEW.decided_under_generation);
    SELECT RAISE(ABORT, 'omnivia: a run settled by a completion decision has no workflow completion record')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_run_completions c
        WHERE c.workspace_id = NEW.workspace_id AND c.run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_completion_decisions_update
BEFORE UPDATE ON omnivia_runtime_completion_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_completion_decisions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_completion_decisions_delete
BEFORE DELETE ON omnivia_runtime_completion_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_completion_decisions is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_events_scheduled_settlement
BEFORE INSERT ON omnivia_runtime_events
WHEN NEW.run_status = 'succeeded' AND EXISTS (
    SELECT 1 FROM omnivia_runtime_runs r
    JOIN omnivia_job_attempts a ON a.workspace_id = r.workspace_id AND a.job_id = r.job_id
    WHERE r.workspace_id = NEW.workspace_id AND r.run_id = NEW.run_id)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: a scheduler-owned succeeded run event must be run_succeeded')
    WHERE NEW.event_kind IS NOT 'run_succeeded';
    SELECT RAISE(ABORT, 'omnivia: a scheduler-owned succeeded run event must carry a well-formed JSON detail')
    WHERE json_valid(NEW.details_json) IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: a succeeded run event must carry the completion decision that settles it')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_completion_decisions d
        WHERE d.workspace_id = NEW.workspace_id AND d.run_id = NEW.run_id
          AND d.settled_sequence = NEW.sequence
          AND d.run_step_id IS NEW.run_step_id
          AND d.decided_at_us = NEW.occurred_at_us
          AND d.decision_digest IS json_extract(NEW.details_json, '$.completion_decision_digest')
          AND d.workspace_id IS json_extract(NEW.details_json, '$.workspace_id')
          AND d.run_id IS json_extract(NEW.details_json, '$.run_id')
          AND d.job_id IS json_extract(NEW.details_json, '$.job_id')
          AND d.run_step_id IS json_extract(NEW.details_json, '$.run_step_id')
          AND d.runtime_attempt_id IS json_extract(NEW.details_json, '$.runtime_attempt_id')
          AND json_type(NEW.details_json, '$.application_attempt_number') = 'integer'
          AND d.application_attempt_number IS json_extract(NEW.details_json, '$.application_attempt_number')
          AND json_type(NEW.details_json, '$.fencing_generation') = 'integer'
          AND d.decided_under_generation IS json_extract(NEW.details_json, '$.fencing_generation'));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_events_decision_sequence
BEFORE INSERT ON omnivia_runtime_events
WHEN NEW.run_status IS NOT 'succeeded'
BEGIN
    SELECT RAISE(ABORT, 'omnivia: the sequence of a completion decision is reserved for its succeeded event')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_completion_decisions d
        WHERE d.workspace_id = NEW.workspace_id AND d.run_id = NEW.run_id
          AND d.settled_sequence = NEW.sequence);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_terminal_observations_settled_run
BEFORE INSERT ON omnivia_job_terminal_observations
WHEN NEW.terminal_state = 'succeeded' AND EXISTS (
    SELECT 1 FROM omnivia_runtime_runs r
    WHERE r.workspace_id = NEW.workspace_id AND r.job_id = NEW.job_id)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: a succeeded runtime-bound job must be closed by its completion decision and run_succeeded event')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_completion_decisions d
        JOIN omnivia_runtime_events e
          ON e.workspace_id = d.workspace_id AND e.run_id = d.run_id
         AND e.sequence = d.settled_sequence
        WHERE d.workspace_id = NEW.workspace_id AND d.job_id = NEW.job_id
          AND d.application_attempt_number IS NEW.attempt_number
          AND d.decided_under_generation IS NEW.fencing_generation
          AND e.event_kind = 'run_succeeded' AND e.run_status = 'succeeded'
          AND e.run_step_id IS d.run_step_id
          AND e.occurred_at_us = d.decided_at_us
          AND e.sequence = (
              SELECT MAX(x.sequence) FROM omnivia_runtime_events x
              WHERE x.workspace_id = d.workspace_id AND x.run_id = d.run_id)
          AND d.decision_digest IS json_extract(e.details_json, '$.completion_decision_digest')
          AND d.workspace_id IS json_extract(e.details_json, '$.workspace_id')
          AND d.run_id IS json_extract(e.details_json, '$.run_id')
          AND d.job_id IS json_extract(e.details_json, '$.job_id')
          AND d.run_step_id IS json_extract(e.details_json, '$.run_step_id')
          AND d.runtime_attempt_id IS json_extract(e.details_json, '$.runtime_attempt_id')
          AND json_type(e.details_json, '$.application_attempt_number') = 'integer'
          AND d.application_attempt_number IS json_extract(e.details_json, '$.application_attempt_number')
          AND json_type(e.details_json, '$.fencing_generation') = 'integer'
          AND d.decided_under_generation IS json_extract(e.details_json, '$.fencing_generation'));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_completions_decided_run
BEFORE INSERT ON omnivia_workflow_run_completions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: a run settled by a completion decision admits no workflow completion record')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_completion_decisions d
        WHERE d.workspace_id = NEW.workspace_id AND d.run_id = NEW.run_id);
END;
