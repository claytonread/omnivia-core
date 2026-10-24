-- Durable, schema-bound provider structured outputs (migration 0077).
--
-- F2a carries a parsed object plus an opaque schema reference. Core records that
-- object before the provider stream advances, then materialises only an explicitly
-- registered schema as a closed Message part. Raw response bytes, SDK objects and
-- arbitrary schemas have no route into this table.

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_structured_outputs (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    generation_job_id         TEXT    NOT NULL,
    generation_attempt_id     TEXT    NOT NULL,
    output_ordinal            INTEGER NOT NULL,
    provider_event_id         TEXT,
    schema_ref                TEXT    NOT NULL,
    output_json               TEXT    NOT NULL,
    content_hash              TEXT    NOT NULL,
    schema_version            INTEGER NOT NULL,
    occurred_at_us            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, generation_job_id, generation_attempt_id, output_ordinal),
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
    CHECK (typeof(output_ordinal) = 'integer' AND output_ordinal BETWEEN 0 AND 1000000),
    CHECK (provider_event_id IS NULL OR (typeof(provider_event_id) = 'text'
           AND length(provider_event_id) BETWEEN 1 AND 128
           AND provider_event_id GLOB '[A-Za-z0-9]*'
           AND provider_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_event_id, char(0)) = 0)),
    CHECK (typeof(schema_ref) = 'text' AND length(schema_ref) BETWEEN 1 AND 512
           AND instr(schema_ref, char(0)) = 0),
    CHECK (typeof(output_json) = 'text'
           AND length(CAST(output_json AS BLOB)) BETWEEN 2 AND 262144
           AND json_valid(output_json) = 1
           AND json_type(output_json) = 'object'),
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

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_generation_structured_outputs_attempt
    ON omnivia_chat_generation_structured_outputs
        (workspace_id, generation_job_id, generation_attempt_id, output_ordinal);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_structured_outputs_insert
BEFORE INSERT ON omnivia_chat_generation_structured_outputs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_structured_outputs')
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
    SELECT RAISE(ABORT, 'omnivia: generation structured outputs must be contiguous from zero')
    WHERE NEW.output_ordinal IS NOT (
        SELECT COALESCE(MAX(output_ordinal), -1) + 1
        FROM omnivia_chat_generation_structured_outputs
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id
          AND generation_attempt_id = NEW.generation_attempt_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_structured_outputs_update
BEFORE UPDATE ON omnivia_chat_generation_structured_outputs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_structured_outputs is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_structured_outputs_delete
BEFORE DELETE ON omnivia_chat_generation_structured_outputs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_structured_outputs forbids DELETE');
END;
