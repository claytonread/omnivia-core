-- Chat turn, step and governed tool lifecycle.
--
-- Additive only. Migration 0029 remains the immutable Chat foundation and
-- migration 0030 remains the generation-status overlay. This migration adds the
-- durable identities needed to audit and replay a multi-step assistant turn with
-- governed tool proposals and side-effect receipts. It stores canonical JSON
-- records only; provider SDK objects, endpoints, headers and credentials have no
-- column in this schema.

CREATE TABLE IF NOT EXISTS omnivia_chat_turns (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    branch_id                 TEXT    NOT NULL,
    generation_job_id         TEXT    NOT NULL,
    generation_attempt_id     TEXT    NOT NULL,
    turn_id                   TEXT    NOT NULL,
    state                     TEXT    NOT NULL,
    current_step_id           TEXT,
    version                   INTEGER NOT NULL,
    schema_version            INTEGER NOT NULL,
    created_at_us             INTEGER NOT NULL,
    updated_at_us             INTEGER NOT NULL,
    finished_at_us            INTEGER,

    PRIMARY KEY (workspace_id, turn_id),
    UNIQUE (workspace_id, generation_job_id, generation_attempt_id),
    UNIQUE (workspace_id, conversation_id, turn_id),

    CHECK (state IN ('running', 'waiting', 'succeeded', 'failed', 'cancelled')),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (finished_at_us IS NULL OR (typeof(finished_at_us) = 'integer'
           AND finished_at_us >= updated_at_us)),
    CHECK ((state IN ('succeeded', 'failed', 'cancelled') AND finished_at_us IS NOT NULL)
           OR (state NOT IN ('succeeded', 'failed', 'cancelled') AND finished_at_us IS NULL)),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, branch_id)
        REFERENCES omnivia_chat_message_branches (workspace_id, branch_id),
    FOREIGN KEY (workspace_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs (workspace_id, generation_job_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_turns_conversation
    ON omnivia_chat_turns (workspace_id, conversation_id, created_at_us, turn_id);

CREATE TABLE IF NOT EXISTS omnivia_chat_turn_steps (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    turn_id                   TEXT    NOT NULL,
    step_id                   TEXT    NOT NULL,
    generation_job_id         TEXT    NOT NULL,
    generation_attempt_id     TEXT    NOT NULL,
    step_ordinal              INTEGER NOT NULL,
    step_kind                 TEXT    NOT NULL,
    state                     TEXT    NOT NULL,
    version                   INTEGER NOT NULL,
    schema_version            INTEGER NOT NULL,
    created_at_us             INTEGER NOT NULL,
    updated_at_us             INTEGER NOT NULL,
    finished_at_us            INTEGER,

    PRIMARY KEY (workspace_id, step_id),
    UNIQUE (workspace_id, turn_id, step_ordinal),
    UNIQUE (workspace_id, turn_id, step_id),

    CHECK (typeof(step_ordinal) = 'integer' AND step_ordinal >= 0),
    CHECK (step_kind IN ('assistant_output', 'tool_call')),
    CHECK (state IN ('proposed', 'approved', 'executing', 'succeeded',
                     'failed', 'denied', 'cancelled')),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (finished_at_us IS NULL OR (typeof(finished_at_us) = 'integer'
           AND finished_at_us >= updated_at_us)),
    CHECK ((state IN ('succeeded', 'failed', 'denied', 'cancelled') AND finished_at_us IS NOT NULL)
           OR (state NOT IN ('succeeded', 'failed', 'denied', 'cancelled') AND finished_at_us IS NULL)),

    FOREIGN KEY (workspace_id, turn_id)
        REFERENCES omnivia_chat_turns (workspace_id, turn_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_turn_steps_turn
    ON omnivia_chat_turn_steps (workspace_id, turn_id, step_ordinal);

CREATE TABLE IF NOT EXISTS omnivia_chat_tool_calls (
    workspace_id                    TEXT    NOT NULL,
    conversation_id                 TEXT    NOT NULL,
    turn_id                         TEXT    NOT NULL,
    step_id                         TEXT    NOT NULL,
    generation_job_id               TEXT    NOT NULL,
    generation_attempt_id           TEXT    NOT NULL,
    tool_call_id                    TEXT    NOT NULL,
    tool_name                       TEXT    NOT NULL,
    tool_version                    TEXT    NOT NULL,
    registry_ref                    TEXT    NOT NULL,
    state                           TEXT    NOT NULL,
    policy_state                    TEXT    NOT NULL,
    proposed_arguments_json         TEXT    NOT NULL,
    proposed_arguments_digest       TEXT    NOT NULL,
    post_policy_arguments_json      TEXT,
    post_policy_arguments_digest    TEXT,
    executed_arguments_digest       TEXT,
    result_id                       TEXT,
    failure_code                    TEXT,
    version                         INTEGER NOT NULL,
    schema_version                  INTEGER NOT NULL,
    created_at_us                   INTEGER NOT NULL,
    updated_at_us                   INTEGER NOT NULL,
    finished_at_us                  INTEGER,

    PRIMARY KEY (workspace_id, tool_call_id),
    UNIQUE (workspace_id, step_id, tool_call_id),

    CHECK (state IN ('proposed', 'approved', 'executing', 'succeeded',
                     'failed', 'denied', 'cancelled')),
    CHECK (policy_state IN ('pending', 'approved', 'denied')),
    CHECK (json_valid(proposed_arguments_json)),
    CHECK (post_policy_arguments_json IS NULL OR json_valid(post_policy_arguments_json)),
    CHECK (proposed_arguments_digest GLOB 'sha256:[0-9a-f]*'
           AND length(proposed_arguments_digest) = 71),
    CHECK (post_policy_arguments_digest IS NULL
           OR (post_policy_arguments_digest GLOB 'sha256:[0-9a-f]*'
               AND length(post_policy_arguments_digest) = 71)),
    CHECK (executed_arguments_digest IS NULL
           OR (executed_arguments_digest GLOB 'sha256:[0-9a-f]*'
               AND length(executed_arguments_digest) = 71)),
    CHECK (state IN ('proposed', 'denied', 'failed')
           OR post_policy_arguments_digest IS NOT NULL),
    CHECK (state IN ('executing', 'succeeded', 'failed', 'cancelled')
           OR executed_arguments_digest IS NULL),
    CHECK (state <> 'succeeded' OR result_id IS NOT NULL),
    CHECK (state <> 'failed' OR failure_code IS NOT NULL),
    CHECK (state <> 'denied' OR policy_state = 'denied'),
    CHECK (policy_state <> 'denied' OR state = 'denied'),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (finished_at_us IS NULL OR (typeof(finished_at_us) = 'integer'
           AND finished_at_us >= updated_at_us)),
    CHECK ((state IN ('succeeded', 'failed', 'denied', 'cancelled') AND finished_at_us IS NOT NULL)
           OR (state NOT IN ('succeeded', 'failed', 'denied', 'cancelled') AND finished_at_us IS NULL)),

    FOREIGN KEY (workspace_id, turn_id)
        REFERENCES omnivia_chat_turns (workspace_id, turn_id),
    FOREIGN KEY (workspace_id, step_id)
        REFERENCES omnivia_chat_turn_steps (workspace_id, step_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_tool_calls_turn
    ON omnivia_chat_tool_calls (workspace_id, turn_id, created_at_us, tool_call_id);

CREATE TABLE IF NOT EXISTS omnivia_chat_tool_results (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    turn_id                   TEXT    NOT NULL,
    step_id                   TEXT    NOT NULL,
    generation_job_id         TEXT    NOT NULL,
    generation_attempt_id     TEXT    NOT NULL,
    tool_call_id              TEXT    NOT NULL,
    result_id                 TEXT    NOT NULL,
    status                    TEXT    NOT NULL,
    result_payload_json       TEXT    NOT NULL,
    result_digest             TEXT    NOT NULL,
    schema_version            INTEGER NOT NULL,
    created_at_us             INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, result_id),
    UNIQUE (workspace_id, tool_call_id),

    CHECK (status IN ('succeeded', 'failed')),
    CHECK (json_valid(result_payload_json)),
    CHECK (result_digest GLOB 'sha256:[0-9a-f]*' AND length(result_digest) = 71),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),

    FOREIGN KEY (workspace_id, turn_id)
        REFERENCES omnivia_chat_turns (workspace_id, turn_id),
    FOREIGN KEY (workspace_id, step_id)
        REFERENCES omnivia_chat_turn_steps (workspace_id, step_id),
    FOREIGN KEY (workspace_id, tool_call_id)
        REFERENCES omnivia_chat_tool_calls (workspace_id, tool_call_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_tool_results_turn
    ON omnivia_chat_tool_results (workspace_id, turn_id, created_at_us, result_id);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_turns_insert
BEFORE INSERT ON omnivia_chat_turns
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_turns')
    WHERE omnivia_service_writer() IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_turns_update
BEFORE UPDATE ON omnivia_chat_turns
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_turns')
    WHERE omnivia_service_writer() IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: chat turn identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.branch_id <> OLD.branch_id
       OR NEW.generation_job_id <> OLD.generation_job_id
       OR NEW.generation_attempt_id <> OLD.generation_attempt_id
       OR NEW.turn_id <> OLD.turn_id
       OR NEW.schema_version <> OLD.schema_version
       OR NEW.created_at_us <> OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: chat turn version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: chat turn transition is invalid')
    WHERE NOT ((OLD.state = 'running'
                AND NEW.state IN ('running', 'waiting', 'succeeded', 'failed', 'cancelled'))
            OR (OLD.state = 'waiting'
                AND NEW.state IN ('waiting', 'running', 'succeeded', 'failed', 'cancelled'))
            OR (OLD.state IN ('succeeded', 'failed', 'cancelled')
                AND NEW.state = OLD.state));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_turns_delete
BEFORE DELETE ON omnivia_chat_turns
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_turns forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_turn_steps_insert
BEFORE INSERT ON omnivia_chat_turn_steps
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_turn_steps')
    WHERE omnivia_service_writer() IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_turn_steps_update
BEFORE UPDATE ON omnivia_chat_turn_steps
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_turn_steps')
    WHERE omnivia_service_writer() IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: chat turn step identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.turn_id <> OLD.turn_id
       OR NEW.step_id <> OLD.step_id
       OR NEW.generation_job_id <> OLD.generation_job_id
       OR NEW.generation_attempt_id <> OLD.generation_attempt_id
       OR NEW.step_ordinal <> OLD.step_ordinal
       OR NEW.step_kind <> OLD.step_kind
       OR NEW.schema_version <> OLD.schema_version
       OR NEW.created_at_us <> OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: chat turn step version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: chat turn step transition is invalid')
    WHERE NOT ((OLD.state = 'proposed'
                AND NEW.state IN ('proposed', 'approved', 'denied', 'failed', 'cancelled'))
            OR (OLD.state = 'approved'
                AND NEW.state IN ('approved', 'executing', 'failed', 'cancelled'))
            OR (OLD.state = 'executing'
                AND NEW.state IN ('executing', 'succeeded', 'failed', 'cancelled'))
            OR (OLD.state IN ('succeeded', 'failed', 'denied', 'cancelled')
                AND NEW.state = OLD.state));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_turn_steps_delete
BEFORE DELETE ON omnivia_chat_turn_steps
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_turn_steps forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_tool_calls_insert
BEFORE INSERT ON omnivia_chat_tool_calls
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_tool_calls')
    WHERE omnivia_service_writer() IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_tool_calls_update
BEFORE UPDATE ON omnivia_chat_tool_calls
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_tool_calls')
    WHERE omnivia_service_writer() IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: chat tool call identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.turn_id <> OLD.turn_id
       OR NEW.step_id <> OLD.step_id
       OR NEW.generation_job_id <> OLD.generation_job_id
       OR NEW.generation_attempt_id <> OLD.generation_attempt_id
       OR NEW.tool_call_id <> OLD.tool_call_id
       OR NEW.tool_name <> OLD.tool_name
       OR NEW.tool_version <> OLD.tool_version
       OR NEW.registry_ref <> OLD.registry_ref
       OR NEW.proposed_arguments_json <> OLD.proposed_arguments_json
       OR NEW.proposed_arguments_digest <> OLD.proposed_arguments_digest
       OR NEW.schema_version <> OLD.schema_version
       OR NEW.created_at_us <> OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: chat tool call version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: chat tool call policy transition is invalid')
    WHERE NOT ((OLD.policy_state = 'pending'
                AND NEW.policy_state IN ('pending', 'approved', 'denied'))
            OR (OLD.policy_state IN ('approved', 'denied')
                AND NEW.policy_state = OLD.policy_state));
    SELECT RAISE(ABORT, 'omnivia: chat tool call transition is invalid')
    WHERE NOT ((OLD.state = 'proposed'
                AND NEW.state IN ('proposed', 'approved', 'denied', 'failed', 'cancelled'))
            OR (OLD.state = 'approved'
                AND NEW.state IN ('approved', 'executing', 'failed', 'cancelled'))
            OR (OLD.state = 'executing'
                AND NEW.state IN ('executing', 'succeeded', 'failed', 'cancelled'))
            OR (OLD.state IN ('succeeded', 'failed', 'denied', 'cancelled')
                AND NEW.state = OLD.state));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_tool_calls_delete
BEFORE DELETE ON omnivia_chat_tool_calls
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_tool_calls forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_tool_results_insert
BEFORE INSERT ON omnivia_chat_tool_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_tool_results')
    WHERE omnivia_service_writer() IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_tool_results_update
BEFORE UPDATE ON omnivia_chat_tool_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_tool_results is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_tool_results_delete
BEFORE DELETE ON omnivia_chat_tool_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_tool_results forbids DELETE');
END;
