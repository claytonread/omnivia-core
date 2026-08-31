-- Durable selected-connection facts for one Workflow Run's steps.
--
-- Additive only. One append-only table, one named index and three statement triggers,
-- on the durable Workflow Runtime records migration 0027 already established.
--
--   omnivia_workflow_run_step_connections   which connections one run's step requires
--                                           selected, and which have been selected
--
-- Why one table and two fact kinds rather than two tables. `required` and `selected`
-- are the same fact about the same key -- this run's step, this direction, this
-- connection -- asked at two moments, and the scheduler's whole question is whether
-- the second set covers the first. Two tables would store one key twice and licence a
-- selection whose direction disagrees with the requirement it answers.
--
-- These are runtime scheduler facts and not a definition-graph evaluation. Nothing
-- here reads a connection's condition, evaluates it, or knows what the connection
-- joins: a row states that this run recorded this connection as required, or as
-- selected, at this instant. Whatever decided that is upstream of this table.
--
-- Idempotency is by key. Every column except `recorded_at_us` is in the primary key,
-- so re-recording the identical fact is the same row and an `INSERT OR IGNORE` writer
-- replays silently. `recorded_at_us` is deliberately outside the key and is not
-- conflict-checked: a retry after a crash reads its own clock, and the instant a fact
-- already has is the one it keeps.
--
-- Deliberately absent: any notion of a connection *condition*, its operands, its
-- evaluation, or a component's readiness verdict. Also absent: unselection. A run that
-- selected a connection selected it; append-only is the point, and a later run of the
-- same plan is a different `run_id`.
--
-- Every comment sits between statements and never inside one, because the migrator
-- strips comments while the canonical fingerprint replays this text verbatim.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too.

CREATE TABLE IF NOT EXISTS omnivia_workflow_run_step_connections (
    workspace_id   TEXT    NOT NULL,
    run_id         TEXT    NOT NULL,
    step_id        TEXT    NOT NULL,
    direction      TEXT    NOT NULL,
    connection_id  TEXT    NOT NULL,
    fact_kind      TEXT    NOT NULL,
    recorded_at_us INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_id, step_id, direction, connection_id, fact_kind),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(step_id) = 'text' AND length(step_id) BETWEEN 1 AND 128
           AND step_id GLOB '[a-z0-9]*' AND step_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (direction IN ('incoming', 'outgoing')),
    CHECK (typeof(connection_id) = 'text' AND length(connection_id) BETWEEN 1 AND 128
           AND connection_id GLOB '[A-Za-z0-9]*'
           AND connection_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(connection_id, char(0)) = 0),
    CHECK (fact_kind IN ('required', 'selected')),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_workflow_run_step_connections_step
    ON omnivia_workflow_run_step_connections
        (workspace_id, run_id, step_id, fact_kind);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_step_connections_insert
BEFORE INSERT ON omnivia_workflow_run_step_connections
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_run_step_connections')
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
    SELECT RAISE(ABORT, 'omnivia: a workflow run connection fact must name a step of its own plan')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs r
        JOIN omnivia_workflow_plan_steps s
          ON s.workspace_id = r.workspace_id
         AND s.workflow_id = r.workflow_id
         AND s.workflow_version = r.workflow_version
        WHERE r.workspace_id = NEW.workspace_id AND r.run_id = NEW.run_id
          AND s.step_id = NEW.step_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow run connection fact cannot predate its run binding')
    WHERE NEW.recorded_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_step_connections_update
BEFORE UPDATE ON omnivia_workflow_run_step_connections
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_run_step_connections is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_step_connections_delete
BEFORE DELETE ON omnivia_workflow_run_step_connections
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_run_step_connections is append-only; DELETE is never permitted');
END;
