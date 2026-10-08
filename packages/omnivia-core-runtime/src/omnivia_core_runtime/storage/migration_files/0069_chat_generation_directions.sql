-- Chat generation Directions and attempt continuations (C6; migration 0069).
--
-- Additive except for one widening: `omnivia_chat_generation_events` is rebuilt first so that it admits
-- `chat.generation.iteration_completed` and `chat.direction.state_changed`. Everything else in that table,
-- its index and its three guards is carried across from 0034 unchanged. The rebuild comes first because the
-- guards below read the events table by name, and SQLite refuses a rename while a trigger names a table
-- that is momentarily absent.
--
-- Authority. The job state, current Attempt and public job CAS token are those of 0030
-- `omnivia_chat_generation_job_status_projection` and nothing else; this migration adds no version column
-- to any 0029 table. The Direction `version` below is the Direction own CAS token, unrelated to the job.
--
-- `omnivia_chat_directions` holds one durable Direction per row: bounded canonical content aimed at
-- one generation Attempt (the target) of one Job. Directions are numbered contiguously from one within a
-- Job and at most one is `pending`. A Direction is inserted only while the status projection row of its Job
-- is exactly `running` and its `current_attempt_id` is the target Attempt. The only legal change to a row is
-- one guarded transition out of `pending` to `applied`, `superseded`, `rejected` or `cancelled`, advancing
-- `version` from 1 to 2. A terminal row never changes again. Identity, scope, content, digest and
-- `requested_boundary` are immutable from insert.
--
-- Closed boundary constants. `requested_boundary` is exactly `earliest_safe` on every Direction.
-- `effective_boundary` is NULL unless the Direction is `applied`, and exactly `next_iteration` then.
--
-- Application proof. An `applied` Direction records, all NOT NULL and all frozen by the terminal rule:
--   applied_attempt_id             the applying Attempt, number target+1 with retry_of = the target
--   applied_provider_invocation_id the applying Attempt provider invocation
--   applied_request_manifest_id    the 0031 request manifest of the applying Attempt
--   applied_iteration              zero-based iteration at which the Direction takes effect, completed
--                                  iteration + 1, so at least 1
--   effective_boundary             next_iteration
--   terminal_at_us                 is the application time (also updated_at_us)
-- The transition to `applied` is refused unless, at that moment: the status projection is `running` on the
-- target Attempt (the executor advances current_attempt_id to the applying Attempt with the public job CAS
-- only after application); the applying Attempt carries that provider invocation; the 0031 manifest row for
-- exactly that workspace, conversation, branch, job, Attempt, provider invocation and manifest id exists
-- with created_at_us between the Direction creation and the application time; and the target Attempt has a
-- `chat.generation.iteration_completed` event whose payload `iteration` is applied_iteration - 1.
--
-- Manifest binding shape (the executor must emit exactly this in request_manifest_body_json):
--   "direction": {"contentDigest": "sha256:<64 hex>", "directionId": "<id>", "directionVersion": 1}
-- a top-level object of exactly those three keys, inside a request_manifest_body_json that is itself a
-- canonical JSON object (SQLite json(body) = body). directionId and contentDigest equal the Direction row.
-- directionVersion is the integer pending version the manifest was built from, which is always 1; the
-- Direction becomes version 2 by being applied.
--
-- Supersession is two writes in one transaction, old row first because a Job may not hold two pending rows:
-- the old row becomes `superseded` naming its successor, then the successor is inserted naming the old row
-- in `supersedes_direction_id`. The successor insert guard requires the old row to be superseded by exactly
-- that identity. The deferred composite foreign key `superseded_by` -> (successor id, successor
-- supersedes_direction_id = this id) is checked at COMMIT: a superseded row whose successor is absent, or
-- does not name it back, makes the commit fail and the transaction roll back.
--
-- `omnivia_chat_generation_continuations` is the append-only record that one Attempt continued as the next
-- under one applied Direction. Its insert guard requires: the Direction applied at exactly the recorded
-- version, by exactly the next Attempt, aimed at exactly the prior Attempt; next number = prior + 1 and
-- next.retry_of_attempt_id = prior; and a committed iteration_completed event of the prior Attempt whose
-- provider_event_id is provider_finish_id, whose payload priorOutputDigest is prior_output_digest and whose
-- payload iteration is applied_iteration - 1, recorded no later than the continuation.
--
-- Event payload shapes (closed, identity-only, no raw provider output, enforced by CHECK on the events
-- table, producers emit keys in this sorted order):
--   chat.generation.iteration_completed  generation_attempt_id and provider_event_id NOT NULL
--       {"generationJobVersion": <int >=1>, "iteration": <int 0..1000000>,
--        "priorOutputDigest": "sha256:<64 hex>"}
--       generationJobVersion is the immutable event-time evidence of the public job CAS token: the insert
--       guard requires it to equal the 0030 status projection version of the same Job while that projection
--       is `running` on this event Attempt. It is never reconstructed later.
--   chat.direction.state_changed         attempt optional, no result message
--       {"directionId": "<id>", "directionVersion": <int 1..1000000>, "state": "<direction state>"}
--
-- Digests. content_digest, priorOutputDigest and the manifest contentDigest are shaped as
-- sha256:<64 lower hex> and compared only as strings. SQLite has no SHA-256 primitive, so the database
-- cannot prove content_digest is the hash of content_json. content_json is the canonical JSON object
-- {"editableParts":[...]} (sorted keys, compact separators, non-ASCII emitted directly). content_digest is
-- "sha256:" plus the SHA-256 of the exact stored UTF-8 bytes of that object. The public contract contentHash
-- is the same 64 hex without the prefix; the manifest contentDigest carries the prefixed form. The SQL is
-- shape-agnostic beyond canonical-object-only; admission owns the public editableParts shape. Storage and
-- service readers, and the manifest builder, must recompute the digest and fail closed on any difference.
--
-- Committed. Existence checks see the writing transaction, so a manifest or event written earlier in the
-- same transaction counts and rolls back with it. SQL cannot distinguish a row committed in an earlier
-- transaction from one inserted earlier in the same transaction. The database therefore requires the row to
-- exist before the transition statement and validates it atomically with that statement; the service lane
-- sequences the transaction boundary.
--
-- Every write runs inside the caller fenced_transaction. The INSERT guards carry the same connection-
-- authority, guard, workspace-state and lease predicate as the other guarded tables and bind each row to
-- the open workspace. DELETE is refused for everyone.
--
-- No comment sits inside a statement below, for the migrator statement splitter.

CREATE TABLE omnivia_chat_generation_events_0069 (
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
                          'chat.direction.state_changed')),
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

INSERT INTO omnivia_chat_generation_events_0069 (
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

ALTER TABLE omnivia_chat_generation_events_0069 RENAME TO omnivia_chat_generation_events;

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

CREATE TABLE IF NOT EXISTS omnivia_chat_directions (
    workspace_id                    TEXT    NOT NULL,
    conversation_id                 TEXT    NOT NULL,
    branch_id                       TEXT    NOT NULL,
    generation_job_id               TEXT    NOT NULL,
    target_attempt_id               TEXT    NOT NULL,
    direction_id                    TEXT    NOT NULL,
    direction_sequence              INTEGER NOT NULL,
    actor_id                        TEXT    NOT NULL,
    state                           TEXT    NOT NULL,
    version                         INTEGER NOT NULL,
    content_json                    TEXT    NOT NULL,
    content_digest                  TEXT    NOT NULL,
    requested_boundary              TEXT    NOT NULL,
    effective_boundary              TEXT,
    supersedes_direction_id         TEXT,
    superseded_by_direction_id      TEXT,
    applied_attempt_id              TEXT,
    applied_provider_invocation_id  TEXT,
    applied_request_manifest_id     TEXT,
    applied_iteration               INTEGER,
    terminal_reason_json            TEXT,
    schema_version                  INTEGER NOT NULL,
    created_at_us                   INTEGER NOT NULL,
    updated_at_us                   INTEGER NOT NULL,
    terminal_at_us                  INTEGER,

    PRIMARY KEY (workspace_id, direction_id),
    UNIQUE (workspace_id, generation_job_id, direction_sequence),
    UNIQUE (workspace_id, generation_job_id, direction_id),
    UNIQUE (workspace_id, generation_job_id, direction_id, supersedes_direction_id),
    UNIQUE (workspace_id, generation_job_id, applied_attempt_id),
    UNIQUE (workspace_id, applied_request_manifest_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text' AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(branch_id) = 'text' AND length(branch_id) BETWEEN 1 AND 128
           AND branch_id GLOB '[A-Za-z0-9]*'
           AND branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(branch_id, char(0)) = 0),
    CHECK (typeof(generation_job_id) = 'text' AND length(generation_job_id) BETWEEN 1 AND 128
           AND generation_job_id GLOB '[A-Za-z0-9]*'
           AND generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_job_id, char(0)) = 0),
    CHECK (typeof(target_attempt_id) = 'text' AND length(target_attempt_id) BETWEEN 1 AND 128
           AND target_attempt_id GLOB '[A-Za-z0-9]*'
           AND target_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(target_attempt_id, char(0)) = 0),
    CHECK (typeof(direction_id) = 'text' AND length(direction_id) BETWEEN 1 AND 128
           AND direction_id GLOB '[A-Za-z0-9]*'
           AND direction_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(direction_id, char(0)) = 0),
    CHECK (typeof(direction_sequence) = 'integer'
           AND direction_sequence BETWEEN 1 AND 1000000),
    CHECK (typeof(actor_id) = 'text' AND length(actor_id) BETWEEN 1 AND 128
           AND actor_id GLOB '[A-Za-z0-9]*'
           AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(actor_id, char(0)) = 0),
    CHECK (state IN ('pending', 'applied', 'superseded', 'rejected', 'cancelled')),
    CHECK (typeof(version) = 'integer' AND version BETWEEN 1 AND 1000000),
    CHECK ((state = 'pending' AND version = 1) OR (state <> 'pending' AND version >= 2)),
    CHECK (typeof(content_json) = 'text'
           AND length(CAST(content_json AS BLOB)) BETWEEN 2 AND 16384
           AND instr(content_json, char(0)) = 0),
    CHECK (typeof(content_digest) = 'text' AND length(content_digest) = 71
           AND substr(content_digest, 1, 7) = 'sha256:'
           AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(requested_boundary) = 'text' AND requested_boundary = 'earliest_safe'),
    CHECK (effective_boundary IS NULL
           OR (typeof(effective_boundary) = 'text' AND effective_boundary = 'next_iteration')),
    CHECK (supersedes_direction_id IS NULL OR (typeof(supersedes_direction_id) = 'text' AND length(supersedes_direction_id) BETWEEN 1 AND 128
               AND supersedes_direction_id GLOB '[A-Za-z0-9]*'
               AND supersedes_direction_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(supersedes_direction_id, char(0)) = 0)),
    CHECK (supersedes_direction_id IS NULL OR supersedes_direction_id <> direction_id),
    CHECK (superseded_by_direction_id IS NULL OR (typeof(superseded_by_direction_id) = 'text' AND length(superseded_by_direction_id) BETWEEN 1 AND 128
               AND superseded_by_direction_id GLOB '[A-Za-z0-9]*'
               AND superseded_by_direction_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(superseded_by_direction_id, char(0)) = 0)),
    CHECK (superseded_by_direction_id IS NULL OR superseded_by_direction_id <> direction_id),
    CHECK (applied_attempt_id IS NULL OR (typeof(applied_attempt_id) = 'text' AND length(applied_attempt_id) BETWEEN 1 AND 128
               AND applied_attempt_id GLOB '[A-Za-z0-9]*'
               AND applied_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(applied_attempt_id, char(0)) = 0)),
    CHECK (applied_attempt_id IS NULL OR applied_attempt_id <> target_attempt_id),
    CHECK (applied_provider_invocation_id IS NULL OR (typeof(applied_provider_invocation_id) = 'text' AND length(applied_provider_invocation_id) BETWEEN 1 AND 128
               AND applied_provider_invocation_id GLOB '[A-Za-z0-9]*'
               AND applied_provider_invocation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(applied_provider_invocation_id, char(0)) = 0)),
    CHECK (applied_request_manifest_id IS NULL OR (typeof(applied_request_manifest_id) = 'text' AND length(applied_request_manifest_id) BETWEEN 1 AND 128
               AND applied_request_manifest_id GLOB '[A-Za-z0-9]*'
               AND applied_request_manifest_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(applied_request_manifest_id, char(0)) = 0)),
    CHECK (applied_iteration IS NULL
           OR (typeof(applied_iteration) = 'integer' AND applied_iteration BETWEEN 1 AND 1000000)),
    CHECK (terminal_reason_json IS NULL OR (typeof(terminal_reason_json) = 'text'
           AND length(CAST(terminal_reason_json AS BLOB)) BETWEEN 2 AND 4096
           AND instr(terminal_reason_json, char(0)) = 0)),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (terminal_at_us IS NULL OR (typeof(terminal_at_us) = 'integer'
           AND terminal_at_us >= created_at_us AND terminal_at_us = updated_at_us)),
    CHECK ((state = 'pending' AND terminal_at_us IS NULL)
           OR (state <> 'pending' AND terminal_at_us IS NOT NULL)),
    CHECK ((state = 'applied'
            AND applied_attempt_id IS NOT NULL
            AND applied_provider_invocation_id IS NOT NULL
            AND applied_request_manifest_id IS NOT NULL
            AND applied_iteration IS NOT NULL
            AND effective_boundary IS 'next_iteration')
           OR (state <> 'applied'
               AND applied_attempt_id IS NULL
               AND applied_provider_invocation_id IS NULL
               AND applied_request_manifest_id IS NULL
               AND applied_iteration IS NULL
               AND effective_boundary IS NULL)),
    CHECK ((state = 'superseded' AND superseded_by_direction_id IS NOT NULL)
           OR (state <> 'superseded' AND superseded_by_direction_id IS NULL)),
    CHECK ((state IN ('superseded', 'rejected', 'cancelled')
            AND terminal_reason_json IS NOT NULL)
           OR (state IN ('pending', 'applied') AND terminal_reason_json IS NULL)),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, conversation_id, branch_id)
        REFERENCES omnivia_chat_message_branches
            (workspace_id, conversation_id, branch_id),
    FOREIGN KEY (workspace_id, generation_job_id, target_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id),
    FOREIGN KEY (workspace_id, generation_job_id, applied_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id),
    FOREIGN KEY (workspace_id, applied_provider_invocation_id)
        REFERENCES omnivia_provider_invocations (workspace_id, invocation_id),
    FOREIGN KEY (workspace_id, applied_request_manifest_id)
        REFERENCES omnivia_chat_request_manifests (workspace_id, request_manifest_id),
    FOREIGN KEY (workspace_id, generation_job_id, supersedes_direction_id)
        REFERENCES omnivia_chat_directions (workspace_id, generation_job_id, direction_id),
    FOREIGN KEY (workspace_id, generation_job_id, superseded_by_direction_id, direction_id)
        REFERENCES omnivia_chat_directions
            (workspace_id, generation_job_id, direction_id, supersedes_direction_id)
        DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_continuations (
    workspace_id                 TEXT    NOT NULL,
    conversation_id              TEXT    NOT NULL,
    generation_job_id            TEXT    NOT NULL,
    continuation_id              TEXT    NOT NULL,
    prior_attempt_id             TEXT    NOT NULL,
    next_attempt_id              TEXT    NOT NULL,
    direction_id                 TEXT    NOT NULL,
    direction_version            INTEGER NOT NULL,
    provider_finish_id           TEXT    NOT NULL,
    prior_output_digest          TEXT    NOT NULL,
    schema_version               INTEGER NOT NULL,
    created_at_us                INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, continuation_id),
    UNIQUE (workspace_id, generation_job_id, next_attempt_id),
    UNIQUE (workspace_id, generation_job_id, direction_id),

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
    CHECK (typeof(continuation_id) = 'text' AND length(continuation_id) BETWEEN 1 AND 128
           AND continuation_id GLOB '[A-Za-z0-9]*'
           AND continuation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(continuation_id, char(0)) = 0),
    CHECK (typeof(prior_attempt_id) = 'text' AND length(prior_attempt_id) BETWEEN 1 AND 128
           AND prior_attempt_id GLOB '[A-Za-z0-9]*'
           AND prior_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(prior_attempt_id, char(0)) = 0),
    CHECK (typeof(next_attempt_id) = 'text' AND length(next_attempt_id) BETWEEN 1 AND 128
           AND next_attempt_id GLOB '[A-Za-z0-9]*'
           AND next_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(next_attempt_id, char(0)) = 0),
    CHECK (prior_attempt_id <> next_attempt_id),
    CHECK (typeof(direction_id) = 'text' AND length(direction_id) BETWEEN 1 AND 128
           AND direction_id GLOB '[A-Za-z0-9]*'
           AND direction_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(direction_id, char(0)) = 0),
    CHECK (typeof(direction_version) = 'integer' AND direction_version BETWEEN 2 AND 1000000),
    CHECK (typeof(provider_finish_id) = 'text' AND length(provider_finish_id) BETWEEN 1 AND 128
           AND provider_finish_id GLOB '[A-Za-z0-9]*'
           AND provider_finish_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_finish_id, char(0)) = 0),
    CHECK (typeof(prior_output_digest) = 'text' AND length(prior_output_digest) = 71
           AND substr(prior_output_digest, 1, 7) = 'sha256:'
           AND substr(prior_output_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, generation_job_id, prior_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id),
    FOREIGN KEY (workspace_id, generation_job_id, next_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id),
    FOREIGN KEY (workspace_id, generation_job_id, direction_id)
        REFERENCES omnivia_chat_directions (workspace_id, generation_job_id, direction_id)
) WITHOUT ROWID;

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_chat_directions_one_pending
    ON omnivia_chat_directions (workspace_id, generation_job_id)
    WHERE state = 'pending';

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_directions_insert
BEFORE INSERT ON omnivia_chat_directions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_directions')
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
    SELECT RAISE(ABORT, 'omnivia: directions are inserted only in pending state at version one')
    WHERE NEW.state <> 'pending' OR NEW.version <> 1;
    SELECT RAISE(ABORT, 'omnivia: directions must be contiguous from one within their job')
    WHERE NEW.direction_sequence IS NOT (
        SELECT COALESCE(MAX(direction_sequence), 0) + 1
        FROM omnivia_chat_directions
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id);
    SELECT RAISE(ABORT, 'omnivia: a direction must name the conversation and branch of its job')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_chat_generation_jobs j
        WHERE j.workspace_id = NEW.workspace_id
          AND j.conversation_id = NEW.conversation_id
          AND j.generation_job_id = NEW.generation_job_id
          AND j.branch_id = NEW.branch_id
          AND j.created_at_us <= NEW.created_at_us);
    SELECT RAISE(ABORT, 'omnivia: a direction needs a running job status projection on its target attempt')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_chat_generation_job_status_projection p
        WHERE p.workspace_id = NEW.workspace_id
          AND p.conversation_id = NEW.conversation_id
          AND p.generation_job_id = NEW.generation_job_id
          AND p.state = 'running'
          AND p.current_attempt_id = NEW.target_attempt_id);
    SELECT RAISE(ABORT, 'omnivia: direction content must be an exact canonical JSON object')
    WHERE (CASE WHEN json_valid(NEW.content_json) IS NOT 1 THEN 1
                WHEN json(NEW.content_json) <> NEW.content_json OR json_type(NEW.content_json) <> 'object' THEN 1
                ELSE 0 END) = 1;
    SELECT RAISE(ABORT, 'omnivia: a successor direction must replace a direction already superseded by it')
    WHERE NEW.supersedes_direction_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_directions p
        WHERE p.workspace_id = NEW.workspace_id
          AND p.generation_job_id = NEW.generation_job_id
          AND p.direction_id = NEW.supersedes_direction_id
          AND p.state = 'superseded'
          AND p.superseded_by_direction_id = NEW.direction_id
          AND p.direction_sequence < NEW.direction_sequence);
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_directions_delete
BEFORE DELETE ON omnivia_chat_directions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_directions forbids DELETE');
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_continuations_update
BEFORE UPDATE ON omnivia_chat_generation_continuations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_continuations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_continuations_delete
BEFORE DELETE ON omnivia_chat_generation_continuations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_continuations forbids DELETE');
END;
