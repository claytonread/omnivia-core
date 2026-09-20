-- The `role` grant kind for installed-MCP setups (Gate B, R004 section 9.1).
--
-- R004 section 9.1 requires an `authoring` setup to hold "workspace contributor
-- authority sufficient for `memory:write`" alongside its read rights. That authority is
-- a role rather than a scope -- the mutation coordinator asks for `workspace_contributor`
-- by name -- and `0002` admitted four grant kinds, none of which could carry it. Without
-- this, an authoring principal authenticates with every operation, scope, purpose and
-- capability its profile implies and is still refused every mutation those exist for.
--
-- Forward-only, and a rebuild rather than an edit. SQLite has no `ALTER TABLE ... DROP
-- CONSTRAINT`, so widening a CHECK means building the table this migration wants,
-- carrying `0002`'s rows into it unchanged, and putting the guards back. Every row is
-- preserved: the grant table is the evidence of what each setup generation was granted,
-- and a migration that dropped a generation would be erasing the record of a right
-- somebody once held.
--
-- Nothing else moves. The column list, the types, every other CHECK, the foreign key,
-- both indexes and all three triggers are `0002`'s, restated because a rebuild has to
-- restate them, and a reviewer should be able to diff this table against `0002`'s and
-- find exactly one changed line.
--
-- A role value is bounded and exact by the same CHECK every other grant value obeys: no
-- `*`, no `?`, one to 128 characters. Which roles a profile actually grants stays in
-- `service/installed_mcp.py`, where it is derived beside the rest of the policy, rather
-- than being restated as a vocabulary here that a second reviewed change would have to
-- keep in step.
--
-- The conventions are `0001`'s and `0002`'s: every comment sits between statements,
-- never inside one, so the applied schema matches the canonical fingerprint byte for
-- byte.

-- The guards come off first. They name the table this migration replaces, and they are
-- recreated verbatim below against the rebuilt one; between the two the table is
-- unguarded, which is safe only because the whole migration runs inside the migrator's
-- single transaction and nothing else holds this catalogue open.
DROP TRIGGER IF EXISTS omnivia_guard_installation_mcp_grants_insert;

DROP TRIGGER IF EXISTS omnivia_guard_installation_mcp_grants_update;

DROP TRIGGER IF EXISTS omnivia_guard_installation_mcp_grants_delete;

-- `0002`'s grant table with one line changed: `grant_kind` now admits 'role'.
CREATE TABLE IF NOT EXISTS omnivia_installation_mcp_grants_rebuilt (
    grant_row_id       TEXT    NOT NULL PRIMARY KEY,
    installation_id    TEXT    NOT NULL,
    setup_id           TEXT    NOT NULL,
    setup_generation   INTEGER NOT NULL,
    grant_kind         TEXT    NOT NULL,
    grant_value        TEXT    NOT NULL,
    grant_version      TEXT,
    fencing_generation INTEGER NOT NULL,
    granted_at_us      INTEGER NOT NULL,

    CHECK (length(grant_row_id)    BETWEEN 1 AND 128),
    CHECK (length(installation_id) BETWEEN 1 AND 128),
    CHECK (length(setup_id)        BETWEEN 1 AND 128),
    CHECK (typeof(setup_generation) = 'integer' AND setup_generation > 0),
    CHECK (grant_kind IN ('operation', 'scope', 'purpose', 'capability', 'role')),
    CHECK (length(grant_value) BETWEEN 1 AND 128 AND grant_value NOT GLOB '*[*?]*'),
    CHECK (
        (grant_kind = 'capability' AND grant_version IS NOT NULL
         AND length(grant_version) BETWEEN 1 AND 32
         AND grant_version NOT GLOB '*[*?]*')
        OR (grant_kind <> 'capability' AND grant_version IS NULL)
    ),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(granted_at_us) = 'integer' AND granted_at_us > 0),

    FOREIGN KEY (setup_id, installation_id)
        REFERENCES omnivia_installation_mcp_setups (setup_id, installation_id)
);

-- Every `0002` row, carried across by name rather than by `SELECT *`, so a column added
-- on either side is a loud failure here instead of a silent misalignment. The rebuilt
-- table has no triggers yet, which is what lets an append-only table be written to at
-- all: its own INSERT guard would refuse these rows for naming a historical generation.
INSERT INTO omnivia_installation_mcp_grants_rebuilt
    (grant_row_id, installation_id, setup_id, setup_generation, grant_kind,
     grant_value, grant_version, fencing_generation, granted_at_us)
SELECT grant_row_id, installation_id, setup_id, setup_generation, grant_kind,
       grant_value, grant_version, fencing_generation, granted_at_us
FROM omnivia_installation_mcp_grants;

-- The old table goes, taking its indexes with it. Its DELETE guard does not fire: the
-- implicit row removal a DROP performs is not a statement any trigger sees, which is the
-- one way an append-only table can ever be replaced.
DROP TABLE omnivia_installation_mcp_grants;

ALTER TABLE omnivia_installation_mcp_grants_rebuilt
    RENAME TO omnivia_installation_mcp_grants;

-- `0002`'s two indexes, recreated because they were dropped with the table they were on.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_mcp_grants_right
    ON omnivia_installation_mcp_grants
       (setup_id, setup_generation, grant_kind, grant_value);

CREATE INDEX IF NOT EXISTS omnivia_idx_installation_mcp_grants_generation
    ON omnivia_installation_mcp_grants (setup_id, setup_generation);

-- `0002`'s three guards, verbatim. A grant row is written by the current owner, for a
-- setup in this installation, and at exactly that setup's current generation; nothing is
-- ever updated, and nothing is ever deleted.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_mcp_grants_insert
BEFORE INSERT ON omnivia_installation_mcp_grants
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_mcp_setups m
    WHERE m.setup_id = NEW.setup_id
      AND m.installation_id = NEW.installation_id
      AND m.setup_generation = NEW.setup_generation
      AND m.status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_mcp_grants');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_mcp_grants_update
BEFORE UPDATE ON omnivia_installation_mcp_grants
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_mcp_grants is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_mcp_grants_delete
BEFORE DELETE ON omnivia_installation_mcp_grants
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_mcp_grants is append-only; DELETE is never permitted');
END;
