-- Durable application settings for the Core Settings screen.
--
-- Additive only. One mutable key/value table and three statement triggers.
--
--   omnivia_app_settings  one stored value for one setting key, per workspace
--
-- Why a key/value table rather than a column per setting
-- ------------------------------------------------------
--
-- The settings screen's controls are owned by features that land independently --
-- backup, processing profiles, per-application access levels, capture, local
-- decisions. A column per control would make every one of those features a schema
-- migration against a table none of them own, and would couple their release order
-- to this one. A key per control lets each feature start writing its own keys when
-- it exists, with no further DDL.
--
-- The cost of that choice is that SQLite cannot type-check the values, so the value
-- is JSON text and its shape is the writing feature's to validate. This table
-- enforces what is checkable here: the key's shape, that the value is non-empty
-- bounded text, and that every write is fenced, attributed and versioned.
--
-- Why the key is not constrained to a fixed allowlist
-- ----------------------------------------------------
--
-- An allowlist in a CHECK would have to name every setting of every unshipped
-- feature up front, and each later addition would be a migration whose only purpose
-- is to widen a list -- the exact coupling the key/value shape exists to avoid. The
-- key's *shape* is constrained instead: lowercase dotted segments, so a key is
-- always parseable as namespace plus leaf, and a typo cannot collide with another
-- feature's namespace. Which keys are meaningful is the reading feature's business.
--
-- Why rows may be deleted
-- ------------------------
--
-- Absence means "no stored preference, use the product default", and that is a state
-- the user can return to. An append-only table would have to encode it as a
-- tombstone value that every reader must then understand; deleting the row says the
-- same thing in a way no reader can misread. Deletes are fenced like every other
-- write, so this is not an unguarded escape hatch.
--
-- Why there is no audit reference
-- --------------------------------
--
-- A settings write is a user preference, not a governed application mutation, and
-- `omnivia_application_audit_events` is the ledger for the latter. `updated_by`
-- records which component wrote the value, which is what a support question about a
-- surprising setting actually needs. If a setting is ever made a governed operation,
-- that operation brings its own audit row.

CREATE TABLE IF NOT EXISTS omnivia_app_settings (
    workspace_id   TEXT    NOT NULL,
    setting_key    TEXT    NOT NULL,
    value_json     TEXT    NOT NULL,
    schema_version INTEGER NOT NULL,
    version        INTEGER NOT NULL,
    updated_at_us  INTEGER NOT NULL,
    updated_by     TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, setting_key),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(setting_key) = 'text' AND length(setting_key) BETWEEN 1 AND 128
           AND setting_key GLOB '[a-z]*'
           AND setting_key NOT GLOB '*[^a-z0-9._-]*'
           AND setting_key NOT GLOB '*..*'
           AND setting_key NOT GLOB '*.'
           AND instr(setting_key, char(0)) = 0),
    CHECK (typeof(value_json) = 'text'
           AND length(CAST(value_json AS BLOB)) BETWEEN 1 AND 16384
           AND instr(value_json, char(0)) = 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),
    CHECK (typeof(updated_by) = 'text' AND length(updated_by) BETWEEN 1 AND 128
           AND updated_by GLOB '[A-Za-z0-9]*'
           AND updated_by NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(updated_by, char(0)) = 0)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_app_settings_insert
BEFORE INSERT ON omnivia_app_settings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_app_settings')
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
    SELECT RAISE(ABORT, 'omnivia: a stored setting starts at version 1')
    WHERE NEW.version IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_app_settings_update
BEFORE UPDATE ON omnivia_app_settings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_app_settings')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: setting identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.setting_key <> OLD.setting_key
       OR NEW.schema_version <> OLD.schema_version;
    SELECT RAISE(ABORT, 'omnivia: setting version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: setting time must not regress')
    WHERE NEW.updated_at_us < OLD.updated_at_us;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_app_settings_delete
BEFORE DELETE ON omnivia_app_settings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_app_settings')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
END;
