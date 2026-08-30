-- Durable Chat text transport events.
--
-- Additive only. Migrations 0029 through 0033 remain immutable, and none of the
-- three that landed between this one and 0030 -- the request manifests, the
-- turn/step/tool lifecycle and the compaction/waits/agent-run tables -- touches
-- `omnivia_chat_generation_events` or references it, so the rebuild below is
-- unaffected by them. The single fact this
-- migration adds is that `omnivia_chat_generation_events` -- still the one
-- authority for a generation job's ordered lifecycle -- also admits
-- `chat.generation.text_appended`, the durable transport event that orders a
-- streamed text delta. Its existing closed canonical `payload_json` carries
-- normalized metadata only: the chunk ordinal and the Provider event identity.
--
-- SQLite cannot widen a CHECK constraint in place, so the table is rebuilt by the
-- documented procedure: build the successor, copy every row, drop the original
-- (which takes its triggers with it) and rename, then recreate the index and the
-- three guard triggers verbatim from 0029. Every column, key, foreign key, CHECK,
-- immutability rule, contiguous per-job sequence rule, event-shape rule and index
-- is carried across unchanged apart from the two widened `event_type` rules. The
-- whole migration runs inside the migrator's single transaction.
--
-- Nothing here stores a Provider credential, header, endpoint or raw external
-- body. The exact text delta remains solely in
-- `omnivia_chat_generation_text_chunks` from 0030 and is never copied here; the
-- event row carries only the metadata needed to order and identify that chunk,
-- and transport projection joins the two on the chunk ordinal.

CREATE TABLE omnivia_chat_generation_events_0034 (
    workspace_id                 TEXT    NOT NULL,
    conversation_id              TEXT    NOT NULL,
    branch_id                    TEXT    NOT NULL,
    generation_job_id            TEXT    NOT NULL,
    generation_attempt_id        TEXT,
    event_id                     TEXT    NOT NULL,
    event_type                   TEXT    NOT NULL,
    generation_event_sequence    INTEGER NOT NULL,
    trigger_message_id           TEXT    NOT NULL,
    result_message_id            TEXT,
    provider_event_id            TEXT,
    cursor                       TEXT    NOT NULL,
    payload_json                 TEXT    NOT NULL,
    occurred_at_us               INTEGER NOT NULL,
    schema_version               INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, event_id),
    UNIQUE (workspace_id, generation_job_id, generation_event_sequence),
    UNIQUE (workspace_id, cursor),
    UNIQUE (workspace_id, generation_job_id, generation_attempt_id, provider_event_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(branch_id) = 'text' AND length(branch_id) BETWEEN 1 AND 128
           AND branch_id GLOB '[A-Za-z0-9]*'
           AND branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(branch_id, char(0)) = 0),
    CHECK (typeof(generation_job_id) = 'text'
           AND length(generation_job_id) BETWEEN 1 AND 128
           AND generation_job_id GLOB '[A-Za-z0-9]*'
           AND generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_job_id, char(0)) = 0),
    CHECK (generation_attempt_id IS NULL
           OR (typeof(generation_attempt_id) = 'text'
               AND length(generation_attempt_id) BETWEEN 1 AND 128
               AND generation_attempt_id GLOB '[A-Za-z0-9]*'
               AND generation_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(generation_attempt_id, char(0)) = 0)),
    CHECK (typeof(event_id) = 'text' AND length(event_id) BETWEEN 1 AND 128
           AND event_id GLOB '[A-Za-z0-9]*'
           AND event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(event_id, char(0)) = 0),
    CHECK (event_type IN ('chat.generation.queued', 'chat.generation.started',
                          'chat.generation.text_appended',
                          'chat.generation.succeeded', 'chat.generation.failed',
                          'chat.generation.cancelled')),
    CHECK (typeof(generation_event_sequence) = 'integer'
           AND generation_event_sequence BETWEEN 1 AND 1000000),
    CHECK (typeof(trigger_message_id) = 'text'
           AND length(trigger_message_id) BETWEEN 1 AND 128
           AND trigger_message_id GLOB '[A-Za-z0-9]*'
           AND trigger_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(trigger_message_id, char(0)) = 0),
    CHECK (result_message_id IS NULL OR (typeof(result_message_id) = 'text'
           AND length(result_message_id) BETWEEN 1 AND 128
           AND result_message_id GLOB '[A-Za-z0-9]*'
           AND result_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(result_message_id, char(0)) = 0)),
    CHECK ((event_type = 'chat.generation.queued'
            AND generation_attempt_id IS NULL AND result_message_id IS NULL)
           OR (event_type = 'chat.generation.started'
               AND generation_attempt_id IS NOT NULL AND result_message_id IS NULL)
           OR (event_type = 'chat.generation.text_appended'
               AND generation_attempt_id IS NOT NULL AND result_message_id IS NULL)
           OR (event_type = 'chat.generation.succeeded'
               AND generation_attempt_id IS NOT NULL AND result_message_id IS NOT NULL)
           OR (event_type IN ('chat.generation.failed', 'chat.generation.cancelled')
               AND result_message_id IS NULL)),
    CHECK (provider_event_id IS NULL OR (typeof(provider_event_id) = 'text'
           AND length(provider_event_id) BETWEEN 1 AND 128
           AND provider_event_id GLOB '[A-Za-z0-9]*'
           AND provider_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_event_id, char(0)) = 0)),
    CHECK (provider_event_id IS NULL OR generation_attempt_id IS NOT NULL),
    CHECK (typeof(cursor) = 'text' AND length(cursor) BETWEEN 1 AND 256
           AND cursor GLOB '[A-Za-z0-9]*'
           AND cursor NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(cursor, char(0)) = 0),
    CHECK (typeof(payload_json) = 'text'
           AND length(CAST(payload_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(payload_json, char(0)) = 0),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, conversation_id, branch_id)
        REFERENCES omnivia_chat_message_branches
            (workspace_id, conversation_id, branch_id),
    FOREIGN KEY (workspace_id, conversation_id, trigger_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, conversation_id, result_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id)
) WITHOUT ROWID;

INSERT INTO omnivia_chat_generation_events_0034 (
    workspace_id, conversation_id, branch_id, generation_job_id, generation_attempt_id,
    event_id, event_type, generation_event_sequence, trigger_message_id,
    result_message_id, provider_event_id, cursor, payload_json, occurred_at_us,
    schema_version
)
SELECT
    workspace_id, conversation_id, branch_id, generation_job_id, generation_attempt_id,
    event_id, event_type, generation_event_sequence, trigger_message_id,
    result_message_id, provider_event_id, cursor, payload_json, occurred_at_us,
    schema_version
FROM omnivia_chat_generation_events;

DROP TABLE omnivia_chat_generation_events;

ALTER TABLE omnivia_chat_generation_events_0034 RENAME TO omnivia_chat_generation_events;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_generation_events_order
    ON omnivia_chat_generation_events
        (workspace_id, generation_job_id, generation_event_sequence);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_events_insert
BEFORE INSERT ON omnivia_chat_generation_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_events')
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
    SELECT RAISE(ABORT, 'omnivia: generation events must be contiguous from one')
    WHERE NEW.generation_event_sequence IS NOT (
        SELECT COALESCE(MAX(generation_event_sequence), 0) + 1
        FROM omnivia_chat_generation_events
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id);
    SELECT RAISE(ABORT, 'omnivia: generation event cannot predate its job')
    WHERE NEW.occurred_at_us < (
        SELECT created_at_us FROM omnivia_chat_generation_jobs
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id);
    SELECT RAISE(ABORT, 'omnivia: generation event payload must be an exact canonical JSON object')
    WHERE json_valid(NEW.payload_json) IS NOT 1
       OR json(NEW.payload_json) <> NEW.payload_json
       OR json_type(NEW.payload_json) <> 'object';
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_events_update
BEFORE UPDATE ON omnivia_chat_generation_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_events_delete
BEFORE DELETE ON omnivia_chat_generation_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_events forbids DELETE');
END;
