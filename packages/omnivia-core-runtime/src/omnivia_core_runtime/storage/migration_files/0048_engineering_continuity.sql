-- Engineering continuity records (SPEC-CORE-ENGMEM-001, plan PR-B; spec §7, §9).
--
-- Additive only; 0047 (repository identity) remains reserved ahead of this file.
-- The families:
--
--   omnivia_engineering_sessions     one continuity session binding: the
--                                    service-issued operational bookkeeping of
--                                    §7. Identity is immutable; only the state
--                                    and the last-acknowledged checkpoint
--                                    pointer may settle, forwards in time. The
--                                    binding generation recorded here is the
--                                    session's own; the authoritative workspace
--                                    writer generation is enforced separately by
--                                    the guard triggers below, and every write
--                                    checks both.
--   omnivia_engineering_checkpoints  one immutable L0 checkpoint: the structured
--                                    payload is stored whole (never truncated),
--                                    digest-identified, strictly append-only,
--                                    with a per-session contiguous sequence.
--                                    A successful row IS the durable receipt's
--                                    backing record; a replayed idempotency key
--                                    returns it via the mutation coordinator
--                                    without a second row.
--
-- A stored checkpoint is evidence, not accepted knowledge: nothing here writes
-- governed records or governance state.

CREATE TABLE IF NOT EXISTS omnivia_engineering_sessions (
    workspace_id            TEXT    NOT NULL,
    session_id              TEXT    NOT NULL,
    principal_id            TEXT    NOT NULL,
    state                   TEXT    NOT NULL,
    binding_generation      INTEGER NOT NULL,
    lease_expires_at_us     INTEGER NOT NULL,
    host_session_ref        TEXT,
    checkout_hint           TEXT,
    repository_target_json  TEXT,
    registered_at_us        INTEGER NOT NULL,
    closed_at_us            INTEGER,
    last_checkpoint_sequence INTEGER,
    last_checkpoint_id      TEXT,
    audit_ref               TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, session_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(session_id) = 'text' AND length(session_id) BETWEEN 1 AND 128
           AND session_id GLOB '[A-Za-z0-9]*'
           AND session_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(session_id, char(0)) = 0),
    CHECK (typeof(principal_id) = 'text'
           AND length(principal_id) BETWEEN 1 AND 128),
    CHECK (state IN ('active', 'closed', 'expired', 'revoked')),
    CHECK (typeof(binding_generation) = 'integer' AND binding_generation > 0),
    CHECK (typeof(lease_expires_at_us) = 'integer' AND lease_expires_at_us > 0),
    CHECK (host_session_ref IS NULL OR (typeof(host_session_ref) = 'text'
           AND length(host_session_ref) BETWEEN 1 AND 512)),
    CHECK (checkout_hint IS NULL OR (typeof(checkout_hint) = 'text'
           AND length(checkout_hint) BETWEEN 1 AND 512)),
    CHECK (repository_target_json IS NULL
           OR (typeof(repository_target_json) = 'text'
               AND length(CAST(repository_target_json AS BLOB)) BETWEEN 2 AND 8192
               AND json_valid(repository_target_json) = 1
               AND json(repository_target_json) = repository_target_json)),
    CHECK (typeof(registered_at_us) = 'integer' AND registered_at_us > 0),
    CHECK (closed_at_us IS NULL
           OR (typeof(closed_at_us) = 'integer' AND closed_at_us > 0)),
    CHECK (last_checkpoint_sequence IS NULL
           OR (typeof(last_checkpoint_sequence) = 'integer'
               AND last_checkpoint_sequence > 0)),
    CHECK (last_checkpoint_id IS NULL
           OR (typeof(last_checkpoint_id) = 'text' AND length(last_checkpoint_id) BETWEEN 1 AND 128
               AND last_checkpoint_id GLOB '[A-Za-z0-9]*'
               AND last_checkpoint_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(last_checkpoint_id, char(0)) = 0)),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK ((state = 'closed') = (closed_at_us IS NOT NULL)),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_checkpoints (
    workspace_id          TEXT    NOT NULL,
    checkpoint_id         TEXT    NOT NULL,
    session_id            TEXT    NOT NULL,
    sequence              INTEGER NOT NULL,
    parent_checkpoint_id  TEXT,
    checkpoint_kind       TEXT    NOT NULL,
    payload_json          TEXT    NOT NULL,
    content_digest        TEXT    NOT NULL,
    recorded_at_us        INTEGER NOT NULL,
    audit_ref             TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, checkpoint_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(checkpoint_id) = 'text' AND length(checkpoint_id) BETWEEN 1 AND 128
           AND checkpoint_id GLOB '[A-Za-z0-9]*'
           AND checkpoint_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(checkpoint_id, char(0)) = 0),
    CHECK (typeof(sequence) = 'integer' AND sequence > 0),
    CHECK (parent_checkpoint_id IS NULL
           OR (typeof(parent_checkpoint_id) = 'text'
               AND length(parent_checkpoint_id) BETWEEN 1 AND 128
               AND parent_checkpoint_id GLOB '[A-Za-z0-9]*'
               AND parent_checkpoint_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(parent_checkpoint_id, char(0)) = 0)),
    CHECK (typeof(checkpoint_kind) = 'text' AND length(checkpoint_kind) BETWEEN 1 AND 64
           AND checkpoint_kind GLOB '[a-z_]*'
           AND checkpoint_kind NOT GLOB '*[^a-z_]*'),
    CHECK (typeof(payload_json) = 'text'
           AND length(CAST(payload_json AS BLOB)) BETWEEN 2 AND 262144
           AND json_valid(payload_json) = 1 AND json(payload_json) = payload_json),
    CHECK (typeof(content_digest) = 'text' AND length(content_digest) = 71
           AND substr(content_digest, 1, 7) = 'sha256:'
           AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    UNIQUE (workspace_id, session_id, sequence),

    FOREIGN KEY (workspace_id, session_id)
        REFERENCES omnivia_engineering_sessions (workspace_id, session_id),
    FOREIGN KEY (workspace_id, parent_checkpoint_id)
        REFERENCES omnivia_engineering_checkpoints (workspace_id, checkpoint_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_sessions_principal
    ON omnivia_engineering_sessions (workspace_id, principal_id, registered_at_us);
CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_checkpoints_session
    ON omnivia_engineering_checkpoints (workspace_id, session_id, sequence);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_sessions_insert
BEFORE INSERT ON omnivia_engineering_sessions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_sessions')
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
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_sessions_update
BEFORE UPDATE ON omnivia_engineering_sessions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_engineering_sessions')
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
    SELECT RAISE(ABORT, 'omnivia: engineering session identity is immutable; only the state and checkpoint pointer may settle')
    WHERE NEW.principal_id IS NOT OLD.principal_id
       OR NEW.binding_generation IS NOT OLD.binding_generation
       OR NEW.lease_expires_at_us IS NOT OLD.lease_expires_at_us
       OR NEW.host_session_ref IS NOT OLD.host_session_ref
       OR NEW.checkout_hint IS NOT OLD.checkout_hint
       OR NEW.repository_target_json IS NOT OLD.repository_target_json
       OR NEW.registered_at_us IS NOT OLD.registered_at_us
       OR NEW.audit_ref IS NOT OLD.audit_ref
       OR (OLD.state = 'closed'
           AND (NEW.state IS NOT OLD.state
                OR NEW.closed_at_us IS NOT OLD.closed_at_us
                OR NEW.last_checkpoint_sequence IS NOT OLD.last_checkpoint_sequence
                OR NEW.last_checkpoint_id IS NOT OLD.last_checkpoint_id));
    SELECT RAISE(ABORT, 'omnivia: an engineering session cannot leave a terminal state')
    WHERE OLD.state IN ('closed', 'revoked')
      AND NEW.state IS NOT OLD.state;
    SELECT RAISE(ABORT, 'omnivia: the engineering session checkpoint pointer may only advance')
    WHERE NEW.last_checkpoint_sequence IS NOT NULL
      AND OLD.last_checkpoint_sequence IS NOT NULL
      AND NEW.last_checkpoint_sequence < OLD.last_checkpoint_sequence;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_sessions_delete
BEFORE DELETE ON omnivia_engineering_sessions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_sessions is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_checkpoints_insert
BEFORE INSERT ON omnivia_engineering_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_checkpoints')
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
    SELECT RAISE(ABORT, 'omnivia: an engineering checkpoint sequence must be contiguous within its session')
    WHERE NEW.sequence IS NOT (
        SELECT COALESCE(MAX(sequence), 0) + 1
        FROM omnivia_engineering_checkpoints
        WHERE workspace_id = NEW.workspace_id
          AND session_id = NEW.session_id);
    SELECT RAISE(ABORT, 'omnivia: an engineering checkpoint cannot be appended to a closed session')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_engineering_sessions s
        WHERE s.workspace_id = NEW.workspace_id
          AND s.session_id = NEW.session_id
          AND s.state <> 'active');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_checkpoints_update
BEFORE UPDATE ON omnivia_engineering_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_checkpoints is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_checkpoints_delete
BEFORE DELETE ON omnivia_engineering_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_checkpoints is append-only; DELETE is never permitted');
END;
