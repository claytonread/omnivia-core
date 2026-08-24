-- Durable Workflow Runtime foundation: one sealed plan, its steps, the runs bound to
-- it, their replay-safe observations, fenced child correlations and evidence-gated
-- completion.
--
-- Additive only. Eight append-only tables, four named indexes and twenty-four statement
-- triggers, on the canonical Runtime records migration 0018 already established. This
-- file stores Workflow Runtime truth and nothing that acts on it: no scheduler, no
-- dispatcher, no repository, no transport, no renderer state, no Chat state and no
-- Provider invocation state. It holds no Provider SDK type, no credential, no filesystem
-- path, no URL and no raw external log.
--
--   omnivia_workflow_plans                    one sealed definition and its one plan
--   omnivia_workflow_plan_steps               that plan's steps, declared and materialised
--   omnivia_workflow_runs                     one runtime run bound to exactly one plan
--   omnivia_workflow_run_step_observations    replay-safe plan and branch observations
--   omnivia_workflow_child_correlations       one fenced, budgeted parent/child binding
--   omnivia_workflow_child_correlation_results  what that correlation consumed and closed
--   omnivia_workflow_run_completion_evidence  the evidence a completion may be gated on
--   omnivia_workflow_run_completions          the single evidence-gated completion decision
--
-- Why eight and not eleven. A sealed `WorkflowDefinition` materialises to exactly one
-- `MaterialisedWorkflow`, because `materialise_workflow` is total and deterministic over
-- an already-sealed definition: the plan is a re-addressing of the definition, not a
-- second decision somebody made about it. Storing the two as separate tables would store
-- a 1:1 relation twice and licence a definition row whose plan disagrees with it, so one
-- row carries both sealed identities -- `definition_hash` and `plan_hash` -- and one step
-- row carries both step identities -- `step_definition_hash` and `materialised_step_hash`.
-- Both facts are stored immutably and content-addressed; they simply share a key.
--
-- A step's `route` is not a free choice beside its `execution_class`: `CHECK` admits
-- `route = execution_class` for a step with no child workflow, and exactly
-- `execution_class = 'WAIT'` with `route = 'CHILD_WORKFLOW'` for a step that has one. So
-- `CHILD_WORKFLOW` is unrepresentable except where the seam already puts it, and a
-- fifth route cannot appear beside the four execution classes by accident.
--
-- Observations are replay-safe by key rather than by convention. The primary key is
-- `(workspace, run, step, kind)`, so re-observing the same step is the same row -- a
-- writer replays with `INSERT OR IGNORE` and nothing changes -- while an observation
-- that contradicts the one already recorded is refused by an explicit `RAISE(ABORT)`,
-- which `OR IGNORE` does not suppress. A plan observation must additionally state the
-- route and sequence index its own plan step carries, so a stored observation is
-- replay-*equivalent* to the plan and not merely internally consistent.
--
-- Child correlation is fenced per parent step, not per correlation: the fence line is
-- `MAX(fence) + 1` over every correlation the same parent step ever opened, so a result
-- minted against a closed epoch stays stale instead of landing on its successor. Every
-- result must name that exact fence and the exact child id, version and hash the
-- correlation was opened with -- checked here against both the correlation row and the
-- step's own declared `child_workflow_json` -- and the running total of accepted cost may
-- never exceed the correlation's budget.
--
-- Deliberately absent: any evaluation of a `CompletionRule`. Storage requires that a
-- completion name at least one evidence kind and digest, and refuses one that names
-- none; whether *those particular* kinds satisfy the rule the workflow was configured
-- with is a comparison against a configured rule this migration does not store, and a
-- later repository or service owns it. What is enforced here is the part that is a
-- durable fact rather than a policy read: a completion with no evidence at all cannot
-- exist, and evidence cannot be appended after the decision it was supposed to gate.
--
-- Also absent: dependency-name resolution. `depends_on_json` is stored as exact canonical
-- JSON bytes and is checked for canonical form, not walked to prove every name is a step
-- of the same plan. That is a graph check over a document, and the deterministic
-- materialisation that produced these rows already refused an unknown dependency and a
-- cycle before either could be sealed.
--
-- Every comment sits between statements and never inside one, because the migrator
-- strips comments while the canonical fingerprint replays this text verbatim.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too.

CREATE TABLE IF NOT EXISTS omnivia_workflow_plans (
    workspace_id     TEXT    NOT NULL,
    workflow_id      TEXT    NOT NULL,
    workflow_version TEXT    NOT NULL,
    definition_hash  TEXT    NOT NULL,
    plan_hash        TEXT    NOT NULL,
    sealed_at_us     INTEGER NOT NULL,
    audit_ref        TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, workflow_id, workflow_version),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(workflow_id) = 'text' AND length(workflow_id) BETWEEN 1 AND 128
           AND workflow_id GLOB '[a-z0-9]*'
           AND workflow_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (typeof(workflow_version) = 'text'
           AND length(workflow_version) BETWEEN 1 AND 128
           AND workflow_version GLOB '[0-9]*'
           AND workflow_version NOT GLOB '*[^0-9A-Za-z.+-]*'
           AND instr(workflow_version, char(0)) = 0),
    CHECK (typeof(definition_hash) = 'text' AND length(definition_hash) = 71
           AND substr(definition_hash, 1, 7) = 'sha256:'
           AND substr(definition_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(plan_hash) = 'text' AND length(plan_hash) = 71
           AND substr(plan_hash, 1, 7) = 'sha256:'
           AND substr(plan_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (plan_hash <> definition_hash),
    CHECK (typeof(sealed_at_us) = 'integer' AND sealed_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_plan_steps (
    workspace_id           TEXT    NOT NULL,
    workflow_id            TEXT    NOT NULL,
    workflow_version       TEXT    NOT NULL,
    step_id                TEXT    NOT NULL,
    component_id           TEXT    NOT NULL,
    component_version      TEXT    NOT NULL,
    execution_class        TEXT    NOT NULL,
    route                  TEXT    NOT NULL,
    sequence_index         INTEGER NOT NULL,
    depends_on_json        TEXT    NOT NULL,
    branch_json            TEXT,
    loop_json              TEXT,
    child_workflow_json    TEXT,
    step_definition_hash   TEXT    NOT NULL,
    materialised_step_hash TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, workflow_id, workflow_version, step_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(workflow_id) = 'text' AND length(workflow_id) BETWEEN 1 AND 128
           AND workflow_id GLOB '[a-z0-9]*'
           AND workflow_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (typeof(workflow_version) = 'text'
           AND length(workflow_version) BETWEEN 1 AND 128
           AND workflow_version GLOB '[0-9]*'
           AND workflow_version NOT GLOB '*[^0-9A-Za-z.+-]*'
           AND instr(workflow_version, char(0)) = 0),
    CHECK (typeof(step_id) = 'text' AND length(step_id) BETWEEN 1 AND 128
           AND step_id GLOB '[a-z0-9]*' AND step_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (typeof(component_id) = 'text' AND length(component_id) BETWEEN 1 AND 128
           AND component_id GLOB '[a-z0-9]*' AND component_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (typeof(component_version) = 'text'
           AND length(component_version) BETWEEN 1 AND 128
           AND component_version GLOB '[0-9]*'
           AND component_version NOT GLOB '*[^0-9A-Za-z.+-]*'
           AND instr(component_version, char(0)) = 0),
    CHECK (execution_class IN ('AGENT', 'DETERMINISTIC', 'EFFECT', 'WAIT')),
    CHECK (route IN ('AGENT', 'DETERMINISTIC', 'EFFECT', 'WAIT', 'CHILD_WORKFLOW')),
    CHECK ((child_workflow_json IS NULL AND route = execution_class)
           OR (child_workflow_json IS NOT NULL
               AND execution_class = 'WAIT' AND route = 'CHILD_WORKFLOW')),
    CHECK (typeof(sequence_index) = 'integer'
           AND sequence_index BETWEEN 0 AND 1023),
    CHECK (typeof(depends_on_json) = 'text'
           AND length(CAST(depends_on_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(depends_on_json, char(0)) = 0),
    CHECK (branch_json IS NULL OR (typeof(branch_json) = 'text'
           AND length(CAST(branch_json AS BLOB)) BETWEEN 2 AND 4096
           AND instr(branch_json, char(0)) = 0)),
    CHECK (loop_json IS NULL OR (typeof(loop_json) = 'text'
           AND length(CAST(loop_json AS BLOB)) BETWEEN 2 AND 4096
           AND instr(loop_json, char(0)) = 0)),
    CHECK (child_workflow_json IS NULL OR (typeof(child_workflow_json) = 'text'
           AND length(CAST(child_workflow_json AS BLOB)) BETWEEN 2 AND 4096
           AND instr(child_workflow_json, char(0)) = 0)),
    CHECK (typeof(step_definition_hash) = 'text'
           AND length(step_definition_hash) = 71
           AND substr(step_definition_hash, 1, 7) = 'sha256:'
           AND substr(step_definition_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(materialised_step_hash) = 'text'
           AND length(materialised_step_hash) = 71
           AND substr(materialised_step_hash, 1, 7) = 'sha256:'
           AND substr(materialised_step_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (materialised_step_hash <> step_definition_hash),

    FOREIGN KEY (workspace_id, workflow_id, workflow_version)
        REFERENCES omnivia_workflow_plans (workspace_id, workflow_id, workflow_version)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_runs (
    workspace_id     TEXT    NOT NULL,
    run_id           TEXT    NOT NULL,
    workflow_id      TEXT    NOT NULL,
    workflow_version TEXT    NOT NULL,
    plan_hash        TEXT    NOT NULL,
    bound_at_us      INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(workflow_id) = 'text' AND length(workflow_id) BETWEEN 1 AND 128
           AND workflow_id GLOB '[a-z0-9]*'
           AND workflow_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (typeof(workflow_version) = 'text'
           AND length(workflow_version) BETWEEN 1 AND 128
           AND workflow_version GLOB '[0-9]*'
           AND workflow_version NOT GLOB '*[^0-9A-Za-z.+-]*'
           AND instr(workflow_version, char(0)) = 0),
    CHECK (typeof(plan_hash) = 'text' AND length(plan_hash) = 71
           AND substr(plan_hash, 1, 7) = 'sha256:'
           AND substr(plan_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(bound_at_us) = 'integer' AND bound_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, workflow_id, workflow_version, plan_hash)
        REFERENCES omnivia_workflow_plans
            (workspace_id, workflow_id, workflow_version, plan_hash)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_run_step_observations (
    workspace_id     TEXT    NOT NULL,
    run_id           TEXT    NOT NULL,
    step_id          TEXT    NOT NULL,
    observation_kind TEXT    NOT NULL,
    route            TEXT,
    sequence_index   INTEGER,
    branch_outcome   TEXT,
    branch_reason    TEXT,
    observed_at_us   INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_id, step_id, observation_kind),

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
    CHECK (observation_kind IN ('plan', 'branch')),
    CHECK (route IS NULL
           OR route IN ('AGENT', 'DETERMINISTIC', 'EFFECT', 'WAIT', 'CHILD_WORKFLOW')),
    CHECK (sequence_index IS NULL
           OR (typeof(sequence_index) = 'integer'
               AND sequence_index BETWEEN 0 AND 1023)),
    CHECK (branch_outcome IS NULL
           OR branch_outcome IN ('MATCHED', 'UNMATCHED', 'BLOCKED')),
    CHECK (branch_reason IS NULL
           OR (typeof(branch_reason) = 'text'
               AND length(branch_reason) BETWEEN 1 AND 128
               AND branch_reason GLOB '[a-z0-9]*'
               AND branch_reason NOT GLOB '*[^a-z0-9._-]*')),
    CHECK ((observation_kind = 'plan'
            AND route IS NOT NULL AND sequence_index IS NOT NULL
            AND branch_outcome IS NULL AND branch_reason IS NULL)
           OR (observation_kind = 'branch'
               AND route IS NULL AND sequence_index IS NULL
               AND branch_outcome IS NOT NULL AND branch_reason IS NOT NULL)),
    CHECK (typeof(observed_at_us) = 'integer' AND observed_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_child_correlations (
    workspace_id        TEXT    NOT NULL,
    correlation_id      TEXT    NOT NULL,
    parent_run_id       TEXT    NOT NULL,
    parent_step_id      TEXT    NOT NULL,
    child_workflow_id   TEXT    NOT NULL,
    child_version       TEXT    NOT NULL,
    child_workflow_hash TEXT    NOT NULL,
    fence               INTEGER NOT NULL,
    budget              INTEGER NOT NULL,
    opened_at_us        INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, correlation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(correlation_id) = 'text' AND length(correlation_id) BETWEEN 1 AND 128
           AND correlation_id GLOB '[A-Za-z0-9]*'
           AND correlation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(correlation_id, char(0)) = 0),
    CHECK (typeof(parent_run_id) = 'text' AND length(parent_run_id) BETWEEN 1 AND 128
           AND parent_run_id GLOB '[A-Za-z0-9]*'
           AND parent_run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(parent_run_id, char(0)) = 0),
    CHECK (typeof(parent_step_id) = 'text' AND length(parent_step_id) BETWEEN 1 AND 128
           AND parent_step_id GLOB '[a-z0-9]*'
           AND parent_step_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (typeof(child_workflow_id) = 'text'
           AND length(child_workflow_id) BETWEEN 1 AND 128
           AND child_workflow_id GLOB '[a-z0-9]*'
           AND child_workflow_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (typeof(child_version) = 'text' AND length(child_version) BETWEEN 1 AND 128
           AND child_version GLOB '[0-9]*'
           AND child_version NOT GLOB '*[^0-9A-Za-z.+-]*'
           AND instr(child_version, char(0)) = 0),
    CHECK (typeof(child_workflow_hash) = 'text'
           AND length(child_workflow_hash) = 71
           AND substr(child_workflow_hash, 1, 7) = 'sha256:'
           AND substr(child_workflow_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(fence) = 'integer' AND fence BETWEEN 1 AND 1000000),
    CHECK (typeof(budget) = 'integer' AND budget BETWEEN 1 AND 1000000000),
    CHECK (typeof(opened_at_us) = 'integer' AND opened_at_us > 0),

    FOREIGN KEY (workspace_id, parent_run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_child_correlation_results (
    workspace_id        TEXT    NOT NULL,
    correlation_id      TEXT    NOT NULL,
    result_sequence     INTEGER NOT NULL,
    outcome             TEXT    NOT NULL,
    fence               INTEGER NOT NULL,
    child_workflow_id   TEXT    NOT NULL,
    child_version       TEXT    NOT NULL,
    child_workflow_hash TEXT    NOT NULL,
    cost                INTEGER,
    recorded_at_us      INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, correlation_id, result_sequence),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(correlation_id) = 'text' AND length(correlation_id) BETWEEN 1 AND 128
           AND correlation_id GLOB '[A-Za-z0-9]*'
           AND correlation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(correlation_id, char(0)) = 0),
    CHECK (typeof(result_sequence) = 'integer'
           AND result_sequence BETWEEN 1 AND 1000000),
    CHECK (outcome IN ('accepted', 'closed')),
    CHECK (typeof(fence) = 'integer' AND fence BETWEEN 1 AND 1000000),
    CHECK (typeof(child_workflow_id) = 'text'
           AND length(child_workflow_id) BETWEEN 1 AND 128
           AND child_workflow_id GLOB '[a-z0-9]*'
           AND child_workflow_id NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (typeof(child_version) = 'text' AND length(child_version) BETWEEN 1 AND 128
           AND child_version GLOB '[0-9]*'
           AND child_version NOT GLOB '*[^0-9A-Za-z.+-]*'
           AND instr(child_version, char(0)) = 0),
    CHECK (typeof(child_workflow_hash) = 'text'
           AND length(child_workflow_hash) = 71
           AND substr(child_workflow_hash, 1, 7) = 'sha256:'
           AND substr(child_workflow_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (cost IS NULL
           OR (typeof(cost) = 'integer' AND cost BETWEEN 1 AND 1000000000)),
    CHECK ((outcome = 'accepted' AND cost IS NOT NULL)
           OR (outcome = 'closed' AND cost IS NULL)),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, correlation_id)
        REFERENCES omnivia_workflow_child_correlations (workspace_id, correlation_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_run_completion_evidence (
    workspace_id    TEXT    NOT NULL,
    run_id          TEXT    NOT NULL,
    evidence_kind   TEXT    NOT NULL,
    evidence_digest TEXT    NOT NULL,
    recorded_at_us  INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_id, evidence_kind),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(evidence_kind) = 'text' AND length(evidence_kind) BETWEEN 1 AND 128
           AND evidence_kind GLOB '[a-z0-9]*'
           AND evidence_kind NOT GLOB '*[^a-z0-9._-]*'),
    CHECK (typeof(evidence_digest) = 'text' AND length(evidence_digest) = 71
           AND substr(evidence_digest, 1, 7) = 'sha256:'
           AND substr(evidence_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_run_completions (
    workspace_id  TEXT    NOT NULL,
    run_id        TEXT    NOT NULL,
    outcome       TEXT    NOT NULL,
    decided_at_us INTEGER NOT NULL,
    audit_ref     TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, run_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (outcome IN ('SUCCEEDED', 'FAILED')),
    CHECK (typeof(decided_at_us) = 'integer' AND decided_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- Four named indexes: the parent key a composite foreign key needs, the two uniqueness
-- rules that would otherwise live in an implicit `sqlite_autoindex_*`, and the fence
-- line. They are declared by name rather than as inline `UNIQUE` clauses because the
-- canonical schema fingerprint filters implicit indexes out, and a constraint drift
-- detection cannot see is not a constraint.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_workflow_plans_identity
    ON omnivia_workflow_plans
        (workspace_id, workflow_id, workflow_version, plan_hash);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_workflow_plans_definition
    ON omnivia_workflow_plans (workspace_id, definition_hash);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_workflow_plan_steps_sequence
    ON omnivia_workflow_plan_steps
        (workspace_id, workflow_id, workflow_version, sequence_index);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_workflow_child_correlations_fence
    ON omnivia_workflow_child_correlations
        (workspace_id, parent_run_id, parent_step_id, fence);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_plans_insert
BEFORE INSERT ON omnivia_workflow_plans
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_plans')
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
    SELECT RAISE(ABORT, 'omnivia: workflow plan audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_plans_update
BEFORE UPDATE ON omnivia_workflow_plans
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_plans is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_plans_delete
BEFORE DELETE ON omnivia_workflow_plans
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_plans is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_plan_steps_insert
BEFORE INSERT ON omnivia_workflow_plan_steps
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_plan_steps')
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
    SELECT RAISE(ABORT, 'omnivia: a materialised step sequence must be contiguous from zero')
    WHERE NEW.sequence_index IS NOT (
        SELECT COALESCE(MAX(sequence_index), -1) + 1 FROM omnivia_workflow_plan_steps
        WHERE workspace_id = NEW.workspace_id
          AND workflow_id = NEW.workflow_id
          AND workflow_version = NEW.workflow_version);
    SELECT RAISE(ABORT, 'omnivia: a workflow plan that has admitted a run is sealed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id
          AND workflow_id = NEW.workflow_id
          AND workflow_version = NEW.workflow_version);
    SELECT RAISE(ABORT, 'omnivia: a workflow plan step document must be exact canonical JSON')
    WHERE json_valid(NEW.depends_on_json) IS NOT 1
       OR json(NEW.depends_on_json) <> NEW.depends_on_json
       OR json_type(NEW.depends_on_json) <> 'array'
       OR (NEW.branch_json IS NOT NULL
           AND (json_valid(NEW.branch_json) IS NOT 1
                OR json(NEW.branch_json) <> NEW.branch_json))
       OR (NEW.loop_json IS NOT NULL
           AND (json_valid(NEW.loop_json) IS NOT 1
                OR json(NEW.loop_json) <> NEW.loop_json))
       OR (NEW.child_workflow_json IS NOT NULL
           AND (json_valid(NEW.child_workflow_json) IS NOT 1
                OR json(NEW.child_workflow_json) <> NEW.child_workflow_json));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_plan_steps_update
BEFORE UPDATE ON omnivia_workflow_plan_steps
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_plan_steps is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_plan_steps_delete
BEFORE DELETE ON omnivia_workflow_plan_steps
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_plan_steps is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_runs_insert
BEFORE INSERT ON omnivia_workflow_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_runs')
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
    SELECT RAISE(ABORT, 'omnivia: a workflow run must bind a runtime run admitted as a workflow')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND definition_kind = 'workflow');
    SELECT RAISE(ABORT, 'omnivia: a workflow run must name the workflow its runtime run was admitted for')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND definition_id = NEW.workflow_id
          AND definition_version = NEW.workflow_version);
    SELECT RAISE(ABORT, 'omnivia: a workflow run cannot predate the runtime run it binds')
    WHERE NEW.bound_at_us < (
        SELECT created_at_us FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow run must bind a plan that has at least one step')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_plan_steps
        WHERE workspace_id = NEW.workspace_id
          AND workflow_id = NEW.workflow_id
          AND workflow_version = NEW.workflow_version);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_runs_update
BEFORE UPDATE ON omnivia_workflow_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_runs is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_runs_delete
BEFORE DELETE ON omnivia_workflow_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_runs is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_step_observations_insert
BEFORE INSERT ON omnivia_workflow_run_step_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_run_step_observations')
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
    SELECT RAISE(ABORT, 'omnivia: a workflow run step observation conflicts with the one already recorded')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_run_step_observations
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND step_id = NEW.step_id AND observation_kind = NEW.observation_kind
          AND (route IS NOT NEW.route
               OR sequence_index IS NOT NEW.sequence_index
               OR branch_outcome IS NOT NEW.branch_outcome
               OR branch_reason IS NOT NEW.branch_reason));
    SELECT RAISE(ABORT, 'omnivia: a workflow run observation must name a step of its own plan')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs r
        JOIN omnivia_workflow_plan_steps s
          ON s.workspace_id = r.workspace_id
         AND s.workflow_id = r.workflow_id
         AND s.workflow_version = r.workflow_version
        WHERE r.workspace_id = NEW.workspace_id AND r.run_id = NEW.run_id
          AND s.step_id = NEW.step_id);
    SELECT RAISE(ABORT, 'omnivia: a plan observation must state the route and position its plan step carries')
    WHERE NEW.observation_kind = 'plan'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs r
        JOIN omnivia_workflow_plan_steps s
          ON s.workspace_id = r.workspace_id
         AND s.workflow_id = r.workflow_id
         AND s.workflow_version = r.workflow_version
        WHERE r.workspace_id = NEW.workspace_id AND r.run_id = NEW.run_id
          AND s.step_id = NEW.step_id
          AND s.route = NEW.route
          AND s.sequence_index = NEW.sequence_index);
    SELECT RAISE(ABORT, 'omnivia: a branch observation must name a step that declares a branch')
    WHERE NEW.observation_kind = 'branch'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs r
        JOIN omnivia_workflow_plan_steps s
          ON s.workspace_id = r.workspace_id
         AND s.workflow_id = r.workflow_id
         AND s.workflow_version = r.workflow_version
        WHERE r.workspace_id = NEW.workspace_id AND r.run_id = NEW.run_id
          AND s.step_id = NEW.step_id
          AND s.branch_json IS NOT NULL);
    SELECT RAISE(ABORT, 'omnivia: a workflow run observation cannot predate its run binding')
    WHERE NEW.observed_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_step_observations_update
BEFORE UPDATE ON omnivia_workflow_run_step_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_run_step_observations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_step_observations_delete
BEFORE DELETE ON omnivia_workflow_run_step_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_run_step_observations is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_child_correlations_insert
BEFORE INSERT ON omnivia_workflow_child_correlations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_child_correlations')
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
    SELECT RAISE(ABORT, 'omnivia: a child correlation must name the exact child workflow its parent step delegates to')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs r
        JOIN omnivia_workflow_plan_steps s
          ON s.workspace_id = r.workspace_id
         AND s.workflow_id = r.workflow_id
         AND s.workflow_version = r.workflow_version
        WHERE r.workspace_id = NEW.workspace_id AND r.run_id = NEW.parent_run_id
          AND s.step_id = NEW.parent_step_id
          AND s.route = 'CHILD_WORKFLOW'
          AND json_extract(s.child_workflow_json, '$.workflow_id')
              = NEW.child_workflow_id
          AND json_extract(s.child_workflow_json, '$.version') = NEW.child_version
          AND json_extract(s.child_workflow_json, '$.workflow_hash')
              = NEW.child_workflow_hash
          AND json_extract(s.child_workflow_json, '$.budget') = NEW.budget);
    SELECT RAISE(ABORT, 'omnivia: a child correlation fence must continue its parent step''s fence line')
    WHERE NEW.fence IS NOT (
        SELECT COALESCE(MAX(fence), 0) + 1 FROM omnivia_workflow_child_correlations
        WHERE workspace_id = NEW.workspace_id
          AND parent_run_id = NEW.parent_run_id
          AND parent_step_id = NEW.parent_step_id);
    SELECT RAISE(ABORT, 'omnivia: a parent step may hold only one open child correlation')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_child_correlations c
        WHERE c.workspace_id = NEW.workspace_id
          AND c.parent_run_id = NEW.parent_run_id
          AND c.parent_step_id = NEW.parent_step_id
          AND NOT EXISTS (
            SELECT 1 FROM omnivia_workflow_child_correlation_results
            WHERE workspace_id = c.workspace_id
              AND correlation_id = c.correlation_id
              AND outcome = 'closed'));
    SELECT RAISE(ABORT, 'omnivia: a child correlation cannot predate its run binding')
    WHERE NEW.opened_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.parent_run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_child_correlations_update
BEFORE UPDATE ON omnivia_workflow_child_correlations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_child_correlations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_child_correlations_delete
BEFORE DELETE ON omnivia_workflow_child_correlations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_child_correlations is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_child_correlation_results_insert
BEFORE INSERT ON omnivia_workflow_child_correlation_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_child_correlation_results')
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
    SELECT RAISE(ABORT, 'omnivia: a child workflow result must name the fence and child identity it was opened with')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_child_correlations
        WHERE workspace_id = NEW.workspace_id
          AND correlation_id = NEW.correlation_id
          AND fence = NEW.fence
          AND child_workflow_id = NEW.child_workflow_id
          AND child_version = NEW.child_version
          AND child_workflow_hash = NEW.child_workflow_hash);
    SELECT RAISE(ABORT, 'omnivia: a closed child correlation admits no further result')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_child_correlation_results
        WHERE workspace_id = NEW.workspace_id
          AND correlation_id = NEW.correlation_id
          AND outcome = 'closed');
    SELECT RAISE(ABORT, 'omnivia: child correlation results must be contiguous from one')
    WHERE NEW.result_sequence IS NOT (
        SELECT COALESCE(MAX(result_sequence), 0) + 1
        FROM omnivia_workflow_child_correlation_results
        WHERE workspace_id = NEW.workspace_id
          AND correlation_id = NEW.correlation_id);
    SELECT RAISE(ABORT, 'omnivia: a child workflow result exceeds the correlation budget')
    WHERE NEW.outcome = 'accepted'
      AND NEW.cost + (
        SELECT COALESCE(SUM(cost), 0)
        FROM omnivia_workflow_child_correlation_results
        WHERE workspace_id = NEW.workspace_id
          AND correlation_id = NEW.correlation_id) > (
        SELECT budget FROM omnivia_workflow_child_correlations
        WHERE workspace_id = NEW.workspace_id
          AND correlation_id = NEW.correlation_id);
    SELECT RAISE(ABORT, 'omnivia: a child workflow result cannot predate its correlation')
    WHERE NEW.recorded_at_us < (
        SELECT opened_at_us FROM omnivia_workflow_child_correlations
        WHERE workspace_id = NEW.workspace_id
          AND correlation_id = NEW.correlation_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_child_correlation_results_update
BEFORE UPDATE ON omnivia_workflow_child_correlation_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_child_correlation_results is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_child_correlation_results_delete
BEFORE DELETE ON omnivia_workflow_child_correlation_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_child_correlation_results is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_completion_evidence_insert
BEFORE INSERT ON omnivia_workflow_run_completion_evidence
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_run_completion_evidence')
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
    SELECT RAISE(ABORT, 'omnivia: completion evidence is fixed at the decision and is never appended after it')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_run_completions
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: completion evidence cannot predate its run binding')
    WHERE NEW.recorded_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_completion_evidence_update
BEFORE UPDATE ON omnivia_workflow_run_completion_evidence
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_run_completion_evidence is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_completion_evidence_delete
BEFORE DELETE ON omnivia_workflow_run_completion_evidence
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_run_completion_evidence is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_completions_insert
BEFORE INSERT ON omnivia_workflow_run_completions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_run_completions')
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
    SELECT RAISE(ABORT, 'omnivia: completing a workflow run requires at least one evidence kind and digest')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_run_completion_evidence
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a workflow run cannot complete before it was bound')
    WHERE NEW.decided_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: workflow completion audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_completions_update
BEFORE UPDATE ON omnivia_workflow_run_completions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_run_completions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_run_completions_delete
BEFORE DELETE ON omnivia_workflow_run_completions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_run_completions is append-only; DELETE is never permitted');
END;
