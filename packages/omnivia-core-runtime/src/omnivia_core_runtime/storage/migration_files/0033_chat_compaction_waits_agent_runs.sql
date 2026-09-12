-- Chat compaction, durable waits and delegated Agent Run continuation.
--
-- Additive successor schema for T-0663, T-0664 and T-0665.  It records
-- model-input compaction as a source-linked projection instead of rewriting
-- Conversation/Message/Part history, keeps Chat waits/approval pauses durable
-- and CAS-decided, and gives delegated agent work a stable run identity plus a
-- durable mailbox.  No table stores credentials, provider SDK objects,
-- endpoint URLs or signing material.

CREATE TABLE IF NOT EXISTS omnivia_chat_compaction_events (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    branch_id                 TEXT    NOT NULL,
    compaction_id             TEXT    NOT NULL,
    event_id                  TEXT    NOT NULL,
    event_sequence            INTEGER NOT NULL,
    event_type                TEXT    NOT NULL,
    source_start_sequence     INTEGER NOT NULL,
    source_end_sequence       INTEGER NOT NULL,
    policy_version            TEXT    NOT NULL,
    summarizer_version        TEXT    NOT NULL,
    payload_json              TEXT    NOT NULL,
    payload_digest            TEXT    NOT NULL,
    occurred_at_us            INTEGER NOT NULL,
    schema_version            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, event_id),
    UNIQUE (workspace_id, compaction_id, event_sequence),

    CHECK (event_type IN ('started', 'summary', 'completed', 'failed', 'cancelled')),
    CHECK (typeof(event_sequence) = 'integer' AND event_sequence >= 1),
    CHECK (typeof(source_start_sequence) = 'integer' AND source_start_sequence >= 1),
    CHECK (typeof(source_end_sequence) = 'integer' AND source_end_sequence >= source_start_sequence),
    CHECK (json_valid(payload_json)),
    CHECK (payload_digest GLOB 'sha256:[0-9a-f]*' AND length(payload_digest) = 71),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, branch_id)
        REFERENCES omnivia_chat_message_branches (workspace_id, branch_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_compaction_events_conversation
    ON omnivia_chat_compaction_events
        (workspace_id, conversation_id, branch_id, compaction_id, event_sequence);

CREATE TABLE IF NOT EXISTS omnivia_chat_model_input_projections (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    branch_id                 TEXT    NOT NULL,
    projection_id             TEXT    NOT NULL,
    compaction_id             TEXT    NOT NULL,
    source_start_sequence     INTEGER NOT NULL,
    source_end_sequence       INTEGER NOT NULL,
    policy_version            TEXT    NOT NULL,
    summarizer_version        TEXT    NOT NULL,
    omitted_source_refs_json  TEXT    NOT NULL,
    model_input_json          TEXT    NOT NULL,
    projection_digest         TEXT    NOT NULL,
    state                     TEXT    NOT NULL,
    created_at_us             INTEGER NOT NULL,
    completed_at_us           INTEGER NOT NULL,
    schema_version            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, projection_id),
    UNIQUE (workspace_id, compaction_id),

    CHECK (typeof(source_start_sequence) = 'integer' AND source_start_sequence >= 1),
    CHECK (typeof(source_end_sequence) = 'integer' AND source_end_sequence >= source_start_sequence),
    CHECK (json_valid(omitted_source_refs_json)),
    CHECK (json_valid(model_input_json)),
    CHECK (projection_digest GLOB 'sha256:[0-9a-f]*' AND length(projection_digest) = 71),
    CHECK (state IN ('completed')),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(completed_at_us) = 'integer' AND completed_at_us >= created_at_us),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, branch_id)
        REFERENCES omnivia_chat_message_branches (workspace_id, branch_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_model_input_projections_branch
    ON omnivia_chat_model_input_projections
        (workspace_id, conversation_id, branch_id, source_end_sequence, completed_at_us);

CREATE TABLE IF NOT EXISTS omnivia_chat_wait_interactions (
    workspace_id                         TEXT    NOT NULL,
    conversation_id                      TEXT    NOT NULL,
    wait_id                              TEXT    NOT NULL,
    kind                                 TEXT    NOT NULL,
    state                                TEXT    NOT NULL,
    requester_actor_id                   TEXT    NOT NULL,
    authorised_responder_policy_json     TEXT    NOT NULL,
    prompt_json                          TEXT    NOT NULL,
    resume_token_digest                  TEXT    NOT NULL,
    generation_job_id                    TEXT,
    turn_id                              TEXT,
    tool_call_id                         TEXT,
    agent_run_id                         TEXT,
    decision                             TEXT,
    decided_by_actor_id                  TEXT,
    sensitive_answer_ciphertext_digest   TEXT,
    audit_ref                            TEXT,
    version                              INTEGER NOT NULL,
    schema_version                       INTEGER NOT NULL,
    created_at_us                        INTEGER NOT NULL,
    updated_at_us                        INTEGER NOT NULL,
    expires_at_us                        INTEGER,
    decided_at_us                        INTEGER,

    PRIMARY KEY (workspace_id, wait_id),

    CHECK (kind IN ('approval', 'human_input', 'policy_pause')),
    CHECK (state IN ('asked', 'decided', 'expired', 'failed')),
    CHECK (json_valid(authorised_responder_policy_json)),
    CHECK (json_valid(prompt_json)),
    CHECK (resume_token_digest GLOB 'sha256:[0-9a-f]*' AND length(resume_token_digest) = 71),
    CHECK (sensitive_answer_ciphertext_digest IS NULL OR
           (sensitive_answer_ciphertext_digest GLOB 'sha256:[0-9a-f]*'
            AND length(sensitive_answer_ciphertext_digest) = 71)),
    CHECK (decision IS NULL OR decision IN ('approved', 'denied', 'answered')),
    CHECK ((state = 'decided' AND decision IS NOT NULL AND decided_by_actor_id IS NOT NULL
            AND decided_at_us IS NOT NULL)
           OR (state <> 'decided' AND decision IS NULL AND decided_by_actor_id IS NULL
               AND decided_at_us IS NULL)),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (expires_at_us IS NULL OR typeof(expires_at_us) = 'integer'),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_wait_interactions_attention
    ON omnivia_chat_wait_interactions (workspace_id, state, conversation_id, created_at_us);

CREATE TABLE IF NOT EXISTS omnivia_chat_agent_runs (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    agent_run_id              TEXT    NOT NULL,
    parent_generation_job_id  TEXT,
    parent_turn_id            TEXT,
    runtime_name              TEXT    NOT NULL,
    runtime_version           TEXT    NOT NULL,
    display_name              TEXT    NOT NULL,
    mode                      TEXT    NOT NULL,
    state                     TEXT    NOT NULL,
    configuration_digest      TEXT    NOT NULL,
    created_by_actor_id       TEXT    NOT NULL,
    heartbeat_at_us           INTEGER,
    version                   INTEGER NOT NULL,
    schema_version            INTEGER NOT NULL,
    created_at_us             INTEGER NOT NULL,
    updated_at_us             INTEGER NOT NULL,
    finished_at_us            INTEGER,

    PRIMARY KEY (workspace_id, agent_run_id),

    CHECK (mode IN ('one_shot', 'continuable')),
    CHECK (state IN ('running', 'waiting', 'succeeded', 'failed', 'cancelled', 'stale')),
    CHECK (configuration_digest GLOB 'sha256:[0-9a-f]*' AND length(configuration_digest) = 71),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (heartbeat_at_us IS NULL OR typeof(heartbeat_at_us) = 'integer'),
    CHECK ((state IN ('succeeded', 'failed', 'cancelled') AND finished_at_us IS NOT NULL)
           OR (state NOT IN ('succeeded', 'failed', 'cancelled') AND finished_at_us IS NULL)),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_agent_runs_conversation
    ON omnivia_chat_agent_runs (workspace_id, conversation_id, updated_at_us, agent_run_id);

CREATE TABLE IF NOT EXISTS omnivia_chat_agent_run_mailbox (
    workspace_id              TEXT    NOT NULL,
    agent_run_id              TEXT    NOT NULL,
    mailbox_message_id        TEXT    NOT NULL,
    direction                 TEXT    NOT NULL,
    idempotency_key           TEXT    NOT NULL,
    sender_actor_id           TEXT    NOT NULL,
    payload_json              TEXT    NOT NULL,
    payload_digest            TEXT    NOT NULL,
    delivery_state            TEXT    NOT NULL,
    created_at_us             INTEGER NOT NULL,
    delivered_at_us           INTEGER,
    schema_version            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, mailbox_message_id),
    UNIQUE (workspace_id, agent_run_id, idempotency_key),

    CHECK (direction IN ('to_agent', 'from_agent')),
    CHECK (json_valid(payload_json)),
    CHECK (payload_digest GLOB 'sha256:[0-9a-f]*' AND length(payload_digest) = 71),
    CHECK (delivery_state IN ('queued', 'delivered', 'failed')),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (delivered_at_us IS NULL OR typeof(delivered_at_us) = 'integer'),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),

    FOREIGN KEY (workspace_id, agent_run_id)
        REFERENCES omnivia_chat_agent_runs (workspace_id, agent_run_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_agent_run_mailbox_run
    ON omnivia_chat_agent_run_mailbox
        (workspace_id, agent_run_id, delivery_state, created_at_us, mailbox_message_id);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_compaction_events_insert
BEFORE INSERT ON omnivia_chat_compaction_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_compaction_events')
    WHERE omnivia_service_writer() IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_compaction_events_update
BEFORE UPDATE ON omnivia_chat_compaction_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_compaction_events forbids UPDATE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_compaction_events_delete
BEFORE DELETE ON omnivia_chat_compaction_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_compaction_events forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_model_input_projections_insert
BEFORE INSERT ON omnivia_chat_model_input_projections
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_model_input_projections')
    WHERE omnivia_service_writer() IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_model_input_projections_update
BEFORE UPDATE ON omnivia_chat_model_input_projections
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_model_input_projections forbids UPDATE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_model_input_projections_delete
BEFORE DELETE ON omnivia_chat_model_input_projections
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_model_input_projections forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_wait_interactions_insert
BEFORE INSERT ON omnivia_chat_wait_interactions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_wait_interactions')
    WHERE omnivia_service_writer() IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_wait_interactions_update
BEFORE UPDATE ON omnivia_chat_wait_interactions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_wait_interactions')
    WHERE omnivia_service_writer() IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: chat wait identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.wait_id <> OLD.wait_id
       OR NEW.kind <> OLD.kind
       OR NEW.requester_actor_id <> OLD.requester_actor_id
       OR NEW.resume_token_digest <> OLD.resume_token_digest
       OR NEW.created_at_us <> OLD.created_at_us
       OR NEW.schema_version <> OLD.schema_version;
    SELECT RAISE(ABORT, 'omnivia: chat wait version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: chat wait transition is invalid')
    WHERE NOT ((OLD.state = 'asked' AND NEW.state IN ('asked', 'decided', 'expired', 'failed'))
            OR (OLD.state IN ('decided', 'expired', 'failed') AND NEW.state = OLD.state));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_wait_interactions_delete
BEFORE DELETE ON omnivia_chat_wait_interactions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_wait_interactions forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_agent_runs_insert
BEFORE INSERT ON omnivia_chat_agent_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_agent_runs')
    WHERE omnivia_service_writer() IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_agent_runs_update
BEFORE UPDATE ON omnivia_chat_agent_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_agent_runs')
    WHERE omnivia_service_writer() IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: chat agent run identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.agent_run_id <> OLD.agent_run_id
       OR NEW.mode <> OLD.mode
       OR NEW.configuration_digest <> OLD.configuration_digest
       OR NEW.created_by_actor_id <> OLD.created_by_actor_id
       OR NEW.created_at_us <> OLD.created_at_us
       OR NEW.schema_version <> OLD.schema_version;
    SELECT RAISE(ABORT, 'omnivia: chat agent run version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: chat agent run transition is invalid')
    WHERE NOT ((OLD.state = 'running' AND NEW.state IN ('running', 'waiting', 'succeeded', 'failed', 'cancelled', 'stale'))
            OR (OLD.state = 'waiting' AND NEW.state IN ('waiting', 'running', 'succeeded', 'failed', 'cancelled', 'stale'))
            OR (OLD.state = 'stale' AND NEW.state IN ('stale', 'running', 'failed', 'cancelled'))
            OR (OLD.state IN ('succeeded', 'failed', 'cancelled') AND NEW.state = OLD.state));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_agent_runs_delete
BEFORE DELETE ON omnivia_chat_agent_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_agent_runs forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_agent_run_mailbox_insert
BEFORE INSERT ON omnivia_chat_agent_run_mailbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_agent_run_mailbox')
    WHERE omnivia_service_writer() IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_agent_run_mailbox_update
BEFORE UPDATE ON omnivia_chat_agent_run_mailbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_agent_run_mailbox')
    WHERE omnivia_service_writer() IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: chat agent mailbox identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.agent_run_id <> OLD.agent_run_id
       OR NEW.mailbox_message_id <> OLD.mailbox_message_id
       OR NEW.direction <> OLD.direction
       OR NEW.idempotency_key <> OLD.idempotency_key
       OR NEW.sender_actor_id <> OLD.sender_actor_id
       OR NEW.payload_json <> OLD.payload_json
       OR NEW.payload_digest <> OLD.payload_digest
       OR NEW.created_at_us <> OLD.created_at_us
       OR NEW.schema_version <> OLD.schema_version;
    SELECT RAISE(ABORT, 'omnivia: chat agent mailbox transition is invalid')
    WHERE NOT ((OLD.delivery_state = 'queued' AND NEW.delivery_state IN ('queued', 'delivered', 'failed'))
            OR (OLD.delivery_state IN ('delivered', 'failed') AND NEW.delivery_state = OLD.delivery_state));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_agent_run_mailbox_delete
BEFORE DELETE ON omnivia_chat_agent_run_mailbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_agent_run_mailbox forbids DELETE');
END;
