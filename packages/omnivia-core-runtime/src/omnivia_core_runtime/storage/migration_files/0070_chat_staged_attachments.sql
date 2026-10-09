-- Chat staged attachments (C7A; migration 0070).
--
-- Additive. One new table, `omnivia_chat_staged_attachments`, and its three guards. Nothing existing is
-- rebuilt, widened or renamed.
--
-- Authority. A row is the one durable statement that an actor has staged one already-captured file for
-- composing in one conversation. The attachment reference IS the evidence identity: `attachment_reference_id`
-- is `omnivia_evidence_artifacts.evidence_id` (both are the 1..128 character workspace-scoped identifier) and
-- `source_revision` is that artifact `content_checksum` at staging time, which 0008 makes append-only and so
-- immutable. `blob_content_digest` repeats the artifact blob address, so the bytes a reference names can be
-- re-verified without a second hop. No path, URL, raw bytes, credential or renderer handle is stored, and none
-- can be: every column is an identifier, a digest, a state or an instant.
--
-- Scope. A row is keyed by workspace, conversation, actor and reference, so one file is staged independently
-- by each actor in each conversation. `draft_id` is optional. When present it binds the row to one existing
-- draft of that same conversation and actor, and it can change only when a removed row is staged again.
--
-- Lifecycle. A row is inserted `active` at version one. The only legal changes are `active` to `removed`
-- (stamping `removed_at_us` with the update instant) and `removed` to `active` (clearing it), each advancing
-- `version` by exactly one. Identity, scope, `source_revision`, `blob_content_digest` and `staged_at_us` never
-- change. DELETE is refused for everyone, so a removed row stays as the record that the reference was once
-- staged. A removed row survives restart exactly as an active one does.
--
-- Bounds the database holds and the service does not have to. A conversation must be `active`. The evidence
-- artifact must exist in the same workspace with exactly this `source_revision` and blob address, and its
-- latest non-null tombstone observation must not be a tombstone. An actor holds at most 64 active rows per
-- conversation. A draft binding must name a draft of the same conversation and actor. The service still owns
-- what the database cannot see: media type, size, physical blob presence and blob integrity.
--
-- Every write runs inside the caller fenced_transaction. The guards carry the same connection-authority, guard,
-- workspace-state and lease predicate as the other guarded tables and bind each row to the open workspace.
--
-- No comment sits inside a statement below, for the migrator statement splitter.

CREATE TABLE IF NOT EXISTS omnivia_chat_staged_attachments (
    workspace_id            TEXT    NOT NULL,
    conversation_id         TEXT    NOT NULL,
    actor_id                TEXT    NOT NULL,
    attachment_reference_id TEXT    NOT NULL,
    draft_id                TEXT,
    source_revision         TEXT    NOT NULL,
    blob_content_digest     TEXT    NOT NULL,
    state                   TEXT    NOT NULL,
    version                 INTEGER NOT NULL,
    staged_at_us            INTEGER NOT NULL,
    updated_at_us           INTEGER NOT NULL,
    removed_at_us           INTEGER,

    PRIMARY KEY (workspace_id, conversation_id, actor_id, attachment_reference_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text' AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(actor_id) = 'text' AND length(actor_id) BETWEEN 1 AND 128
           AND actor_id GLOB '[A-Za-z0-9]*'
           AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(actor_id, char(0)) = 0),
    CHECK (typeof(attachment_reference_id) = 'text'
           AND length(attachment_reference_id) BETWEEN 1 AND 128
           AND attachment_reference_id GLOB '[A-Za-z0-9]*'
           AND attachment_reference_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(attachment_reference_id, char(0)) = 0),
    CHECK (draft_id IS NULL
           OR (typeof(draft_id) = 'text' AND length(draft_id) BETWEEN 1 AND 128
               AND draft_id GLOB '[A-Za-z0-9]*'
               AND draft_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(draft_id, char(0)) = 0)),
    CHECK (typeof(source_revision) = 'text' AND length(source_revision) BETWEEN 1 AND 128
           AND instr(source_revision, char(0)) = 0),
    CHECK (typeof(blob_content_digest) = 'text' AND length(blob_content_digest) = 71
           AND substr(blob_content_digest, 1, 7) = 'sha256:'
           AND substr(blob_content_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(blob_content_digest, char(0)) = 0),
    CHECK (state IN ('active', 'removed')),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(staged_at_us) = 'integer' AND staged_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= staged_at_us),
    CHECK ((state = 'active' AND removed_at_us IS NULL)
           OR (state = 'removed' AND typeof(removed_at_us) = 'integer'
               AND removed_at_us >= staged_at_us AND removed_at_us = updated_at_us)),

    FOREIGN KEY (attachment_reference_id, workspace_id)
        REFERENCES omnivia_evidence_artifacts (evidence_id, workspace_id),
    FOREIGN KEY (workspace_id, blob_content_digest)
        REFERENCES omnivia_blob_objects (workspace_id, content_digest),
    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, draft_id)
        REFERENCES omnivia_chat_drafts (workspace_id, draft_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_staged_attachments_insert
BEFORE INSERT ON omnivia_chat_staged_attachments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_staged_attachments')
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
    SELECT RAISE(ABORT, 'omnivia: staged attachments are inserted only active at version one')
    WHERE NEW.state <> 'active' OR NEW.version <> 1 OR NEW.updated_at_us <> NEW.staged_at_us;
    SELECT RAISE(ABORT, 'omnivia: a staged attachment needs an active conversation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_chat_conversations c
        WHERE c.workspace_id = NEW.workspace_id
          AND c.conversation_id = NEW.conversation_id
          AND c.state = 'active');
    SELECT RAISE(ABORT, 'omnivia: a staged attachment must pin its evidence revision and blob address')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts a
        WHERE a.evidence_id = NEW.attachment_reference_id
          AND a.workspace_id = NEW.workspace_id
          AND a.content_checksum = NEW.source_revision
          AND a.blob_content_digest = NEW.blob_content_digest);
    SELECT RAISE(ABORT, 'omnivia: a tombstoned evidence artifact cannot be staged')
    WHERE (SELECT p.tombstoned_observation FROM omnivia_evidence_provenance_events p
           WHERE p.workspace_id = NEW.workspace_id
             AND p.evidence_id = NEW.attachment_reference_id
             AND p.tombstoned_observation IS NOT NULL
           ORDER BY p.provenance_sequence DESC LIMIT 1) IS 1;
    SELECT RAISE(ABORT, 'omnivia: an actor stages at most 64 active attachments per conversation')
    WHERE (SELECT COUNT(*) FROM omnivia_chat_staged_attachments q
           WHERE q.workspace_id = NEW.workspace_id
             AND q.conversation_id = NEW.conversation_id
             AND q.actor_id = NEW.actor_id
             AND q.state = 'active') >= 64;
    SELECT RAISE(ABORT, 'omnivia: a draft binding must name a draft of the same conversation and actor')
    WHERE NEW.draft_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_drafts d
        WHERE d.workspace_id = NEW.workspace_id
          AND d.draft_id = NEW.draft_id
          AND d.conversation_id = NEW.conversation_id
          AND d.actor_id = NEW.actor_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_staged_attachments_update
BEFORE UPDATE ON omnivia_chat_staged_attachments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_staged_attachments')
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
       OR NEW.workspace_id <> OLD.workspace_id;
    SELECT RAISE(ABORT, 'omnivia: staged attachment identity, scope, revision and staging time are immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.actor_id <> OLD.actor_id
       OR NEW.attachment_reference_id <> OLD.attachment_reference_id
       OR NEW.source_revision <> OLD.source_revision
       OR NEW.blob_content_digest <> OLD.blob_content_digest
       OR NEW.staged_at_us <> OLD.staged_at_us;
    SELECT RAISE(ABORT, 'omnivia: staged attachment version must advance by one')
    WHERE NEW.version <> OLD.version + 1 OR NEW.updated_at_us < OLD.updated_at_us;
    SELECT RAISE(ABORT, 'omnivia: a staged attachment moves only between active and removed')
    WHERE NEW.state = OLD.state;
    SELECT RAISE(ABORT, 'omnivia: a staged attachment draft binding changes only when it is staged again')
    WHERE NEW.draft_id IS NOT OLD.draft_id AND NOT (OLD.state = 'removed' AND NEW.state = 'active');
    SELECT RAISE(ABORT, 'omnivia: a staged attachment needs an active conversation')
    WHERE NEW.state = 'active' AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_conversations c
        WHERE c.workspace_id = NEW.workspace_id
          AND c.conversation_id = NEW.conversation_id
          AND c.state = 'active');
    SELECT RAISE(ABORT, 'omnivia: a staged attachment must pin its evidence revision and blob address')
    WHERE NEW.state = 'active' AND NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts a
        WHERE a.evidence_id = NEW.attachment_reference_id
          AND a.workspace_id = NEW.workspace_id
          AND a.content_checksum = NEW.source_revision
          AND a.blob_content_digest = NEW.blob_content_digest);
    SELECT RAISE(ABORT, 'omnivia: a tombstoned evidence artifact cannot be staged')
    WHERE NEW.state = 'active'
      AND (SELECT p.tombstoned_observation FROM omnivia_evidence_provenance_events p
           WHERE p.workspace_id = NEW.workspace_id
             AND p.evidence_id = NEW.attachment_reference_id
             AND p.tombstoned_observation IS NOT NULL
           ORDER BY p.provenance_sequence DESC LIMIT 1) IS 1;
    SELECT RAISE(ABORT, 'omnivia: an actor stages at most 64 active attachments per conversation')
    WHERE NEW.state = 'active'
      AND (SELECT COUNT(*) FROM omnivia_chat_staged_attachments q
           WHERE q.workspace_id = NEW.workspace_id
             AND q.conversation_id = NEW.conversation_id
             AND q.actor_id = NEW.actor_id
             AND q.state = 'active') >= 64;
    SELECT RAISE(ABORT, 'omnivia: a draft binding must name a draft of the same conversation and actor')
    WHERE NEW.state = 'active' AND NEW.draft_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_drafts d
        WHERE d.workspace_id = NEW.workspace_id
          AND d.draft_id = NEW.draft_id
          AND d.conversation_id = NEW.conversation_id
          AND d.actor_id = NEW.actor_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_staged_attachments_delete
BEFORE DELETE ON omnivia_chat_staged_attachments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_staged_attachments forbids DELETE');
END;
