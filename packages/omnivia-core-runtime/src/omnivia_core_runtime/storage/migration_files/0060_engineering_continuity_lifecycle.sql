-- Trusted continuity lifecycle history and current-association authority.
--
-- Migration 0048 stores the immutable session identity and checkpoint chain.  This
-- migration adds the append-only history and the single mutable current pointer
-- needed to rotate a trusted adapter binding without turning an old registration
-- response back into authority.  Existing rows are retained as historical legacy
-- facts, but no trusted association pointer is inferred from their correlation text.

CREATE TABLE IF NOT EXISTS omnivia_engineering_session_lifecycle (
    workspace_id            TEXT    NOT NULL,
    session_id              TEXT    NOT NULL,
    event_sequence          INTEGER NOT NULL,
    event_type              TEXT    NOT NULL,
    association_key         TEXT,
    binding_generation      INTEGER NOT NULL,
    state                   TEXT    NOT NULL,
    lease_expires_at_us     INTEGER NOT NULL,
    settled_at_us           INTEGER NOT NULL,
    prior_session_id        TEXT,
    audit_ref               TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, session_id, event_sequence),

    CHECK (event_sequence > 0 AND typeof(event_sequence) = 'integer'),
    CHECK (event_type IN (
        'legacy_imported', 'registered', 'renewed', 'closed',
        'expired', 'revoked', 'superseded'
    )),
    CHECK (association_key IS NULL OR (
        typeof(association_key) = 'text'
        AND length(association_key) = 71
        AND substr(association_key, 1, 7) = 'sha256:'
        AND substr(association_key, 8) NOT GLOB '*[^0-9a-f]*'
    )),
    CHECK (binding_generation > 0 AND typeof(binding_generation) = 'integer'),
    CHECK (state IN ('active', 'closed', 'expired', 'revoked')),
    CHECK (lease_expires_at_us > 0 AND typeof(lease_expires_at_us) = 'integer'),
    CHECK (settled_at_us > 0 AND typeof(settled_at_us) = 'integer'),
    CHECK (prior_session_id IS NULL OR (
        typeof(prior_session_id) = 'text'
        AND length(prior_session_id) BETWEEN 1 AND 128
        AND prior_session_id GLOB '[A-Za-z0-9]*'
        AND prior_session_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(prior_session_id, char(0)) = 0
    )),
    CHECK ((event_type IN ('registered', 'renewed')) = (state = 'active')
           OR event_type = 'legacy_imported'),
    CHECK ((event_type = 'closed') = (state = 'closed')
           OR event_type NOT IN ('closed')),
    CHECK ((event_type IN ('revoked', 'superseded')) = (state = 'revoked')
           OR event_type NOT IN ('revoked', 'superseded')),
    CHECK ((event_type = 'expired') = (state = 'expired')
           OR event_type <> 'expired'),

    FOREIGN KEY (workspace_id, session_id)
        REFERENCES omnivia_engineering_sessions (workspace_id, session_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- Preserve the pre-0060 state without claiming that an encoded host correlation
-- proves a current trusted association.  Runtime inserts of `legacy_imported` are
-- prohibited by the trigger installed below.
INSERT INTO omnivia_engineering_session_lifecycle (
    workspace_id, session_id, event_sequence, event_type, association_key,
    binding_generation, state, lease_expires_at_us, settled_at_us,
    prior_session_id, audit_ref
)
SELECT workspace_id, session_id, 1, 'legacy_imported', NULL,
       binding_generation, state, lease_expires_at_us, registered_at_us,
       NULL, audit_ref
FROM omnivia_engineering_sessions;

CREATE TABLE IF NOT EXISTS omnivia_engineering_session_authority (
    workspace_id            TEXT    NOT NULL,
    principal_id            TEXT    NOT NULL,
    association_key         TEXT    NOT NULL,
    current_session_id      TEXT    NOT NULL,
    binding_generation      INTEGER NOT NULL,
    state                   TEXT    NOT NULL,
    lease_expires_at_us     INTEGER NOT NULL,
    updated_at_us           INTEGER NOT NULL,
    audit_ref               TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, principal_id, association_key),
    UNIQUE (workspace_id, current_session_id),

    CHECK (typeof(association_key) = 'text'
           AND length(association_key) = 71
           AND substr(association_key, 1, 7) = 'sha256:'
           AND substr(association_key, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (binding_generation >= 2 AND typeof(binding_generation) = 'integer'),
    CHECK (state IN ('active', 'closed', 'expired', 'revoked')),
    CHECK (lease_expires_at_us > 0 AND typeof(lease_expires_at_us) = 'integer'),
    CHECK (updated_at_us > 0 AND typeof(updated_at_us) = 'integer'),

    FOREIGN KEY (workspace_id, current_session_id)
        REFERENCES omnivia_engineering_sessions (workspace_id, session_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_session_lifecycle_association
    ON omnivia_engineering_session_lifecycle (
        workspace_id, association_key, binding_generation, session_id
    );

DROP TRIGGER IF EXISTS omnivia_guard_omnivia_engineering_sessions_update;

CREATE TRIGGER omnivia_guard_omnivia_engineering_sessions_update
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);

    SELECT RAISE(ABORT, 'omnivia: engineering session identity is immutable')
    WHERE NEW.principal_id IS NOT OLD.principal_id
       OR NEW.host_session_ref IS NOT OLD.host_session_ref
       OR NEW.checkout_hint IS NOT OLD.checkout_hint
       OR NEW.repository_target_json IS NOT OLD.repository_target_json
       OR NEW.registered_at_us IS NOT OLD.registered_at_us
       OR NEW.audit_ref IS NOT OLD.audit_ref;

    SELECT RAISE(ABORT, 'omnivia: an engineering session cannot leave or mutate a terminal state')
    WHERE OLD.state IN ('closed', 'expired', 'revoked')
      AND (NEW.state IS NOT OLD.state
           OR NEW.binding_generation IS NOT OLD.binding_generation
           OR NEW.lease_expires_at_us IS NOT OLD.lease_expires_at_us
           OR NEW.closed_at_us IS NOT OLD.closed_at_us
           OR NEW.last_checkpoint_sequence IS NOT OLD.last_checkpoint_sequence
           OR NEW.last_checkpoint_id IS NOT OLD.last_checkpoint_id);

    SELECT RAISE(ABORT, 'omnivia: an engineering session transition is invalid')
    WHERE NEW.state IS NOT OLD.state
      AND NOT (OLD.state = 'active' AND NEW.state IN ('closed', 'expired', 'revoked'));

    SELECT RAISE(ABORT, 'omnivia: an engineering binding generation must advance by exactly one')
    WHERE NEW.binding_generation IS NOT OLD.binding_generation
      AND (OLD.state <> 'active'
           OR NEW.state <> 'active'
           OR OLD.binding_generation >= 9223372036854775807
           OR typeof(NEW.binding_generation) <> 'integer'
           OR NEW.binding_generation <> OLD.binding_generation + 1);

    SELECT RAISE(ABORT, 'omnivia: a continuity lease changes only with one generation rotation')
    WHERE NEW.lease_expires_at_us IS NOT OLD.lease_expires_at_us
      AND (NEW.binding_generation <> OLD.binding_generation + 1
           OR NEW.lease_expires_at_us <= OLD.lease_expires_at_us
           OR NEW.state <> 'active');

    SELECT RAISE(ABORT, 'omnivia: a generation rotation must extend the continuity lease')
    WHERE NEW.binding_generation IS NOT OLD.binding_generation
      AND NEW.lease_expires_at_us IS OLD.lease_expires_at_us;

    SELECT RAISE(ABORT, 'omnivia: a lifecycle change requires matching append-only history')
    WHERE (NEW.state IS NOT OLD.state
           OR NEW.binding_generation IS NOT OLD.binding_generation
           OR NEW.lease_expires_at_us IS NOT OLD.lease_expires_at_us)
      AND NOT EXISTS (
          SELECT 1 FROM omnivia_engineering_session_lifecycle h
          WHERE h.workspace_id = NEW.workspace_id
            AND h.session_id = NEW.session_id
            AND h.event_sequence = (
                SELECT MAX(h2.event_sequence)
                FROM omnivia_engineering_session_lifecycle h2
                WHERE h2.workspace_id = NEW.workspace_id
                  AND h2.session_id = NEW.session_id
            )
            AND h.binding_generation = NEW.binding_generation
            AND h.state = NEW.state
            AND h.lease_expires_at_us = NEW.lease_expires_at_us
      );

    SELECT RAISE(ABORT, 'omnivia: the engineering session checkpoint pointer may only advance')
    WHERE NEW.last_checkpoint_sequence IS NOT NULL
      AND OLD.last_checkpoint_sequence IS NOT NULL
      AND NEW.last_checkpoint_sequence < OLD.last_checkpoint_sequence;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_session_lifecycle_insert
BEFORE INSERT ON omnivia_engineering_session_lifecycle
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_session_lifecycle')
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

    SELECT RAISE(ABORT, 'omnivia: legacy continuity history is migration-owned')
    WHERE NEW.event_type = 'legacy_imported';

    SELECT RAISE(ABORT, 'omnivia: continuity lifecycle history must be contiguous')
    WHERE NEW.event_sequence IS NOT (
        SELECT COALESCE(MAX(event_sequence), 0) + 1
        FROM omnivia_engineering_session_lifecycle
        WHERE workspace_id = NEW.workspace_id AND session_id = NEW.session_id
    );

    SELECT RAISE(ABORT, 'omnivia: continuity lifecycle history requires its exact successful audit')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_application_audit_events a
        JOIN omnivia_engineering_sessions s
          ON s.workspace_id = a.workspace_id
         AND s.principal_id = a.principal_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.audit_ref = NEW.audit_ref
          AND a.recorded_at_us = NEW.settled_at_us
          AND a.outcome_class = 'succeeded'
          AND s.session_id = NEW.session_id
    );

    SELECT RAISE(ABORT, 'omnivia: continuity association history does not match its session')
    WHERE NEW.association_key IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM omnivia_engineering_sessions s
          WHERE s.workspace_id = NEW.workspace_id
            AND s.session_id = NEW.session_id
            AND substr(s.host_session_ref, 1,
                       length('core-association.v1:' || NEW.association_key || ':'))
                = 'core-association.v1:' || NEW.association_key || ':'
      );

    SELECT RAISE(ABORT, 'omnivia: registered lifecycle history does not match its session')
    WHERE NEW.event_type = 'registered'
      AND ((NEW.association_key IS NULL AND NEW.binding_generation <> 1)
           OR NOT EXISTS (
          SELECT 1 FROM omnivia_engineering_sessions s
          WHERE s.workspace_id = NEW.workspace_id
            AND s.session_id = NEW.session_id
            AND s.state = 'active'
            AND s.binding_generation = NEW.binding_generation
            AND s.lease_expires_at_us = NEW.lease_expires_at_us
      ));

    SELECT RAISE(ABORT, 'omnivia: associated registration must advance the settled generation by one')
    WHERE NEW.event_type = 'registered'
      AND NEW.association_key IS NOT NULL
      AND NEW.binding_generation IS NOT (
          SELECT COALESCE(MAX(s.binding_generation), 1) + 1
          FROM omnivia_engineering_sessions s
          WHERE s.workspace_id = NEW.workspace_id
            AND s.session_id <> NEW.session_id
            AND substr(s.host_session_ref, 1,
                       length('core-association.v1:' || NEW.association_key || ':'))
                = 'core-association.v1:' || NEW.association_key || ':'
      );

    SELECT RAISE(ABORT, 'omnivia: continuity renewal does not match current active authority')
    WHERE NEW.event_type = 'renewed'
      AND NOT EXISTS (
          SELECT 1
          FROM omnivia_engineering_sessions s
          JOIN omnivia_engineering_session_authority a
            ON a.workspace_id = s.workspace_id
           AND a.current_session_id = s.session_id
          WHERE s.workspace_id = NEW.workspace_id
            AND s.session_id = NEW.session_id
            AND a.association_key = NEW.association_key
            AND s.state = 'active'
            AND a.state = 'active'
            AND a.binding_generation = s.binding_generation
            AND NEW.binding_generation = s.binding_generation + 1
            AND NEW.lease_expires_at_us > s.lease_expires_at_us
            AND s.lease_expires_at_us > NEW.settled_at_us
      );

    SELECT RAISE(ABORT, 'omnivia: terminal continuity history does not match current active state')
    WHERE NEW.event_type IN ('closed', 'expired', 'revoked', 'superseded')
      AND NOT EXISTS (
          SELECT 1 FROM omnivia_engineering_sessions s
          WHERE s.workspace_id = NEW.workspace_id
            AND s.session_id = NEW.session_id
            AND s.state = 'active'
            AND s.binding_generation = NEW.binding_generation
            AND s.lease_expires_at_us = NEW.lease_expires_at_us
      );

    SELECT RAISE(ABORT, 'omnivia: continuity expiry cannot precede its lease boundary')
    WHERE NEW.event_type = 'expired'
      AND NEW.settled_at_us < NEW.lease_expires_at_us;
END;

-- Couple every guarded session insertion to its first append-only lifecycle
-- fact in the database itself.  Generation one is deliberately unassociated:
-- a caller-controlled opaque host reference that resembles Core's encoding
-- must never manufacture trusted association authority.  Associated rows use
-- generation two or later and the guarded lifecycle trigger below validates
-- the complete encoded key and exact generation step.
CREATE TRIGGER IF NOT EXISTS omnivia_record_engineering_session_registration
AFTER INSERT ON omnivia_engineering_sessions
BEGIN
    INSERT INTO omnivia_engineering_session_lifecycle (
        workspace_id, session_id, event_sequence, event_type, association_key,
        binding_generation, state, lease_expires_at_us, settled_at_us,
        prior_session_id, audit_ref
    ) VALUES (
        NEW.workspace_id,
        NEW.session_id,
        1,
        'registered',
        CASE
            WHEN NEW.binding_generation >= 2
            THEN substr(NEW.host_session_ref, 21, 71)
            ELSE NULL
        END,
        NEW.binding_generation,
        NEW.state,
        NEW.lease_expires_at_us,
        NEW.registered_at_us,
        CASE
            WHEN NEW.binding_generation >= 2
            THEN COALESCE(
                (
                    SELECT current_session_id
                    FROM omnivia_engineering_session_authority
                    WHERE workspace_id = NEW.workspace_id
                      AND principal_id = NEW.principal_id
                      AND association_key = substr(NEW.host_session_ref, 21, 71)
                ),
                (
                    SELECT session_id
                    FROM omnivia_engineering_sessions
                    WHERE workspace_id = NEW.workspace_id
                      AND principal_id = NEW.principal_id
                      AND session_id <> NEW.session_id
                      AND binding_generation >= 2
                      AND substr(host_session_ref, 1, 92) =
                          substr(NEW.host_session_ref, 1, 92)
                    ORDER BY binding_generation DESC, session_id ASC
                    LIMIT 1
                )
            )
            ELSE NULL
        END,
        NEW.audit_ref
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_session_lifecycle_update
BEFORE UPDATE ON omnivia_engineering_session_lifecycle
BEGIN
    SELECT RAISE(ABORT, 'omnivia: continuity lifecycle history is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_session_lifecycle_delete
BEFORE DELETE ON omnivia_engineering_session_lifecycle
BEGIN
    SELECT RAISE(ABORT, 'omnivia: continuity lifecycle history is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_session_authority_insert
BEFORE INSERT ON omnivia_engineering_session_authority
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_session_authority')
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

    SELECT RAISE(ABORT, 'omnivia: current continuity authority must match a registered session')
    WHERE NEW.state <> 'active'
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_engineering_sessions s
        JOIN omnivia_engineering_session_lifecycle h
          ON h.workspace_id = s.workspace_id AND h.session_id = s.session_id
        WHERE s.workspace_id = NEW.workspace_id
          AND s.session_id = NEW.current_session_id
          AND s.principal_id = NEW.principal_id
          AND s.binding_generation = NEW.binding_generation
          AND s.state = NEW.state
          AND s.lease_expires_at_us = NEW.lease_expires_at_us
          AND h.event_type = 'registered'
          AND h.association_key = NEW.association_key
          AND h.binding_generation = NEW.binding_generation
          AND h.state = NEW.state
          AND h.lease_expires_at_us = NEW.lease_expires_at_us
          AND h.settled_at_us = NEW.updated_at_us
          AND h.audit_ref = NEW.audit_ref
          AND h.event_sequence = (
              SELECT MAX(h2.event_sequence)
              FROM omnivia_engineering_session_lifecycle h2
              WHERE h2.workspace_id = NEW.workspace_id
                AND h2.session_id = NEW.current_session_id
          )
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_session_authority_update
BEFORE UPDATE ON omnivia_engineering_session_authority
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_engineering_session_authority')
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

    SELECT RAISE(ABORT, 'omnivia: continuity association authority identity is immutable')
    WHERE NEW.principal_id IS NOT OLD.principal_id
       OR NEW.association_key IS NOT OLD.association_key;

    SELECT RAISE(ABORT, 'omnivia: a terminal continuity session cannot reopen')
    WHERE OLD.state IN ('closed', 'expired', 'revoked')
      AND NEW.current_session_id IS OLD.current_session_id
      AND (NEW.state IS NOT OLD.state
           OR NEW.binding_generation IS NOT OLD.binding_generation
           OR NEW.lease_expires_at_us IS NOT OLD.lease_expires_at_us);

    SELECT RAISE(ABORT, 'omnivia: current continuity authority rotates by exactly one generation')
    WHERE (NEW.current_session_id IS NOT OLD.current_session_id
           OR NEW.binding_generation IS NOT OLD.binding_generation)
      AND (NEW.state <> 'active'
           OR (OLD.state <> 'active'
               AND NEW.current_session_id IS OLD.current_session_id)
           OR OLD.binding_generation >= 9223372036854775807
           OR NEW.binding_generation <> OLD.binding_generation + 1);

    SELECT RAISE(ABORT, 'omnivia: continuity authority state transition is invalid')
    WHERE NEW.state IS NOT OLD.state
      AND NOT (
          (OLD.state = 'active' AND NEW.state IN ('closed', 'expired', 'revoked'))
          OR (OLD.state IN ('closed', 'expired', 'revoked')
              AND NEW.state = 'active'
              AND NEW.current_session_id IS NOT OLD.current_session_id
              AND NEW.binding_generation = OLD.binding_generation + 1)
      );

    SELECT RAISE(ABORT, 'omnivia: continuity authority lease changes only with generation rotation')
    WHERE NEW.lease_expires_at_us IS NOT OLD.lease_expires_at_us
      AND (NEW.binding_generation <> OLD.binding_generation + 1
           OR NEW.lease_expires_at_us <= OLD.lease_expires_at_us);

    SELECT RAISE(ABORT, 'omnivia: current continuity authority must match its session')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_sessions s
        WHERE s.workspace_id = NEW.workspace_id
          AND s.session_id = NEW.current_session_id
          AND s.principal_id = NEW.principal_id
          AND s.binding_generation = NEW.binding_generation
          AND s.state = NEW.state
          AND s.lease_expires_at_us = NEW.lease_expires_at_us
    );

    SELECT RAISE(ABORT, 'omnivia: current continuity authority requires matching lifecycle history')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_session_lifecycle h
        WHERE h.workspace_id = NEW.workspace_id
          AND h.session_id = NEW.current_session_id
          AND h.binding_generation = NEW.binding_generation
          AND h.state = NEW.state
          AND h.lease_expires_at_us = NEW.lease_expires_at_us
          AND h.settled_at_us = NEW.updated_at_us
          AND h.audit_ref = NEW.audit_ref
          AND h.event_sequence = (
              SELECT MAX(h2.event_sequence)
              FROM omnivia_engineering_session_lifecycle h2
              WHERE h2.workspace_id = NEW.workspace_id
                AND h2.session_id = NEW.current_session_id
          )
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_session_authority_delete
BEFORE DELETE ON omnivia_engineering_session_authority
BEGIN
    SELECT RAISE(ABORT, 'omnivia: continuity association authority cannot be deleted');
END;
