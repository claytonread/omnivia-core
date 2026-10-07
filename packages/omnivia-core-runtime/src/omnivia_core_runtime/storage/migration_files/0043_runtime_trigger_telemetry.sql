-- Durable trigger telemetry for declared triggers and external-signal waits (C21-A).
--
-- Additive only. Four append-only tables and twelve statement triggers. Nothing here
-- starts work, schedules anything, resolves a wait, or decides whether a stimulus
-- should be accepted: Core still has no live trigger source, and these tables record
-- what a later, separately owned writer decided. Every row is written in the same
-- fenced transaction as the state change it describes, so telemetry and truth cannot
-- diverge.
--
--   omnivia_runtime_trigger_declarations         one immutable version of a trigger
--   omnivia_runtime_trigger_subscription_events  one step of a trigger's lifecycle
--   omnivia_runtime_trigger_observations         one stimulus received by a trigger
--   omnivia_runtime_wait_signal_observations     one signal delivered to a wait
--
-- What a declaration is, and is not
-- ---------------------------------
--
-- A declaration pins a trigger's identity (`trigger_id`), its kind, the Project and
-- Workflow it is bound to, the exact Workflow version and sealed plan it would start
-- (a reference into 0027's plans, so the version is one Core actually holds), the
-- event contract it accepts (`event_type` plus a digest of the contract) and a digest
-- of its configuration (schedule, cooldown, debounce). A later change is a new
-- numbered declaration, contiguous from 1, never an edit. The kind, Project and
-- Workflow are fixed at declaration 1: a trigger that could be re-pointed at another
-- Workflow would make its own observation history describe a different thing.
--
-- A declaration is a description, not a grant. It carries no principal, scope,
-- capability or approval, and nothing here lets event content widen what a run may
-- do. Authority to start work still arrives through the governed operation that
-- consumes an observation.
--
-- `project_id` is a scoping label recorded as given. The runtime holds no Project
-- table, so there is nothing for it to reference; reads are keyed by it so that one
-- Project's trigger can never be read as another's.
--
-- Subscription state is history, not a column
-- -------------------------------------------
--
-- The highest-numbered subscription event is the current state. A subscription
-- starts `active` or `paused`; `disabled` is terminal; `unavailable` is how a
-- subscriber reports that it cannot currently receive, and is left only by an
-- explicit event. Every event names the latest declaration, so a state is always
-- stated against a known configuration, and time never moves backwards.
--
-- Delivery is not processing
-- --------------------------
--
-- An observation records what happened to a stimulus at the door: `accepted`,
-- `duplicate`, `dead_lettered`, or `uncertain` when the outcome could not be
-- established. It deliberately carries no processing status and no failure copy.
-- Whether the work it started finished, failed or was interrupted is a question
-- the job and run ledgers (0010, 0015, 0018) already answer, and the observation
-- only links to them (`job_id`, `run_id`, accepted observations alone). Acceptance
-- is an acknowledgement; it never implies completion. An accepted observation with
-- no link says only that nothing is known about processing.
--
-- Accepted observations need an active subscription and the declared event type.
-- Per trigger, observations are contiguous from 1 and monotonic in time. One
-- idempotency key is accepted once; a `duplicate` must repeat that accepted
-- observation unchanged (same key, same envelope digest), so a replay with changed
-- content cannot be filed as a harmless duplicate. A job or run is linked at most
-- once. A linked run must be a run of the trigger's own Workflow.
--
-- Wait signals
-- ------------
--
-- Wait signals share the vocabulary but not the table: a wait is not a declared
-- trigger and has no declaration to bind. Only `external_signal` waits qualify, so
-- `timer` and `approval` waits are refused here, and no timer support is implied.
-- A wait takes at most one accepted signal and none after its deadline or after it
-- expired or was cancelled. Whether the accepted signal then resolved the wait is
-- read from 0018's resolutions, not restated here.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too.
-- Retention is deliberately not provided; deletion or compaction needs its own
-- migration.
--
-- Every comment in this file sits between statements and never inside one, for the
-- fingerprint-loader reason 0018 states.

CREATE TABLE IF NOT EXISTS omnivia_runtime_trigger_declarations (
    workspace_id           TEXT    NOT NULL,
    trigger_declaration_id TEXT    NOT NULL,
    trigger_id             TEXT    NOT NULL,
    declaration_sequence   INTEGER NOT NULL,
    trigger_kind           TEXT    NOT NULL,
    project_id             TEXT    NOT NULL,
    workflow_id            TEXT    NOT NULL,
    workflow_version       TEXT    NOT NULL,
    plan_hash              TEXT    NOT NULL,
    event_type             TEXT    NOT NULL,
    event_contract_digest  TEXT    NOT NULL,
    configuration_digest   TEXT    NOT NULL,
    declared_at_us         INTEGER NOT NULL,
    audit_ref              TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, trigger_declaration_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(trigger_declaration_id) = 'text' AND length(trigger_declaration_id) BETWEEN 1 AND 128
           AND trigger_declaration_id GLOB '[A-Za-z0-9]*'
           AND trigger_declaration_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(trigger_declaration_id, char(0)) = 0),
    CHECK (typeof(trigger_id) = 'text' AND length(trigger_id) BETWEEN 1 AND 128
           AND trigger_id GLOB '[A-Za-z0-9]*'
           AND trigger_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(trigger_id, char(0)) = 0),
    CHECK (typeof(declaration_sequence) = 'integer' AND declaration_sequence > 0),
    CHECK (trigger_kind IN ('manual', 'schedule', 'webhook', 'cloudevent', 'catalogue_event')),
    CHECK (typeof(project_id) = 'text' AND length(project_id) BETWEEN 1 AND 128
           AND project_id GLOB '[A-Za-z0-9]*'
           AND project_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(project_id, char(0)) = 0),
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
    CHECK (typeof(event_type) = 'text' AND length(event_type) BETWEEN 1 AND 128
           AND event_type GLOB '[A-Za-z0-9]*'
           AND event_type NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(event_type, char(0)) = 0),
    CHECK (typeof(event_contract_digest) = 'text' AND length(event_contract_digest) = 71
           AND substr(event_contract_digest, 1, 7) = 'sha256:'
           AND substr(event_contract_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(configuration_digest) = 'text' AND length(configuration_digest) = 71
           AND substr(configuration_digest, 1, 7) = 'sha256:'
           AND substr(configuration_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(declared_at_us) = 'integer' AND declared_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    UNIQUE (workspace_id, trigger_id, declaration_sequence),

    FOREIGN KEY (workspace_id, workflow_id, workflow_version, plan_hash)
        REFERENCES omnivia_workflow_plans
            (workspace_id, workflow_id, workflow_version, plan_hash),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_trigger_declarations_workflow
    ON omnivia_runtime_trigger_declarations (
        workspace_id, project_id, workflow_id, trigger_id, declaration_sequence
    );

CREATE TABLE IF NOT EXISTS omnivia_runtime_trigger_subscription_events (
    workspace_id          TEXT    NOT NULL,
    subscription_event_id TEXT    NOT NULL,
    trigger_id            TEXT    NOT NULL,
    declaration_sequence  INTEGER NOT NULL,
    subscription_sequence INTEGER NOT NULL,
    subscription_state    TEXT    NOT NULL,
    reason                TEXT    NOT NULL,
    observed_at_us        INTEGER NOT NULL,
    audit_ref             TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, subscription_event_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(subscription_event_id) = 'text' AND length(subscription_event_id) BETWEEN 1 AND 128
           AND subscription_event_id GLOB '[A-Za-z0-9]*'
           AND subscription_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(subscription_event_id, char(0)) = 0),
    CHECK (typeof(trigger_id) = 'text' AND length(trigger_id) BETWEEN 1 AND 128
           AND trigger_id GLOB '[A-Za-z0-9]*'
           AND trigger_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(trigger_id, char(0)) = 0),
    CHECK (typeof(declaration_sequence) = 'integer' AND declaration_sequence > 0),
    CHECK (typeof(subscription_sequence) = 'integer' AND subscription_sequence > 0),
    CHECK (subscription_state IN ('active', 'paused', 'unavailable', 'disabled')),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*'
           AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.'
           AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(observed_at_us) = 'integer' AND observed_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    UNIQUE (workspace_id, trigger_id, subscription_sequence),

    FOREIGN KEY (workspace_id, trigger_id, declaration_sequence)
        REFERENCES omnivia_runtime_trigger_declarations (workspace_id, trigger_id, declaration_sequence),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_trigger_observations (
    workspace_id                 TEXT    NOT NULL,
    trigger_observation_id       TEXT    NOT NULL,
    trigger_id                   TEXT    NOT NULL,
    declaration_sequence         INTEGER NOT NULL,
    observation_sequence         INTEGER NOT NULL,
    event_id                     TEXT    NOT NULL,
    idempotency_key              TEXT    NOT NULL,
    event_type                   TEXT    NOT NULL,
    envelope_digest              TEXT    NOT NULL,
    occurred_at_us               INTEGER,
    observed_at_us               INTEGER NOT NULL,
    delivery_status              TEXT    NOT NULL,
    delivery_reason              TEXT,
    duplicate_of_observation_id  TEXT,
    job_id                       TEXT,
    run_id                       TEXT,
    audit_ref                    TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, trigger_observation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(trigger_observation_id) = 'text' AND length(trigger_observation_id) BETWEEN 1 AND 128
           AND trigger_observation_id GLOB '[A-Za-z0-9]*'
           AND trigger_observation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(trigger_observation_id, char(0)) = 0),
    CHECK (typeof(trigger_id) = 'text' AND length(trigger_id) BETWEEN 1 AND 128
           AND trigger_id GLOB '[A-Za-z0-9]*'
           AND trigger_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(trigger_id, char(0)) = 0),
    CHECK (typeof(declaration_sequence) = 'integer' AND declaration_sequence > 0),
    CHECK (typeof(observation_sequence) = 'integer' AND observation_sequence > 0),
    CHECK (typeof(event_id) = 'text' AND length(event_id) BETWEEN 1 AND 128
           AND event_id GLOB '[A-Za-z0-9]*'
           AND event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(event_id, char(0)) = 0),
    CHECK (typeof(idempotency_key) = 'text' AND length(idempotency_key) BETWEEN 1 AND 128
           AND idempotency_key GLOB '[A-Za-z0-9]*'
           AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(idempotency_key, char(0)) = 0),
    CHECK (typeof(event_type) = 'text' AND length(event_type) BETWEEN 1 AND 128
           AND event_type GLOB '[A-Za-z0-9]*'
           AND event_type NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(event_type, char(0)) = 0),
    CHECK (typeof(envelope_digest) = 'text' AND length(envelope_digest) = 71
           AND substr(envelope_digest, 1, 7) = 'sha256:'
           AND substr(envelope_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (occurred_at_us IS NULL OR (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0)),
    CHECK (typeof(observed_at_us) = 'integer' AND observed_at_us > 0),
    CHECK (delivery_status IN ('accepted', 'duplicate', 'dead_lettered', 'uncertain')),
    CHECK (
        (delivery_status IN ('accepted', 'duplicate') AND delivery_reason IS NULL)
        OR (delivery_status = 'dead_lettered' AND delivery_reason IS NOT NULL
            AND delivery_reason IN (
            'inactive_trigger', 'event_type_mismatch', 'trigger_cooldown',
            'trigger_debounce', 'no_automation_for_trigger', 'inactive_automation',
            'automation_concurrency', 'payload_rejected'))
        OR (delivery_status = 'uncertain' AND delivery_reason IS NOT NULL
            AND delivery_reason IN (
            'delivery_unconfirmed', 'recovery_interrupted'))),
    CHECK (duplicate_of_observation_id IS NULL OR (typeof(duplicate_of_observation_id) = 'text' AND length(duplicate_of_observation_id) BETWEEN 1 AND 128
           AND duplicate_of_observation_id GLOB '[A-Za-z0-9]*'
           AND duplicate_of_observation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(duplicate_of_observation_id, char(0)) = 0)),
    CHECK ((delivery_status = 'duplicate') = (duplicate_of_observation_id IS NOT NULL)),
    CHECK (job_id IS NULL OR (typeof(job_id) = 'text' AND length(job_id) BETWEEN 1 AND 128
           AND job_id GLOB '[A-Za-z0-9]*'
           AND job_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(job_id, char(0)) = 0)),
    CHECK (run_id IS NULL OR (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0)),
    CHECK (delivery_status = 'accepted' OR (job_id IS NULL AND run_id IS NULL)),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    UNIQUE (workspace_id, trigger_id, observation_sequence),
    UNIQUE (workspace_id, trigger_id, trigger_observation_id),

    FOREIGN KEY (workspace_id, trigger_id, declaration_sequence)
        REFERENCES omnivia_runtime_trigger_declarations (workspace_id, trigger_id, declaration_sequence),
    FOREIGN KEY (workspace_id, trigger_id, duplicate_of_observation_id)
        REFERENCES omnivia_runtime_trigger_observations (workspace_id, trigger_id, trigger_observation_id),
    FOREIGN KEY (workspace_id, job_id)
        REFERENCES omnivia_job_application_metadata (workspace_id, job_id),
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_runtime_trigger_observations_accepted_key
    ON omnivia_runtime_trigger_observations (workspace_id, trigger_id, idempotency_key)
    WHERE delivery_status = 'accepted';

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_runtime_trigger_observations_job
    ON omnivia_runtime_trigger_observations (workspace_id, job_id)
    WHERE job_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_runtime_trigger_observations_run
    ON omnivia_runtime_trigger_observations (workspace_id, run_id)
    WHERE run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS omnivia_runtime_wait_signal_observations (
    workspace_id                TEXT    NOT NULL,
    wait_signal_observation_id  TEXT    NOT NULL,
    wait_id                     TEXT    NOT NULL,
    observation_sequence        INTEGER NOT NULL,
    event_id                    TEXT    NOT NULL,
    envelope_digest             TEXT    NOT NULL,
    occurred_at_us              INTEGER,
    observed_at_us              INTEGER NOT NULL,
    delivery_status             TEXT    NOT NULL,
    delivery_reason             TEXT,
    duplicate_of_observation_id TEXT,
    audit_ref                   TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, wait_signal_observation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(wait_signal_observation_id) = 'text' AND length(wait_signal_observation_id) BETWEEN 1 AND 128
           AND wait_signal_observation_id GLOB '[A-Za-z0-9]*'
           AND wait_signal_observation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(wait_signal_observation_id, char(0)) = 0),
    CHECK (typeof(wait_id) = 'text' AND length(wait_id) BETWEEN 1 AND 128
           AND wait_id GLOB '[A-Za-z0-9]*'
           AND wait_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(wait_id, char(0)) = 0),
    CHECK (typeof(observation_sequence) = 'integer' AND observation_sequence > 0),
    CHECK (typeof(event_id) = 'text' AND length(event_id) BETWEEN 1 AND 128
           AND event_id GLOB '[A-Za-z0-9]*'
           AND event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(event_id, char(0)) = 0),
    CHECK (typeof(envelope_digest) = 'text' AND length(envelope_digest) = 71
           AND substr(envelope_digest, 1, 7) = 'sha256:'
           AND substr(envelope_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (occurred_at_us IS NULL OR (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0)),
    CHECK (typeof(observed_at_us) = 'integer' AND observed_at_us > 0),
    CHECK (delivery_status IN ('accepted', 'duplicate', 'dead_lettered', 'uncertain')),
    CHECK (
        (delivery_status IN ('accepted', 'duplicate') AND delivery_reason IS NULL)
        OR (delivery_status = 'dead_lettered' AND delivery_reason IS NOT NULL
            AND delivery_reason IN (
            'wait_already_resolved', 'deadline_passed', 'contract_rejected',
            'payload_rejected'))
        OR (delivery_status = 'uncertain' AND delivery_reason IS NOT NULL
            AND delivery_reason IN (
            'delivery_unconfirmed', 'recovery_interrupted'))),
    CHECK (duplicate_of_observation_id IS NULL OR (typeof(duplicate_of_observation_id) = 'text' AND length(duplicate_of_observation_id) BETWEEN 1 AND 128
           AND duplicate_of_observation_id GLOB '[A-Za-z0-9]*'
           AND duplicate_of_observation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(duplicate_of_observation_id, char(0)) = 0)),
    CHECK ((delivery_status = 'duplicate') = (duplicate_of_observation_id IS NOT NULL)),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    UNIQUE (workspace_id, wait_id, observation_sequence),
    UNIQUE (workspace_id, wait_id, wait_signal_observation_id),

    FOREIGN KEY (workspace_id, wait_id)
        REFERENCES omnivia_runtime_waits (workspace_id, wait_id),
    FOREIGN KEY (workspace_id, wait_id, duplicate_of_observation_id)
        REFERENCES omnivia_runtime_wait_signal_observations (workspace_id, wait_id, wait_signal_observation_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_runtime_wait_signal_observations_accepted
    ON omnivia_runtime_wait_signal_observations (workspace_id, wait_id)
    WHERE delivery_status = 'accepted';

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_trigger_declarations_insert
BEFORE INSERT ON omnivia_runtime_trigger_declarations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_trigger_declarations')
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
    SELECT RAISE(ABORT, 'omnivia: trigger declaration sequence must be contiguous')
    WHERE NEW.declaration_sequence IS NOT (
        SELECT COALESCE(MAX(declaration_sequence), 0) + 1
        FROM omnivia_runtime_trigger_declarations
        WHERE workspace_id = NEW.workspace_id AND trigger_id = NEW.trigger_id);
    SELECT RAISE(ABORT, 'omnivia: a trigger keeps its kind, project and workflow across declarations')
    WHERE NEW.declaration_sequence > 1 AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_trigger_declarations first
        WHERE first.workspace_id = NEW.workspace_id
          AND first.trigger_id = NEW.trigger_id
          AND first.declaration_sequence = 1
          AND first.trigger_kind = NEW.trigger_kind
          AND first.project_id = NEW.project_id
          AND first.workflow_id = NEW.workflow_id);
    SELECT RAISE(ABORT, 'omnivia: a trigger declaration must change something the previous one fixed')
    WHERE NEW.declaration_sequence > 1 AND EXISTS (
        SELECT 1 FROM omnivia_runtime_trigger_declarations prior
        WHERE prior.workspace_id = NEW.workspace_id
          AND prior.trigger_id = NEW.trigger_id
          AND prior.declaration_sequence = NEW.declaration_sequence - 1
          AND prior.workflow_version = NEW.workflow_version
          AND prior.plan_hash = NEW.plan_hash
          AND prior.event_type = NEW.event_type
          AND prior.event_contract_digest = NEW.event_contract_digest
          AND prior.configuration_digest = NEW.configuration_digest);
    SELECT RAISE(ABORT, 'omnivia: trigger declaration time must not regress')
    WHERE NEW.declaration_sequence > 1 AND NEW.declared_at_us < (
        SELECT declared_at_us FROM omnivia_runtime_trigger_declarations
        WHERE workspace_id = NEW.workspace_id
          AND trigger_id = NEW.trigger_id
          AND declaration_sequence = NEW.declaration_sequence - 1);
    SELECT RAISE(ABORT, 'omnivia: trigger declaration audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_trigger_declarations_update
BEFORE UPDATE ON omnivia_runtime_trigger_declarations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_trigger_declarations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_trigger_declarations_delete
BEFORE DELETE ON omnivia_runtime_trigger_declarations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_trigger_declarations is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_trigger_subscription_events_insert
BEFORE INSERT ON omnivia_runtime_trigger_subscription_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_trigger_subscription_events')
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
    SELECT RAISE(ABORT, 'omnivia: subscription sequence must be contiguous')
    WHERE NEW.subscription_sequence IS NOT (
        SELECT COALESCE(MAX(subscription_sequence), 0) + 1
        FROM omnivia_runtime_trigger_subscription_events
        WHERE workspace_id = NEW.workspace_id AND trigger_id = NEW.trigger_id);
    SELECT RAISE(ABORT, 'omnivia: a subscription event must name the latest declaration')
    WHERE NEW.declaration_sequence IS NOT (
        SELECT MAX(declaration_sequence) FROM omnivia_runtime_trigger_declarations
        WHERE workspace_id = NEW.workspace_id AND trigger_id = NEW.trigger_id);
    SELECT RAISE(ABORT, 'omnivia: a subscription starts active or paused')
    WHERE NEW.subscription_sequence = 1
      AND NEW.subscription_state NOT IN ('active', 'paused');
    SELECT RAISE(ABORT, 'omnivia: invalid subscription transition')
    WHERE NEW.subscription_sequence > 1 AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_trigger_subscription_events prior
        WHERE prior.workspace_id = NEW.workspace_id
          AND prior.trigger_id = NEW.trigger_id
          AND prior.subscription_sequence = NEW.subscription_sequence - 1
          AND ((prior.subscription_state = 'active'
                AND NEW.subscription_state IN ('paused', 'unavailable', 'disabled'))
            OR (prior.subscription_state = 'paused'
                AND NEW.subscription_state IN ('active', 'disabled'))
            OR (prior.subscription_state = 'unavailable'
                AND NEW.subscription_state IN ('active', 'paused', 'disabled'))));
    SELECT RAISE(ABORT, 'omnivia: subscription time must not regress')
    WHERE NEW.observed_at_us < COALESCE((
        SELECT MAX(observed_at_us) FROM omnivia_runtime_trigger_subscription_events
        WHERE workspace_id = NEW.workspace_id AND trigger_id = NEW.trigger_id), 0);
    SELECT RAISE(ABORT, 'omnivia: a subscription event cannot predate the declaration it follows')
    WHERE NEW.observed_at_us < COALESCE((
        SELECT declared_at_us FROM omnivia_runtime_trigger_declarations
        WHERE workspace_id = NEW.workspace_id
          AND trigger_id = NEW.trigger_id
          AND declaration_sequence = NEW.declaration_sequence), 0);
    SELECT RAISE(ABORT, 'omnivia: subscription event audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_trigger_subscription_events_update
BEFORE UPDATE ON omnivia_runtime_trigger_subscription_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_trigger_subscription_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_trigger_subscription_events_delete
BEFORE DELETE ON omnivia_runtime_trigger_subscription_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_trigger_subscription_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_trigger_observations_insert
BEFORE INSERT ON omnivia_runtime_trigger_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_trigger_observations')
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
    SELECT RAISE(ABORT, 'omnivia: trigger observation sequence must be contiguous')
    WHERE NEW.observation_sequence IS NOT (
        SELECT COALESCE(MAX(observation_sequence), 0) + 1
        FROM omnivia_runtime_trigger_observations
        WHERE workspace_id = NEW.workspace_id AND trigger_id = NEW.trigger_id);
    SELECT RAISE(ABORT, 'omnivia: an observation must name the latest declaration')
    WHERE NEW.declaration_sequence IS NOT (
        SELECT MAX(declaration_sequence) FROM omnivia_runtime_trigger_declarations
        WHERE workspace_id = NEW.workspace_id AND trigger_id = NEW.trigger_id);
    SELECT RAISE(ABORT, 'omnivia: trigger observation time must not regress')
    WHERE NEW.observed_at_us < COALESCE((
        SELECT MAX(observed_at_us) FROM omnivia_runtime_trigger_observations
        WHERE workspace_id = NEW.workspace_id AND trigger_id = NEW.trigger_id), 0);
    SELECT RAISE(ABORT, 'omnivia: an observation cannot predate the declaration it was judged against')
    WHERE NEW.observed_at_us < COALESCE((
        SELECT declared_at_us FROM omnivia_runtime_trigger_declarations
        WHERE workspace_id = NEW.workspace_id
          AND trigger_id = NEW.trigger_id
          AND declaration_sequence = NEW.declaration_sequence), 0);
    SELECT RAISE(ABORT, 'omnivia: an accepted observation must match the declared event type')
    WHERE NEW.delivery_status = 'accepted' AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_trigger_declarations d
        WHERE d.workspace_id = NEW.workspace_id
          AND d.trigger_id = NEW.trigger_id
          AND d.declaration_sequence = NEW.declaration_sequence
          AND d.event_type = NEW.event_type);
    SELECT RAISE(ABORT, 'omnivia: an accepted observation requires an active subscription')
    WHERE NEW.delivery_status = 'accepted' AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_trigger_subscription_events s
        WHERE s.workspace_id = NEW.workspace_id
          AND s.trigger_id = NEW.trigger_id
          AND s.subscription_state = 'active'
          AND s.subscription_sequence = (
            SELECT MAX(subscription_sequence) FROM omnivia_runtime_trigger_subscription_events
            WHERE workspace_id = NEW.workspace_id AND trigger_id = NEW.trigger_id));
    SELECT RAISE(ABORT, 'omnivia: a duplicate must repeat an accepted observation of the same trigger unchanged')
    WHERE NEW.delivery_status = 'duplicate' AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_trigger_observations o
        WHERE o.workspace_id = NEW.workspace_id
          AND o.trigger_id = NEW.trigger_id
          AND o.trigger_observation_id = NEW.duplicate_of_observation_id
          AND o.delivery_status = 'accepted'
          AND o.idempotency_key = NEW.idempotency_key
          AND o.envelope_digest = NEW.envelope_digest);
    SELECT RAISE(ABORT, 'omnivia: an observed run must be a run of the trigger workflow')
    WHERE NEW.run_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs r
        JOIN omnivia_runtime_trigger_declarations d
          ON d.workspace_id = r.workspace_id AND d.workflow_id = r.workflow_id
        WHERE r.workspace_id = NEW.workspace_id
          AND r.run_id = NEW.run_id
          AND d.trigger_id = NEW.trigger_id
          AND d.declaration_sequence = NEW.declaration_sequence);
    SELECT RAISE(ABORT, 'omnivia: an observed run must belong to the observed job')
    WHERE NEW.run_id IS NOT NULL AND NEW.job_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id
          AND run_id = NEW.run_id
          AND job_id = NEW.job_id);
    SELECT RAISE(ABORT, 'omnivia: trigger observation audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_trigger_observations_update
BEFORE UPDATE ON omnivia_runtime_trigger_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_trigger_observations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_trigger_observations_delete
BEFORE DELETE ON omnivia_runtime_trigger_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_trigger_observations is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_wait_signal_observations_insert
BEFORE INSERT ON omnivia_runtime_wait_signal_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_wait_signal_observations')
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
    SELECT RAISE(ABORT, 'omnivia: a signal observation must name an external-signal wait')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_waits
        WHERE workspace_id = NEW.workspace_id
          AND wait_id = NEW.wait_id
          AND kind = 'external_signal');
    SELECT RAISE(ABORT, 'omnivia: wait signal observation sequence must be contiguous')
    WHERE NEW.observation_sequence IS NOT (
        SELECT COALESCE(MAX(observation_sequence), 0) + 1
        FROM omnivia_runtime_wait_signal_observations
        WHERE workspace_id = NEW.workspace_id AND wait_id = NEW.wait_id);
    SELECT RAISE(ABORT, 'omnivia: wait signal observation time must not regress')
    WHERE NEW.observed_at_us < COALESCE((
        SELECT MAX(observed_at_us) FROM omnivia_runtime_wait_signal_observations
        WHERE workspace_id = NEW.workspace_id AND wait_id = NEW.wait_id), 0);
    SELECT RAISE(ABORT, 'omnivia: a signal observation cannot predate its wait')
    WHERE NEW.observed_at_us < (
        SELECT created_at_us FROM omnivia_runtime_waits
        WHERE workspace_id = NEW.workspace_id AND wait_id = NEW.wait_id);
    SELECT RAISE(ABORT, 'omnivia: an accepted signal must arrive before the wait deadline')
    WHERE NEW.delivery_status = 'accepted' AND EXISTS (
        SELECT 1 FROM omnivia_runtime_waits
        WHERE workspace_id = NEW.workspace_id
          AND wait_id = NEW.wait_id
          AND expires_at_us IS NOT NULL
          AND NEW.observed_at_us > expires_at_us);
    SELECT RAISE(ABORT, 'omnivia: an accepted signal cannot follow an expired or cancelled wait')
    WHERE NEW.delivery_status = 'accepted' AND EXISTS (
        SELECT 1 FROM omnivia_runtime_wait_resolutions
        WHERE workspace_id = NEW.workspace_id
          AND wait_id = NEW.wait_id
          AND status IN ('expired', 'cancelled'));
    SELECT RAISE(ABORT, 'omnivia: a duplicate signal must repeat an accepted observation of the same wait unchanged')
    WHERE NEW.delivery_status = 'duplicate' AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_wait_signal_observations o
        WHERE o.workspace_id = NEW.workspace_id
          AND o.wait_id = NEW.wait_id
          AND o.wait_signal_observation_id = NEW.duplicate_of_observation_id
          AND o.delivery_status = 'accepted'
          AND o.event_id = NEW.event_id
          AND o.envelope_digest = NEW.envelope_digest);
    SELECT RAISE(ABORT, 'omnivia: wait signal observation audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_wait_signal_observations_update
BEFORE UPDATE ON omnivia_runtime_wait_signal_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_wait_signal_observations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_wait_signal_observations_delete
BEFORE DELETE ON omnivia_runtime_wait_signal_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_wait_signal_observations is append-only; DELETE is never permitted');
END;
