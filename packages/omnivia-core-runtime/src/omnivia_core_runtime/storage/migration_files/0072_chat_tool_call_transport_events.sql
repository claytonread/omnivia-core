-- Chat tool-call lifecycle transport events (C8 step 4; migration 0072).
--
-- One widening, by the one route SQLite allows: `omnivia_chat_generation_events` is REBUILT so that it
-- admits `chat.tool_call.state_changed`. Nothing else in the table changes -- every column, every other
-- CHECK, the index and all three guards are 0069's own text, carried over unaltered. A tool call belongs to
-- a generation attempt, so its transport event rides the job-scoped stream `chat events` already serves
-- rather than a second stream of its own; that is the convention C6 set when it put
-- `chat.direction.state_changed` here, and this follows it.
--
-- Why a rebuild at all. The admitted event-type vocabulary is a CHECK, and SQLite cannot alter a CHECK in
-- place. 0034 rebuilt this table for the same reason and 0069 again after it; this is the third, and the
-- guard test in `tests/phase3/runtime/test_chat_text_transport_migration.py` is what holds each rebuild to
-- being a widening rather than a rewrite -- it compares the live table against the migration that last
-- declared it, and reproves that every RAISE 0029 declared is still raised.
--
-- Event payload shape (closed, identity-only, enforced by CHECK on the events table, in the same style
-- 0069 used for its two):
--   chat.tool_call.state_changed   attempt optional, no result message
--       {"lifecycleSequence": <int 1..1000000>, "reasonCode": <text, optional>,
--        "state": <text 1..128>, "toolCallId": <scoped id>, "turnId": <text 1..128>,
--        "waitId": <text, optional>}
--     `state` is deliberately TEXT and not a closed set: the tool lifecycle's own state vocabulary is
--     Core's and moves with it, and a CHECK listing the nine would have to be rebuilt again for a tenth.
--     The two optional members are admitted by name, and `json_remove` of all six must leave `{}`, so a
--     seventh key is refused -- which is what keeps a prompt, an argument or an answer from riding in on a
--     transport event that every reader of the conversation receives.
--
-- NOT additive for a consumer pinned to the 18-type vocabulary, in the same sense 0069 was not: the
-- contract's own `ChatEventType` description says an unrecognised type SHALL be treated as an
-- unknown-additive event requiring a resnapshot, never a guessed interpretation, so an older reader
-- resnapshots rather than breaking.
--
-- Rows are copied, not rewritten: the INSERT names every column explicitly and the old table is dropped
-- only once the successor holds its contents.
--
-- No comment sits inside a statement below, for the migrator statement splitter.

CREATE TABLE omnivia_chat_generation_events_0072 (
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
                          'chat.generation.cancelled',
                          'chat.generation.iteration_completed',
                          'chat.direction.state_changed',
                          'chat.tool_call.state_changed')),
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
               AND result_message_id IS NULL)
           OR (event_type = 'chat.generation.iteration_completed'
               AND generation_attempt_id IS NOT NULL AND result_message_id IS NULL
               AND provider_event_id IS NOT NULL)
           OR (event_type = 'chat.direction.state_changed'
               AND result_message_id IS NULL)
           OR (event_type = 'chat.tool_call.state_changed'
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
    CHECK (event_type <> 'chat.tool_call.state_changed'
           OR COALESCE(CASE WHEN json_valid(payload_json) IS NOT 1 THEN 0
                            ELSE
                                json_type(payload_json) = 'object'
                                AND json_type(payload_json, '$.toolCallId') = 'text'
                                AND length(json_extract(payload_json, '$.toolCallId')) BETWEEN 1 AND 128
                                AND json_extract(payload_json, '$.toolCallId') GLOB '[A-Za-z0-9]*'
                                AND json_extract(payload_json, '$.toolCallId') NOT GLOB '*[^A-Za-z0-9._:-]*'
                                AND json_type(payload_json, '$.turnId') = 'text'
                                AND length(json_extract(payload_json, '$.turnId')) BETWEEN 1 AND 128
                                AND json_type(payload_json, '$.state') = 'text'
                                AND length(json_extract(payload_json, '$.state')) BETWEEN 1 AND 128
                                AND json_type(payload_json, '$.lifecycleSequence') = 'integer'
                                AND json_extract(payload_json, '$.lifecycleSequence') BETWEEN 1 AND 1000000
                                AND (json_type(payload_json, '$.waitId') IS NULL
                                     OR json_type(payload_json, '$.waitId') = 'text')
                                AND (json_type(payload_json, '$.reasonCode') IS NULL
                                     OR json_type(payload_json, '$.reasonCode') = 'text')
                                AND json_remove(payload_json, '$.toolCallId', '$.turnId', '$.state',
                                                '$.lifecycleSequence', '$.waitId', '$.reasonCode') = '{}'
                            END, 0) = 1),
    CHECK (event_type NOT IN ('chat.generation.iteration_completed', 'chat.direction.state_changed')
           OR COALESCE(CASE WHEN json_valid(payload_json) IS NOT 1 THEN 0
                            WHEN event_type = 'chat.generation.iteration_completed' THEN
                                json_type(payload_json) = 'object'
                                AND json_type(payload_json, '$.generationJobVersion') = 'integer'
                                AND json_extract(payload_json, '$.generationJobVersion') >= 1
                                AND json_type(payload_json, '$.iteration') = 'integer'
                                AND json_extract(payload_json, '$.iteration') BETWEEN 0 AND 1000000
                                AND json_type(payload_json, '$.priorOutputDigest') = 'text'
                                AND length(json_extract(payload_json, '$.priorOutputDigest')) = 71
                                AND substr(json_extract(payload_json, '$.priorOutputDigest'), 1, 7) = 'sha256:'
                                AND substr(json_extract(payload_json, '$.priorOutputDigest'), 8) NOT GLOB '*[^0-9a-f]*'
                                AND json_remove(payload_json, '$.generationJobVersion', '$.iteration',
                                                '$.priorOutputDigest') = '{}'
                            ELSE
                                json_type(payload_json) = 'object'
                                AND json_type(payload_json, '$.directionId') = 'text'
                                AND length(json_extract(payload_json, '$.directionId')) BETWEEN 1 AND 128
                                AND json_extract(payload_json, '$.directionId') GLOB '[A-Za-z0-9]*'
                                AND json_extract(payload_json, '$.directionId') NOT GLOB '*[^A-Za-z0-9._:-]*'
                                AND json_type(payload_json, '$.directionVersion') = 'integer'
                                AND json_extract(payload_json, '$.directionVersion') BETWEEN 1 AND 1000000
                                AND json_type(payload_json, '$.state') = 'text'
                                AND json_extract(payload_json, '$.state')
                                    IN ('pending', 'applied', 'superseded', 'rejected', 'cancelled')
                                AND json_remove(payload_json, '$.directionId', '$.directionVersion', '$.state') = '{}'
                            END, 0) = 1),
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

INSERT INTO omnivia_chat_generation_events_0072 (
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

-- Two triggers from 0069 reference this table in their bodies, so the rebuild has to step around them:
-- SQLite validates a trigger body when it fires, and dropping the table out from under one leaves it
-- raising `no such table` on the next write to a completely different table. They are dropped here and
-- recreated below from 0069's own text, byte for byte -- which the guard test in
-- `test_chat_text_transport_migration.py` reproves, because it compares every live trigger against the
-- migration that last declared it.
DROP TRIGGER IF EXISTS omnivia_guard_chat_directions_update;

DROP TRIGGER IF EXISTS omnivia_guard_chat_generation_continuations_insert;

DROP TABLE omnivia_chat_generation_events;

ALTER TABLE omnivia_chat_generation_events_0072 RENAME TO omnivia_chat_generation_events;

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
    SELECT RAISE(ABORT, 'omnivia: an iteration_completed event must carry the job status projection version while it is running on the event attempt')
    WHERE NEW.event_type = 'chat.generation.iteration_completed'
      AND NEW.generation_attempt_id IS NOT NULL
      AND json_type(NEW.payload_json, '$.generationJobVersion') IS 'integer'
      AND json_extract(NEW.payload_json, '$.generationJobVersion') >= 1
      AND NOT EXISTS (
            SELECT 1 FROM omnivia_chat_generation_job_status_projection p
            WHERE p.workspace_id = NEW.workspace_id
              AND p.conversation_id = NEW.conversation_id
              AND p.generation_job_id = NEW.generation_job_id
              AND p.state = 'running'
              AND p.current_attempt_id IS NEW.generation_attempt_id
              AND p.version = json_extract(NEW.payload_json, '$.generationJobVersion'));
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_directions_update
BEFORE UPDATE ON omnivia_chat_directions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_directions')
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
    SELECT RAISE(ABORT, 'omnivia: direction identity, scope, content and requested boundary are immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.conversation_id IS NOT OLD.conversation_id
       OR NEW.branch_id IS NOT OLD.branch_id
       OR NEW.generation_job_id IS NOT OLD.generation_job_id
       OR NEW.target_attempt_id IS NOT OLD.target_attempt_id
       OR NEW.direction_id IS NOT OLD.direction_id
       OR NEW.direction_sequence IS NOT OLD.direction_sequence
       OR NEW.actor_id IS NOT OLD.actor_id
       OR NEW.content_json IS NOT OLD.content_json
       OR NEW.content_digest IS NOT OLD.content_digest
       OR NEW.requested_boundary IS NOT OLD.requested_boundary
       OR NEW.supersedes_direction_id IS NOT OLD.supersedes_direction_id
       OR NEW.schema_version IS NOT OLD.schema_version
       OR NEW.created_at_us IS NOT OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: a terminal direction is immutable')
    WHERE OLD.state <> 'pending';
    SELECT RAISE(ABORT, 'omnivia: direction transition is invalid')
    WHERE NEW.state NOT IN ('applied', 'superseded', 'rejected', 'cancelled');
    SELECT RAISE(ABORT, 'omnivia: direction version must advance by exactly one')
    WHERE NEW.version IS NOT OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: direction update time cannot move backwards')
    WHERE NEW.updated_at_us < OLD.updated_at_us;
    SELECT RAISE(ABORT, 'omnivia: an applying attempt must directly follow and retry the targeted attempt on the applied provider invocation')
    WHERE NEW.state = 'applied' AND NOT EXISTS (
        SELECT 1
        FROM omnivia_chat_generation_attempts a
        JOIN omnivia_chat_generation_attempts t
          ON t.workspace_id = a.workspace_id
         AND t.generation_job_id = a.generation_job_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.conversation_id = NEW.conversation_id
          AND a.generation_job_id = NEW.generation_job_id
          AND a.generation_attempt_id = NEW.applied_attempt_id
          AND a.provider_invocation_id = NEW.applied_provider_invocation_id
          AND a.retry_of_attempt_id = NEW.target_attempt_id
          AND t.generation_attempt_id = NEW.target_attempt_id
          AND a.attempt_number = t.attempt_number + 1);
    SELECT RAISE(ABORT, 'omnivia: a direction applies only while the job status projection is running on its target attempt')
    WHERE NEW.state = 'applied' AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_generation_job_status_projection p
        WHERE p.workspace_id = NEW.workspace_id
          AND p.conversation_id = NEW.conversation_id
          AND p.generation_job_id = NEW.generation_job_id
          AND p.state = 'running'
          AND p.current_attempt_id = NEW.target_attempt_id);
    SELECT RAISE(ABORT, 'omnivia: a direction applies only to the committed request manifest that binds it')
    WHERE NEW.state = 'applied' AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_request_manifests m
        WHERE m.workspace_id = NEW.workspace_id
          AND m.conversation_id = NEW.conversation_id
          AND m.branch_id = NEW.branch_id
          AND m.generation_job_id = NEW.generation_job_id
          AND m.generation_attempt_id = NEW.applied_attempt_id
          AND m.provider_invocation_id = NEW.applied_provider_invocation_id
          AND m.request_manifest_id = NEW.applied_request_manifest_id
          AND m.created_at_us >= OLD.created_at_us
          AND m.created_at_us <= NEW.terminal_at_us
          AND (CASE WHEN json_valid(m.request_manifest_body_json) IS NOT 1 THEN 0
                    WHEN json_type(m.request_manifest_body_json) IS NOT 'object' THEN 0
                    WHEN json(m.request_manifest_body_json) IS NOT m.request_manifest_body_json THEN 0
                    WHEN json_type(m.request_manifest_body_json, '$.direction') IS NOT 'object' THEN 0
                    WHEN json_remove(json_extract(m.request_manifest_body_json, '$.direction'),
                                     '$.contentDigest', '$.directionId', '$.directionVersion') IS NOT '{}' THEN 0
                    WHEN json_type(m.request_manifest_body_json, '$.direction.directionId') IS NOT 'text' THEN 0
                    WHEN json_type(m.request_manifest_body_json, '$.direction.directionVersion') IS NOT 'integer' THEN 0
                    WHEN json_type(m.request_manifest_body_json, '$.direction.contentDigest') IS NOT 'text' THEN 0
                    WHEN json_extract(m.request_manifest_body_json, '$.direction.directionId') IS NOT OLD.direction_id THEN 0
                    WHEN json_extract(m.request_manifest_body_json, '$.direction.directionVersion') IS NOT OLD.version THEN 0
                    WHEN json_extract(m.request_manifest_body_json, '$.direction.contentDigest') IS NOT OLD.content_digest THEN 0
                    ELSE 1 END) = 1);
    SELECT RAISE(ABORT, 'omnivia: a direction applies only after the target attempt committed its iteration_completed event for the preceding iteration')
    WHERE NEW.state = 'applied' AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_generation_events e
        WHERE e.workspace_id = NEW.workspace_id
          AND e.conversation_id = NEW.conversation_id
          AND e.generation_job_id = NEW.generation_job_id
          AND e.generation_attempt_id = NEW.target_attempt_id
          AND e.event_type = 'chat.generation.iteration_completed'
          AND e.occurred_at_us <= NEW.terminal_at_us
          AND json_extract(e.payload_json, '$.iteration') = NEW.applied_iteration - 1);
    SELECT RAISE(ABORT, 'omnivia: direction terminal reason must be an exact canonical JSON object')
    WHERE NEW.terminal_reason_json IS NOT NULL AND (CASE WHEN json_valid(NEW.terminal_reason_json) IS NOT 1 THEN 1
                WHEN json(NEW.terminal_reason_json) <> NEW.terminal_reason_json OR json_type(NEW.terminal_reason_json) <> 'object' THEN 1
                ELSE 0 END) = 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_continuations_insert
BEFORE INSERT ON omnivia_chat_generation_continuations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_continuations')
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
    SELECT RAISE(ABORT, 'omnivia: a continuation must follow an applied direction of the same job at its applied version')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_chat_directions d
        WHERE d.workspace_id = NEW.workspace_id
          AND d.conversation_id = NEW.conversation_id
          AND d.generation_job_id = NEW.generation_job_id
          AND d.direction_id = NEW.direction_id
          AND d.state = 'applied'
          AND d.version = NEW.direction_version
          AND d.target_attempt_id = NEW.prior_attempt_id
          AND d.applied_attempt_id = NEW.next_attempt_id
          AND d.terminal_at_us <= NEW.created_at_us);
    SELECT RAISE(ABORT, 'omnivia: a continuation must advance to the very next attempt that retries the prior attempt')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_chat_generation_attempts p
        JOIN omnivia_chat_generation_attempts n
          ON n.workspace_id = p.workspace_id
         AND n.generation_job_id = p.generation_job_id
        WHERE p.workspace_id = NEW.workspace_id
          AND p.conversation_id = NEW.conversation_id
          AND p.generation_job_id = NEW.generation_job_id
          AND p.generation_attempt_id = NEW.prior_attempt_id
          AND n.generation_attempt_id = NEW.next_attempt_id
          AND n.retry_of_attempt_id = p.generation_attempt_id
          AND n.attempt_number = p.attempt_number + 1);
    SELECT RAISE(ABORT, 'omnivia: a continuation must be backed by the prior attempt committed iteration_completed event')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_chat_generation_events e
        JOIN omnivia_chat_directions d
          ON d.workspace_id = e.workspace_id
         AND d.generation_job_id = e.generation_job_id
        WHERE e.workspace_id = NEW.workspace_id
          AND e.conversation_id = NEW.conversation_id
          AND e.generation_job_id = NEW.generation_job_id
          AND e.generation_attempt_id = NEW.prior_attempt_id
          AND e.event_type = 'chat.generation.iteration_completed'
          AND e.provider_event_id = NEW.provider_finish_id
          AND e.occurred_at_us <= NEW.created_at_us
          AND d.direction_id = NEW.direction_id
          AND json_extract(e.payload_json, '$.priorOutputDigest') = NEW.prior_output_digest
          AND json_extract(e.payload_json, '$.iteration') = d.applied_iteration - 1);
END;
