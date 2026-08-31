-- Durable loop iteration ledger for one Workflow Run's loop steps.
--
-- Additive only. Two append-only tables and six statement triggers, on the durable
-- Workflow Runtime records migration 0027 already established.
--
--   omnivia_workflow_loop_iterations          one claimed iteration of one loop step
--   omnivia_workflow_loop_iteration_outcomes  that iteration's single completion
--
-- Why this exists at all. 0027 stores a loop *bound* -- `max_iterations`,
-- `per_iteration_budget` and `total_budget` on a step's `loop_json` -- and nothing that
-- counts iterations or spend against it. A scheduler with only that could run a bounded
-- loop exactly once and record it as ordinary one-shot work, which is a false statement
-- about the run rather than a missing feature. These two tables are the durable count.
--
-- Why not a runtime attempt per iteration. 0018 refuses a second attempt on a step whose
-- attempt already succeeded, and every successful iteration of a healthy loop would be
-- exactly that. So a loop step holds *one* canonical runtime attempt for its whole life,
-- and iterations are counted here beside it: `runtime_attempt_id` names that one attempt,
-- and an insert naming a different one for a step that already has iterations aborts.
--
-- Why two tables and not one. A claimed iteration is immutable and its completion is a
-- separate later fact, exactly as `omnivia_runtime_attempts` and
-- `omnivia_runtime_attempt_outcomes` are separate: current fenced ownership does not
-- authorize rewriting history, so an iteration that "changes status" is stored as an
-- immutable half plus an append-only statement of what became of it. The outcome table is
-- keyed by its subject alone, so a second completion of one iteration is refused by the
-- primary key rather than by a rule written somewhere else, and an iteration with no
-- outcome row *is* the open iteration -- there is no status column to disagree with it.
--
-- The five durable invariants this file owns, none of which Python restates:
--
--   * the step must be a step of this run's own plan and must declare a *bounded* loop
--     -- all three of `max_iterations`, `per_iteration_budget` and `total_budget`,
--     coherent with each other. A `loop_json` missing one of them is refused rather
--     than treated as unbounded, because every check below is a comparison against
--     those numbers and a comparison against NULL is not a refusal;
--   * `iteration_number` is contiguous from 1 and never exceeds `max_iterations`;
--   * at most one iteration of a step is open -- claiming a second while one has no
--     outcome aborts, so a replayed or concurrent claim writes nothing;
--   * a loop that has already exited stays exited: once any outcome names an
--     `exit_reason`, no further iteration of that step may be claimed, so an exit on
--     the iteration cap or the total budget survives a restart rather than being
--     re-derived by whichever scheduler happens to look next;
--   * a completion's `cost` is within `per_iteration_budget`, and the running total of
--     costs across the step's iterations never exceeds `total_budget`.
--
-- `exit_reason` is NULL exactly while the loop continues. A completion that stops the
-- loop names why: `requested` when the work asked to stop, `max_iterations` or
-- `total_budget` when the bound stopped it, `failed` when the iteration failed.
--
-- `continue_requested` and `exit_reason` are two different facts and are stored as two.
-- `continue_requested` is what the work asked for; `exit_reason` is what became of the
-- loop. A row saying the work asked to continue and the iteration cap stopped it anyway
-- is the honest record of that iteration, and collapsing the two into one column would
-- lose which of them happened. What is refused is only the incoherent pair: a completion
-- that does not continue and names no reason, and a `requested` exit on a completion
-- that asked to continue.
--
-- Deliberately absent: any provider identity, invocation detail, prompt, response,
-- credential, filesystem path, URL or raw external log. Every column here is an
-- identifier, a member of a closed vocabulary, a bounded integer or a microsecond
-- instant. What an iteration *did* is not this ledger's fact; that it happened, what it
-- cost, and whether the loop goes on are.
--
-- Every comment sits between statements and never inside one, because the migrator
-- strips comments while the canonical fingerprint replays this text verbatim.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too.

CREATE TABLE IF NOT EXISTS omnivia_workflow_loop_iterations (
    workspace_id       TEXT    NOT NULL,
    run_id             TEXT    NOT NULL,
    step_id            TEXT    NOT NULL,
    iteration_number   INTEGER NOT NULL,
    loop_iteration_id  TEXT    NOT NULL,
    runtime_attempt_id TEXT    NOT NULL,
    opened_at_us       INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_id, step_id, iteration_number),
    UNIQUE (workspace_id, loop_iteration_id),

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
    CHECK (typeof(iteration_number) = 'integer' AND iteration_number > 0),
    CHECK (typeof(loop_iteration_id) = 'text'
           AND length(loop_iteration_id) BETWEEN 1 AND 128
           AND loop_iteration_id GLOB '[A-Za-z0-9]*'
           AND loop_iteration_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(loop_iteration_id, char(0)) = 0),
    CHECK (typeof(runtime_attempt_id) = 'text'
           AND length(runtime_attempt_id) BETWEEN 1 AND 128
           AND runtime_attempt_id GLOB '[A-Za-z0-9]*'
           AND runtime_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(runtime_attempt_id, char(0)) = 0),
    CHECK (typeof(opened_at_us) = 'integer' AND opened_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, runtime_attempt_id)
        REFERENCES omnivia_runtime_attempts (workspace_id, attempt_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_loop_iteration_outcomes (
    workspace_id       TEXT    NOT NULL,
    loop_iteration_id  TEXT    NOT NULL,
    status             TEXT    NOT NULL,
    cost               INTEGER NOT NULL,
    continue_requested INTEGER NOT NULL,
    exit_reason        TEXT,
    completed_at_us    INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, loop_iteration_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(loop_iteration_id) = 'text'
           AND length(loop_iteration_id) BETWEEN 1 AND 128
           AND loop_iteration_id GLOB '[A-Za-z0-9]*'
           AND loop_iteration_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(loop_iteration_id, char(0)) = 0),
    CHECK (status IN ('succeeded', 'failed')),
    CHECK (typeof(cost) = 'integer' AND cost > 0),
    CHECK (continue_requested IN (0, 1)),
    CHECK (exit_reason IS NULL
           OR exit_reason IN ('requested', 'max_iterations', 'total_budget', 'failed')),
    CHECK (continue_requested = 1 OR exit_reason IS NOT NULL),
    CHECK (exit_reason IS NOT 'requested' OR continue_requested = 0),
    CHECK (status = 'succeeded' OR exit_reason = 'failed'),
    CHECK (exit_reason IS NOT 'failed' OR status = 'failed'),
    CHECK (typeof(completed_at_us) = 'integer' AND completed_at_us > 0),

    FOREIGN KEY (workspace_id, loop_iteration_id)
        REFERENCES omnivia_workflow_loop_iterations
            (workspace_id, loop_iteration_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_loop_iterations_insert
BEFORE INSERT ON omnivia_workflow_loop_iterations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_loop_iterations')
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
    SELECT RAISE(ABORT, 'omnivia: a workflow loop iteration must name a step of its own plan that declares a bounded loop')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs r
        JOIN omnivia_workflow_plan_steps s
          ON s.workspace_id = r.workspace_id
         AND s.workflow_id = r.workflow_id
         AND s.workflow_version = r.workflow_version
        WHERE r.workspace_id = NEW.workspace_id AND r.run_id = NEW.run_id
          AND s.step_id = NEW.step_id
          AND json_extract(s.loop_json, '$.max_iterations') > 0
          AND json_extract(s.loop_json, '$.per_iteration_budget') > 0
          AND json_extract(s.loop_json, '$.total_budget')
              >= json_extract(s.loop_json, '$.per_iteration_budget'));
    SELECT RAISE(ABORT, 'omnivia: a workflow loop iteration must name a runtime attempt of its own run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_attempts
        WHERE workspace_id = NEW.workspace_id
          AND attempt_id = NEW.runtime_attempt_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow loop step keeps one runtime attempt for every iteration')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_loop_iterations
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND step_id = NEW.step_id
          AND runtime_attempt_id <> NEW.runtime_attempt_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow loop iteration sequence must be contiguous from one')
    WHERE NEW.iteration_number IS NOT (
        SELECT COALESCE(MAX(iteration_number), 0) + 1
        FROM omnivia_workflow_loop_iterations
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND step_id = NEW.step_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow loop step may hold only one open iteration')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_loop_iterations i
        WHERE i.workspace_id = NEW.workspace_id AND i.run_id = NEW.run_id
          AND i.step_id = NEW.step_id
          AND NOT EXISTS (
            SELECT 1 FROM omnivia_workflow_loop_iteration_outcomes o
            WHERE o.workspace_id = i.workspace_id
              AND o.loop_iteration_id = i.loop_iteration_id));
    SELECT RAISE(ABORT, 'omnivia: a workflow loop that has already exited claims no further iteration')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_loop_iterations i
        JOIN omnivia_workflow_loop_iteration_outcomes o
          ON o.workspace_id = i.workspace_id
         AND o.loop_iteration_id = i.loop_iteration_id
        WHERE i.workspace_id = NEW.workspace_id AND i.run_id = NEW.run_id
          AND i.step_id = NEW.step_id AND o.exit_reason IS NOT NULL);
    SELECT RAISE(ABORT, 'omnivia: a workflow loop cannot exceed its declared maximum iterations')
    WHERE NEW.iteration_number > (
        SELECT json_extract(s.loop_json, '$.max_iterations')
        FROM omnivia_workflow_runs r
        JOIN omnivia_workflow_plan_steps s
          ON s.workspace_id = r.workspace_id
         AND s.workflow_id = r.workflow_id
         AND s.workflow_version = r.workflow_version
        WHERE r.workspace_id = NEW.workspace_id AND r.run_id = NEW.run_id
          AND s.step_id = NEW.step_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow loop iteration cannot predate its run binding')
    WHERE NEW.opened_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_loop_iterations_update
BEFORE UPDATE ON omnivia_workflow_loop_iterations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_loop_iterations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_loop_iterations_delete
BEFORE DELETE ON omnivia_workflow_loop_iterations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_loop_iterations is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_loop_iteration_outcomes_insert
BEFORE INSERT ON omnivia_workflow_loop_iteration_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_loop_iteration_outcomes')
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
    SELECT RAISE(ABORT, 'omnivia: a workflow loop iteration outcome must name a claimed iteration')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_loop_iterations
        WHERE workspace_id = NEW.workspace_id
          AND loop_iteration_id = NEW.loop_iteration_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow loop iteration outcome cannot predate the iteration it closes')
    WHERE NEW.completed_at_us < (
        SELECT opened_at_us FROM omnivia_workflow_loop_iterations
        WHERE workspace_id = NEW.workspace_id
          AND loop_iteration_id = NEW.loop_iteration_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow loop iteration cost exceeds its per-iteration budget')
    WHERE NEW.cost > (
        SELECT json_extract(s.loop_json, '$.per_iteration_budget')
        FROM omnivia_workflow_loop_iterations i
        JOIN omnivia_workflow_runs r
          ON r.workspace_id = i.workspace_id AND r.run_id = i.run_id
        JOIN omnivia_workflow_plan_steps s
          ON s.workspace_id = r.workspace_id
         AND s.workflow_id = r.workflow_id
         AND s.workflow_version = r.workflow_version
         AND s.step_id = i.step_id
        WHERE i.workspace_id = NEW.workspace_id
          AND i.loop_iteration_id = NEW.loop_iteration_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow loop iteration cost exceeds the loop total budget')
    WHERE NEW.cost + (
        SELECT COALESCE(SUM(o.cost), 0)
        FROM omnivia_workflow_loop_iteration_outcomes o
        JOIN omnivia_workflow_loop_iterations j
          ON j.workspace_id = o.workspace_id
         AND j.loop_iteration_id = o.loop_iteration_id
        JOIN omnivia_workflow_loop_iterations i
          ON i.workspace_id = NEW.workspace_id
         AND i.loop_iteration_id = NEW.loop_iteration_id
         AND j.run_id = i.run_id AND j.step_id = i.step_id
        WHERE o.workspace_id = NEW.workspace_id) > (
        SELECT json_extract(s.loop_json, '$.total_budget')
        FROM omnivia_workflow_loop_iterations i
        JOIN omnivia_workflow_runs r
          ON r.workspace_id = i.workspace_id AND r.run_id = i.run_id
        JOIN omnivia_workflow_plan_steps s
          ON s.workspace_id = r.workspace_id
         AND s.workflow_id = r.workflow_id
         AND s.workflow_version = r.workflow_version
         AND s.step_id = i.step_id
        WHERE i.workspace_id = NEW.workspace_id
          AND i.loop_iteration_id = NEW.loop_iteration_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_loop_iteration_outcomes_update
BEFORE UPDATE ON omnivia_workflow_loop_iteration_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_loop_iteration_outcomes is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_loop_iteration_outcomes_delete
BEFORE DELETE ON omnivia_workflow_loop_iteration_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_loop_iteration_outcomes is append-only; DELETE is never permitted');
END;
