-- Durable, display-safe provider reasoning summaries (migration 0075).
--
-- This table stores only an explicit provider-issued summary intended for end-user
-- display. It is not a home for raw reasoning tokens, `reasoning_text`, protected
-- chain-of-thought, request bodies or SDK objects. The F2a contract keeps lifecycle
-- reasoning events content-free and admits the separate `reasoning-summary` event;
-- the service accepts only that event into this table.
--
-- A summary is durable before the provider stream advances. Its provider event id
-- makes redelivery idempotent and its server-owned ordinal preserves order within an
-- Attempt. The committed assistant Message later projects the ordered rows to one
-- `reasoning.summary` part. Nothing is copied into the generation event payload.

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_reasoning_summaries (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    generation_job_id         TEXT    NOT NULL,
    generation_attempt_id     TEXT    NOT NULL,
    summary_ordinal           INTEGER NOT NULL,
    provider_event_id         TEXT,
    provider_part_id          TEXT    NOT NULL,
    provider_step_id          TEXT    NOT NULL,
    summary_text              TEXT    NOT NULL,
    content_hash              TEXT    NOT NULL,
    schema_version            INTEGER NOT NULL,
    occurred_at_us            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, generation_job_id, generation_attempt_id, summary_ordinal),
    UNIQUE (workspace_id, generation_job_id, provider_event_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text' AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(generation_job_id) = 'text' AND length(generation_job_id) BETWEEN 1 AND 128
           AND generation_job_id GLOB '[A-Za-z0-9]*'
           AND generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_job_id, char(0)) = 0),
    CHECK (typeof(generation_attempt_id) = 'text'
           AND length(generation_attempt_id) BETWEEN 1 AND 128
           AND generation_attempt_id GLOB '[A-Za-z0-9]*'
           AND generation_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_attempt_id, char(0)) = 0),
    CHECK (typeof(summary_ordinal) = 'integer' AND summary_ordinal BETWEEN 0 AND 1000000),
    CHECK (provider_event_id IS NULL OR (typeof(provider_event_id) = 'text'
           AND length(provider_event_id) BETWEEN 1 AND 128
           AND provider_event_id GLOB '[A-Za-z0-9]*'
           AND provider_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_event_id, char(0)) = 0)),
    CHECK (typeof(provider_part_id) = 'text' AND length(provider_part_id) BETWEEN 1 AND 128
           AND provider_part_id GLOB '[A-Za-z0-9]*'
           AND provider_part_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_part_id, char(0)) = 0),
    CHECK (typeof(provider_step_id) = 'text' AND length(provider_step_id) BETWEEN 1 AND 128
           AND provider_step_id GLOB '[A-Za-z0-9]*'
           AND provider_step_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_step_id, char(0)) = 0),
    CHECK (typeof(summary_text) = 'text'
           AND length(CAST(summary_text AS BLOB)) BETWEEN 1 AND 262144
           AND instr(summary_text, char(0)) = 0),
    CHECK (typeof(content_hash) = 'text' AND length(content_hash) = 71
           AND substr(content_hash, 1, 7) = 'sha256:'
           AND substr(content_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_generation_reasoning_summaries_attempt
    ON omnivia_chat_generation_reasoning_summaries
        (workspace_id, generation_job_id, generation_attempt_id, summary_ordinal);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_reasoning_summaries_insert
BEFORE INSERT ON omnivia_chat_generation_reasoning_summaries
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_reasoning_summaries')
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
    SELECT RAISE(ABORT, 'omnivia: generation reasoning summaries must be contiguous from zero')
    WHERE NEW.summary_ordinal IS NOT (
        SELECT COALESCE(MAX(summary_ordinal), -1) + 1
        FROM omnivia_chat_generation_reasoning_summaries
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id
          AND generation_attempt_id = NEW.generation_attempt_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_reasoning_summaries_update
BEFORE UPDATE ON omnivia_chat_generation_reasoning_summaries
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_reasoning_summaries is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_reasoning_summaries_delete
BEFORE DELETE ON omnivia_chat_generation_reasoning_summaries
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_reasoning_summaries forbids DELETE');
END;
