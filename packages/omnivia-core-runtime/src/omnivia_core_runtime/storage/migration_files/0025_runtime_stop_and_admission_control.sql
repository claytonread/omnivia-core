-- Canonical runtime admission and stop-control audit records.
--
-- Additive only. Three append-only tables and nine statement triggers. These tables
-- record private runtime control decisions around the already-canonical run stream; they
-- do not add a public operation, mutate a run, requeue a job, or replace the event stream.
--
--   omnivia_runtime_admission_decisions  one admission/refusal decision for a logical key
--   omnivia_runtime_stop_requests        one request to stop a run
--   omnivia_runtime_stop_outcomes        the one outcome of that stop request
--
-- An admitted decision points at the `omnivia_runtime_runs` row it admitted and must agree
-- with that row's logical key, operation and audit reference. A rejected decision points at
-- no run. An accepted stop outcome points at the canonical `cancelled` runtime event for
-- its run; that event is what closes the run, and the existing terminal-run guards are what
-- refuse later history. Ignored terminal stops and rejected stops point at no event.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too.

CREATE TABLE IF NOT EXISTS omnivia_runtime_admission_decisions (
    workspace_id            TEXT    NOT NULL,
    admission_decision_id   TEXT    NOT NULL,
    logical_key             TEXT    NOT NULL,
    requested_operation     TEXT    NOT NULL,
    decision                TEXT    NOT NULL,
    resulting_run_id        TEXT,
    decided_at_us           INTEGER NOT NULL,
    reason                  TEXT    NOT NULL,
    audit_ref               TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, admission_decision_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(admission_decision_id) = 'text'
           AND length(admission_decision_id) BETWEEN 1 AND 128
           AND admission_decision_id GLOB '[A-Za-z0-9]*'
           AND admission_decision_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(admission_decision_id, char(0)) = 0),
    CHECK (typeof(logical_key) = 'text' AND length(logical_key) BETWEEN 1 AND 128
           AND logical_key GLOB '[A-Za-z0-9]*'
           AND logical_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(logical_key, char(0)) = 0),
    CHECK (typeof(requested_operation) = 'text'
           AND length(requested_operation) BETWEEN 1 AND 128
           AND requested_operation GLOB '[a-z]*'
           AND requested_operation NOT GLOB '*[^a-z0-9_.]*'
           AND requested_operation NOT GLOB '*.'
           AND requested_operation NOT GLOB '*.[^a-z]*'),
    CHECK (decision IN ('admitted', 'rejected')),
    CHECK (resulting_run_id IS NULL OR (typeof(resulting_run_id) = 'text'
           AND length(resulting_run_id) BETWEEN 1 AND 128
           AND resulting_run_id GLOB '[A-Za-z0-9]*'
           AND resulting_run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(resulting_run_id, char(0)) = 0)),
    CHECK ((decision = 'admitted' AND resulting_run_id IS NOT NULL)
           OR (decision = 'rejected' AND resulting_run_id IS NULL)),
    CHECK (typeof(decided_at_us) = 'integer' AND decided_at_us > 0),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*'
           AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.'
           AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    UNIQUE (workspace_id, requested_operation, logical_key),

    FOREIGN KEY (workspace_id, resulting_run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_stop_requests (
    workspace_id      TEXT    NOT NULL,
    stop_request_id   TEXT    NOT NULL,
    run_id            TEXT    NOT NULL,
    requested_at_us   INTEGER NOT NULL,
    requested_by      TEXT    NOT NULL,
    reason            TEXT    NOT NULL,
    audit_ref         TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, stop_request_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(stop_request_id) = 'text' AND length(stop_request_id) BETWEEN 1 AND 128
           AND stop_request_id GLOB '[A-Za-z0-9]*'
           AND stop_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stop_request_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(requested_at_us) = 'integer' AND requested_at_us > 0),
    CHECK (typeof(requested_by) = 'text' AND length(requested_by) BETWEEN 1 AND 128
           AND requested_by GLOB '[A-Za-z0-9]*'
           AND requested_by NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(requested_by, char(0)) = 0),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*'
           AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.'
           AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_stop_outcomes (
    workspace_id              TEXT    NOT NULL,
    stop_request_id           TEXT    NOT NULL,
    outcome                   TEXT    NOT NULL,
    completed_at_us           INTEGER NOT NULL,
    runtime_event_sequence    INTEGER,
    reason                    TEXT    NOT NULL,
    audit_ref                 TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, stop_request_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(stop_request_id) = 'text' AND length(stop_request_id) BETWEEN 1 AND 128
           AND stop_request_id GLOB '[A-Za-z0-9]*'
           AND stop_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stop_request_id, char(0)) = 0),
    CHECK (outcome IN ('accepted', 'ignored_already_terminal', 'rejected')),
    CHECK (typeof(completed_at_us) = 'integer' AND completed_at_us > 0),
    CHECK (runtime_event_sequence IS NULL OR (
           typeof(runtime_event_sequence) = 'integer'
           AND runtime_event_sequence BETWEEN 0 AND 999)),
    CHECK ((outcome = 'accepted' AND runtime_event_sequence IS NOT NULL)
           OR (outcome <> 'accepted' AND runtime_event_sequence IS NULL)),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*'
           AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.'
           AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, stop_request_id)
        REFERENCES omnivia_runtime_stop_requests (workspace_id, stop_request_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_admission_decisions_insert
BEFORE INSERT ON omnivia_runtime_admission_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_admission_decisions')
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
    SELECT RAISE(ABORT, 'omnivia: an admitted decision must agree with its resulting run')
    WHERE NEW.decision = 'admitted'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id
          AND run_id = NEW.resulting_run_id
          AND logical_key = NEW.logical_key
          AND originating_operation = NEW.requested_operation
          AND audit_ref = NEW.audit_ref
          AND created_at_us >= NEW.decided_at_us);
    SELECT RAISE(ABORT, 'omnivia: admission decision audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_admission_decisions_update
BEFORE UPDATE ON omnivia_runtime_admission_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_admission_decisions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_admission_decisions_delete
BEFORE DELETE ON omnivia_runtime_admission_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_admission_decisions is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_requests_insert
BEFORE INSERT ON omnivia_runtime_stop_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_stop_requests')
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
    SELECT RAISE(ABORT, 'omnivia: stop request cannot predate the run it names')
    WHERE NEW.requested_at_us < (
        SELECT created_at_us FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: stop request audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_requests_update
BEFORE UPDATE ON omnivia_runtime_stop_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_stop_requests is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_requests_delete
BEFORE DELETE ON omnivia_runtime_stop_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_stop_requests is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_outcomes_insert
BEFORE INSERT ON omnivia_runtime_stop_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_stop_outcomes')
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
    SELECT RAISE(ABORT, 'omnivia: stop outcome cannot predate its request')
    WHERE NEW.completed_at_us < (
        SELECT requested_at_us FROM omnivia_runtime_stop_requests
        WHERE workspace_id = NEW.workspace_id AND stop_request_id = NEW.stop_request_id);
    SELECT RAISE(ABORT, 'omnivia: an accepted stop outcome must name the cancelled event for its run')
    WHERE NEW.outcome = 'accepted'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_stop_requests r
        JOIN omnivia_runtime_events e
          ON e.workspace_id = r.workspace_id AND e.run_id = r.run_id
        WHERE r.workspace_id = NEW.workspace_id
          AND r.stop_request_id = NEW.stop_request_id
          AND e.sequence = NEW.runtime_event_sequence
          AND e.run_status = 'cancelled'
          AND e.occurred_at_us >= r.requested_at_us
          AND e.occurred_at_us <= NEW.completed_at_us);
    SELECT RAISE(ABORT, 'omnivia: an ignored stop requires the run to be already terminal')
    WHERE NEW.outcome = 'ignored_already_terminal'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_stop_requests r
        JOIN omnivia_runtime_events e
          ON e.workspace_id = r.workspace_id AND e.run_id = r.run_id
        WHERE r.workspace_id = NEW.workspace_id
          AND r.stop_request_id = NEW.stop_request_id
          AND e.sequence = (
            SELECT MAX(sequence) FROM omnivia_runtime_events
            WHERE workspace_id = r.workspace_id AND run_id = r.run_id)
          AND e.run_status IN ('succeeded', 'partially_completed', 'failed', 'cancelled'));
    SELECT RAISE(ABORT, 'omnivia: stop outcome audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_outcomes_update
BEFORE UPDATE ON omnivia_runtime_stop_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_stop_outcomes is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_outcomes_delete
BEFORE DELETE ON omnivia_runtime_stop_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_stop_outcomes is append-only; DELETE is never permitted');
END;
