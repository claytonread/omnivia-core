-- Durable mapped-input and capability readiness facts for Workflow Run steps.
--
-- Additive only. This table is deliberately a fact surface, not an evaluator:
-- a row states that one run recorded a required or satisfied readiness fact for one
-- step. It does not read the current Workflow definition, UI state, capability policy
-- or live provider state. Those systems may produce facts; the scheduler only consumes
-- facts already written for this run.
--
-- Four fact kinds are admitted:
--
--   mapped_input_required   this run says the step needs this mapped input
--   mapped_input_ready      this run says that mapped input is available
--   capability_required     this run says the step needs this capability
--   capability_granted      this run says that capability grant is available
--
-- `fact_id` is the stable input or capability identifier. Idempotency is by key:
-- every semantic column is in the primary key, so re-recording the same fact returns
-- silently in the writer while a different fact remains visibly different.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too.

CREATE TABLE IF NOT EXISTS omnivia_workflow_run_step_readiness_facts (
    workspace_id   TEXT    NOT NULL,
    run_id         TEXT    NOT NULL,
    step_id        TEXT    NOT NULL,
    fact_kind      TEXT    NOT NULL,
    fact_id        TEXT    NOT NULL,
    recorded_at_us INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_id, step_id, fact_kind, fact_id),

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
    CHECK (fact_kind IN (
        'mapped_input_required',
        'mapped_input_ready',
        'capability_required',
        'capability_granted'
    )),
    CHECK (typeof(fact_id) = 'text' AND length(fact_id) BETWEEN 1 AND 128
           AND fact_id GLOB '[A-Za-z0-9]*'
           AND fact_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(fact_id, char(0)) = 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_workflow_run_step_readiness_facts_step
    ON omnivia_workflow_run_step_readiness_facts
        (workspace_id, run_id, step_id, fact_kind);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_step_readiness_facts_insert
BEFORE INSERT ON omnivia_workflow_run_step_readiness_facts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_run_step_readiness_facts')
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
    SELECT RAISE(ABORT, 'omnivia: a workflow run readiness fact must name a step of its own plan')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs r
        JOIN omnivia_workflow_plan_steps s
          ON s.workspace_id = r.workspace_id
         AND s.workflow_id = r.workflow_id
         AND s.workflow_version = r.workflow_version
        WHERE r.workspace_id = NEW.workspace_id AND r.run_id = NEW.run_id
          AND s.step_id = NEW.step_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow run readiness fact cannot predate its run binding')
    WHERE NEW.recorded_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_step_readiness_facts_update
BEFORE UPDATE ON omnivia_workflow_run_step_readiness_facts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_run_step_readiness_facts is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_step_readiness_facts_delete
BEFORE DELETE ON omnivia_workflow_run_step_readiness_facts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_run_step_readiness_facts is append-only; DELETE is never permitted');
END;
