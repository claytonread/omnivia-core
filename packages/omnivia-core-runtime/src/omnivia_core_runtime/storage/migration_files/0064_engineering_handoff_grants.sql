-- Cross-principal continuity handoff grants.
--
-- Continuity is owner-only (0048): a checkpoint is another principal's `not_found`.
-- A grant is the one narrow, audited exception.  It names exactly one existing
-- checkpoint, pinned by the checkpoint's own content digest, and exactly one other
-- principal, for a bounded time.  It transfers working context through the redacted
-- `continuity_handoff.v1` view and nothing else: no session, no write, no further
-- grant.
--
--   omnivia_engineering_handoff_grants
--       one immutable grant.  Only the checkpoint's owner can write one, because the
--       insert trigger re-derives ownership from the checkpoint's session: a grantee
--       has no row it could write, so a grant is not delegable or re-grantable.
--   omnivia_engineering_handoff_grant_revocations
--       at most one immutable revocation per grant.  A grant is never updated or
--       deleted; revocation is a second append-only fact, so the history of who was
--       granted what and when it ended is always recoverable.
--
-- Trigger notes (kept out of the trigger bodies, whose stored text the schema
-- fingerprint compares).  The grant insert
-- accepts only the checkpoint's own owner and the exact digest the checkpoint carries;
-- a grantee owns no checkpoint of this session, so it can never write a grant of its
-- own.  The revocation insert accepts only a row carrying the grantor's own audited
-- `continuity.handoff.revoke`.
--
-- A grant is live when it is unexpired, unrevoked and its pinned digest still equals
-- the checkpoint's digest.  Closing the owning session changes none of that: a close
-- never silently revokes.  "Existing principal" means the principal has registered a
-- continuity session in this workspace; Core keeps no separate principal registry.

CREATE TABLE IF NOT EXISTS omnivia_engineering_handoff_grants (
    workspace_id          TEXT    NOT NULL,
    grant_id              TEXT    NOT NULL,
    checkpoint_id         TEXT    NOT NULL,
    session_id            TEXT    NOT NULL,
    grantor_principal_id  TEXT    NOT NULL,
    grantee_principal_id  TEXT    NOT NULL,
    checkpoint_digest     TEXT    NOT NULL,
    granted_at_us         INTEGER NOT NULL,
    expires_at_us         INTEGER NOT NULL,
    audit_ref             TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, grant_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(grant_id) = 'text' AND length(grant_id) BETWEEN 1 AND 128
           AND grant_id GLOB '[A-Za-z0-9]*'
           AND grant_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(grant_id, char(0)) = 0),
    CHECK (typeof(checkpoint_id) = 'text' AND length(checkpoint_id) BETWEEN 1 AND 128
           AND checkpoint_id GLOB '[A-Za-z0-9]*'
           AND checkpoint_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(checkpoint_id, char(0)) = 0),
    CHECK (typeof(session_id) = 'text' AND length(session_id) BETWEEN 1 AND 128
           AND session_id GLOB '[A-Za-z0-9]*'
           AND session_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(session_id, char(0)) = 0),
    CHECK (typeof(grantor_principal_id) = 'text'
           AND length(grantor_principal_id) BETWEEN 1 AND 128),
    CHECK (typeof(grantee_principal_id) = 'text'
           AND length(grantee_principal_id) BETWEEN 1 AND 128),
    CHECK (grantor_principal_id <> grantee_principal_id),
    CHECK (typeof(checkpoint_digest) = 'text' AND length(checkpoint_digest) = 71
           AND substr(checkpoint_digest, 1, 7) = 'sha256:'
           AND substr(checkpoint_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(granted_at_us) = 'integer' AND granted_at_us > 0),
    CHECK (typeof(expires_at_us) = 'integer'
           AND expires_at_us - granted_at_us BETWEEN 60000000 AND 604800000000),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, checkpoint_id)
        REFERENCES omnivia_engineering_checkpoints (workspace_id, checkpoint_id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES omnivia_engineering_sessions (workspace_id, session_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_handoff_grant_revocations (
    workspace_id   TEXT    NOT NULL,
    grant_id       TEXT    NOT NULL,
    revoked_at_us  INTEGER NOT NULL,
    audit_ref      TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, grant_id),

    CHECK (typeof(revoked_at_us) = 'integer' AND revoked_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, grant_id)
        REFERENCES omnivia_engineering_handoff_grants (workspace_id, grant_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_handoff_grants_grantee
    ON omnivia_engineering_handoff_grants (
        workspace_id, grantee_principal_id, checkpoint_id, expires_at_us
    );
CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_handoff_grants_checkpoint
    ON omnivia_engineering_handoff_grants (workspace_id, checkpoint_id);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_handoff_grants_insert
BEFORE INSERT ON omnivia_engineering_handoff_grants
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_handoff_grants')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);

    SELECT RAISE(ABORT, 'omnivia: a handoff grant requires its exact successful audit')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events a
        WHERE a.workspace_id = NEW.workspace_id
          AND a.audit_ref = NEW.audit_ref
          AND a.principal_id = NEW.grantor_principal_id
          AND a.operation = 'continuity.handoff.grant'
          AND a.outcome_class = 'succeeded'
          AND a.recorded_at_us = NEW.granted_at_us
    );

    SELECT RAISE(ABORT, 'omnivia: a handoff grant must name its owner''s exact checkpoint and digest')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_engineering_checkpoints c
        JOIN omnivia_engineering_sessions s
          ON s.workspace_id = c.workspace_id AND s.session_id = c.session_id
        WHERE c.workspace_id = NEW.workspace_id
          AND c.checkpoint_id = NEW.checkpoint_id
          AND c.session_id = NEW.session_id
          AND c.content_digest = NEW.checkpoint_digest
          AND s.principal_id = NEW.grantor_principal_id
    );

    SELECT RAISE(ABORT, 'omnivia: a handoff grantee must be an existing continuity principal')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_sessions s
        WHERE s.workspace_id = NEW.workspace_id
          AND s.principal_id = NEW.grantee_principal_id
    );

    SELECT RAISE(ABORT, 'omnivia: a live handoff grant already exists for this checkpoint and grantee')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_engineering_handoff_grants g
        WHERE g.workspace_id = NEW.workspace_id
          AND g.checkpoint_id = NEW.checkpoint_id
          AND g.grantee_principal_id = NEW.grantee_principal_id
          AND g.expires_at_us > NEW.granted_at_us
          AND NOT EXISTS (
              SELECT 1 FROM omnivia_engineering_handoff_grant_revocations r
              WHERE r.workspace_id = g.workspace_id AND r.grant_id = g.grant_id
          )
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_handoff_grants_update
BEFORE UPDATE ON omnivia_engineering_handoff_grants
BEGIN
    SELECT RAISE(ABORT, 'omnivia: handoff grants are append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_handoff_grants_delete
BEFORE DELETE ON omnivia_engineering_handoff_grants
BEGIN
    SELECT RAISE(ABORT, 'omnivia: handoff grants are append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_handoff_grant_revocations_insert
BEFORE INSERT ON omnivia_engineering_handoff_grant_revocations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_handoff_grant_revocations')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);

    SELECT RAISE(ABORT, 'omnivia: a handoff revocation requires its grantor''s exact successful audit')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_engineering_handoff_grants g
        JOIN omnivia_application_audit_events a
          ON a.workspace_id = g.workspace_id
         AND a.principal_id = g.grantor_principal_id
        WHERE g.workspace_id = NEW.workspace_id
          AND g.grant_id = NEW.grant_id
          AND a.audit_ref = NEW.audit_ref
          AND a.operation = 'continuity.handoff.revoke'
          AND a.outcome_class = 'succeeded'
          AND a.recorded_at_us = NEW.revoked_at_us
          AND NEW.revoked_at_us >= g.granted_at_us
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_handoff_grant_revocations_update
BEFORE UPDATE ON omnivia_engineering_handoff_grant_revocations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: handoff revocations are append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_handoff_grant_revocations_delete
BEFORE DELETE ON omnivia_engineering_handoff_grant_revocations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: handoff revocations are append-only; DELETE is never permitted');
END;
