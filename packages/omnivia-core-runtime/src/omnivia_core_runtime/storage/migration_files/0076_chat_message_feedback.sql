-- Private, actor-scoped feedback for committed assistant messages (migration 0076).
--
-- This is workspace conversation data. Nothing in this table is a provider request,
-- a training submission or an integration outbox. One actor owns at most one record
-- per Message and replaces it with compare-and-set versioning.

CREATE TABLE IF NOT EXISTS omnivia_chat_message_feedback (
    workspace_id       TEXT    NOT NULL,
    conversation_id    TEXT    NOT NULL,
    message_id         TEXT    NOT NULL,
    actor_id           TEXT    NOT NULL,
    reaction           TEXT    NOT NULL,
    reasons_json       TEXT    NOT NULL,
    note_text          TEXT,
    schema_version     INTEGER NOT NULL,
    version            INTEGER NOT NULL,
    created_at_us      INTEGER NOT NULL,
    updated_at_us      INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, message_id, actor_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text' AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(message_id) = 'text' AND length(message_id) BETWEEN 1 AND 128
           AND message_id GLOB '[A-Za-z0-9]*'
           AND message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(message_id, char(0)) = 0),
    CHECK (typeof(actor_id) = 'text' AND length(actor_id) BETWEEN 1 AND 128
           AND actor_id GLOB '[A-Za-z0-9]*'
           AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(actor_id, char(0)) = 0),
    CHECK (reaction IN ('helpful', 'not_helpful')),
    CHECK (typeof(reasons_json) = 'text' AND length(CAST(reasons_json AS BLOB)) <= 1024
           AND json_valid(reasons_json) AND json_type(reasons_json) = 'array'
           AND json_array_length(reasons_json) BETWEEN 0 AND 5),
    CHECK (note_text IS NULL OR (typeof(note_text) = 'text'
           AND length(note_text) <= 600
           AND length(CAST(note_text AS BLOB)) <= 2400
           AND instr(note_text, char(0)) = 0)),
    CHECK (reaction = 'not_helpful' OR (reasons_json = '[]' AND note_text IS NULL)),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),

    FOREIGN KEY (workspace_id, conversation_id, message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_message_feedback_actor_conversation
    ON omnivia_chat_message_feedback
        (workspace_id, conversation_id, actor_id, message_id);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_feedback_insert
BEFORE INSERT ON omnivia_chat_message_feedback
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_message_feedback')
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
    SELECT RAISE(ABORT, 'omnivia: message feedback reasons contain an unknown value')
    WHERE EXISTS (
        SELECT 1 FROM json_each(NEW.reasons_json)
        WHERE type <> 'text'
           OR value NOT IN ('incorrect', 'wrong_source', 'missing_evidence',
                            'unsafe_or_private', 'other'));
    SELECT RAISE(ABORT, 'omnivia: message feedback reasons must be unique')
    WHERE json_array_length(NEW.reasons_json) IS NOT (
        SELECT COUNT(DISTINCT value) FROM json_each(NEW.reasons_json));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_feedback_update
BEFORE UPDATE ON omnivia_chat_message_feedback
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_message_feedback')
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
    SELECT RAISE(ABORT, 'omnivia: message feedback identity and creation are immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.conversation_id IS NOT OLD.conversation_id
       OR NEW.message_id IS NOT OLD.message_id
       OR NEW.actor_id IS NOT OLD.actor_id
       OR NEW.schema_version IS NOT OLD.schema_version
       OR NEW.created_at_us IS NOT OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: message feedback version must advance by one')
    WHERE NEW.version IS NOT OLD.version + 1 OR NEW.updated_at_us < OLD.updated_at_us;
    SELECT RAISE(ABORT, 'omnivia: message feedback reasons contain an unknown value')
    WHERE EXISTS (
        SELECT 1 FROM json_each(NEW.reasons_json)
        WHERE type <> 'text'
           OR value NOT IN ('incorrect', 'wrong_source', 'missing_evidence',
                            'unsafe_or_private', 'other'));
    SELECT RAISE(ABORT, 'omnivia: message feedback reasons must be unique')
    WHERE json_array_length(NEW.reasons_json) IS NOT (
        SELECT COUNT(DISTINCT value) FROM json_each(NEW.reasons_json));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_feedback_delete
BEFORE DELETE ON omnivia_chat_message_feedback
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_message_feedback forbids DELETE');
END;
