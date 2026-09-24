-- Local Decisions capability settings for one workspace (ADR-042, plan PR-3).
--
-- Additive only. One singleton row per workspace holding the per-workspace
-- processing switch (§28.2: Local Decisions is off until deliberately enabled;
-- §28.3: the per-workspace disable is a kill switch that never deletes
-- history). `processing` is the bounded enum the decision.1 contract carries
-- ('off' | 'advisory' | 'paused' | 'blocked'); admission refuses while it is
-- not 'advisory'. The row is created lazily by the first settings write and is
-- simply absent while the capability has never been touched -- absence reads as
-- the specification's default ('off'), not as an error.
--
-- `revision` is the compare-and-swap counter `decision.settings.update` takes:
-- the caller states the revision it last observed, and the update mints the
-- next one, so a lost update is visible as a conflict. The row is mutable by
-- design -- that is its whole point -- but its identity columns and its
-- monotonic time are not: an update may never move `updated_at_us` backwards
-- and must advance the revision.

CREATE TABLE IF NOT EXISTS omnivia_decision_settings (
    workspace_id         TEXT    NOT NULL,
    singleton            INTEGER NOT NULL,
    processing           TEXT    NOT NULL,
    subscription_enabled INTEGER NOT NULL,
    revision             INTEGER NOT NULL,
    updated_at_us        INTEGER NOT NULL,
    audit_ref            TEXT    NOT NULL,

    PRIMARY KEY (workspace_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(revision) = 'integer' AND revision >= 0),
    CHECK (typeof(singleton) = 'integer' AND singleton = 1),
    CHECK (processing IN ('off', 'advisory', 'paused', 'blocked')),
    CHECK (typeof(subscription_enabled) = 'integer'
           AND subscription_enabled IN (0, 1)),
    CHECK (typeof(revision) = 'integer' AND revision >= 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0)
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_settings_insert
BEFORE INSERT ON omnivia_decision_settings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_decision_settings')
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
    SELECT RAISE(ABORT, 'omnivia: decision settings are a singleton row')
    WHERE NEW.singleton IS NOT 1
       OR EXISTS (
            SELECT 1 FROM omnivia_decision_settings);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_settings_update
BEFORE UPDATE ON omnivia_decision_settings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_decision_settings')
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
    SELECT RAISE(ABORT, 'omnivia: decision settings identity is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.singleton IS NOT OLD.singleton
       OR NEW.updated_at_us < OLD.updated_at_us
       OR NEW.revision IS NOT OLD.revision + 1;
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_decision_settings_delete
BEFORE DELETE ON omnivia_decision_settings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_decision_settings is append-only; DELETE is never permitted');
END;
