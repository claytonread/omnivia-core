-- Persisted DML guard triggers (T-0629F, ADR-037).
--
-- The guard table holds the authority tuple of whichever service instance is
-- currently permitted to mutate. Triggers on every guarded table abort unless the
-- guard row matches the workspace state's current fencing generation.
--
-- Why persisted triggers rather than only a connection authorizer: an authorizer is
-- connection-local, so it does not apply to a connection this runtime did not
-- create. Triggers live in the schema and therefore fire for *any* writer,
-- including an ordinary sqlite3 client.
--
-- What this is NOT: a security boundary against arbitrary code running as the
-- workspace-owning OS principal. ADR-037 says so explicitly - that principal can
-- terminate the service, alter these triggers offline, or modify database bytes
-- directly. This is fail-closed defence against ordinary unregistered DML, and
-- offline trigger drift is detected before writable readiness rather than
-- prevented.

-- Exactly one guard row, or none when no service holds write authority.
CREATE TABLE IF NOT EXISTS omnivia_mutation_guard (
    singleton           INTEGER PRIMARY KEY CHECK (singleton = 1),
    workspace_id        TEXT    NOT NULL,
    service_instance_id TEXT    NOT NULL,
    fencing_generation  INTEGER NOT NULL,
    opened_at           TEXT    NOT NULL
);

-- The guard predicate, expressed once. A mutation is permitted only when a guard
-- row exists whose generation equals the authoritative current generation. A stale
-- generation therefore cannot commit even while its guard row is present.
--
-- Implemented as triggers per table because SQLite has no table-independent
-- "before any write" hook.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_memories_insert
BEFORE INSERT ON memories
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on memories');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_memories_update
BEFORE UPDATE ON memories
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on memories');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_memories_delete
BEFORE DELETE ON memories
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on memories');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_sources_insert
BEFORE INSERT ON sources
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on sources');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_sources_update
BEFORE UPDATE ON sources
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on sources');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_sources_delete
BEFORE DELETE ON sources
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on sources');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chunks_insert
BEFORE INSERT ON chunks
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on chunks');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chunks_delete
BEFORE DELETE ON chunks
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on chunks');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_graph_entities_insert
BEFORE INSERT ON graph_entities
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on graph_entities');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_graph_entities_update
BEFORE UPDATE ON graph_entities
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on graph_entities');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_graph_entities_delete
BEFORE DELETE ON graph_entities
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on graph_entities');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_graph_relationships_insert
BEFORE INSERT ON graph_relationships
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on graph_relationships');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_graph_relationships_delete
BEFORE DELETE ON graph_relationships
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on graph_relationships');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workspaces_insert
BEFORE INSERT ON workspaces
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on workspaces');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workspaces_update
BEFORE UPDATE ON workspaces
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on workspaces');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workspaces_delete
BEFORE DELETE ON workspaces
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on workspaces');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_control_plane_resources_insert
BEFORE INSERT ON control_plane_resources
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on control_plane_resources');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_control_plane_resources_update
BEFORE UPDATE ON control_plane_resources
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on control_plane_resources');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_control_plane_resources_delete
BEFORE DELETE ON control_plane_resources
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on control_plane_resources');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_durable_jobs_insert
BEFORE INSERT ON omnivia_durable_jobs
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_durable_jobs');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_durable_jobs_update
BEFORE UPDATE ON omnivia_durable_jobs
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_durable_jobs');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_ledger_insert
BEFORE INSERT ON omnivia_projection_ledger
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_projection_ledger');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_projection_ledger_update
BEFORE UPDATE ON omnivia_projection_ledger
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_projection_ledger');
END;
