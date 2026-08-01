-- Complete the persisted mutation-guard coverage (T-0629F, SB-05).
--
-- `0002_mutation_guard.sql` guarded nine tables named by hand. The frozen schema has
-- sixteen mutable tables, and four of the nine were themselves missing one of the
-- three statement triggers -- a table can look guarded while one statement class
-- walks straight past it. Both gaps were executable: `entity_memory_links`,
-- `control_plane_manifests` and `control_plane_events` all have live mutation call
-- sites, and an unguarded INSERT committed while the guard was closed.
--
-- Added here rather than by editing 0002, whose checksum is pinned: rewriting an
-- applied migration is exactly the drift the ledger exists to catch.
--
-- Coverage below is computed per (table, statement) rather than per trigger name.
-- 0002 named its `omnivia_durable_jobs` and `omnivia_projection_ledger` triggers
-- without the table prefix, so a name-based check reports those statements as
-- unguarded and creates a second trigger that does the same job.
--
-- The six ownership-substrate tables are never guarded. They are the mechanism: a
-- trigger on `omnivia_mutation_guard` would make opening a guard a mutation that
-- requires an open guard.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chunks_update
BEFORE UPDATE ON chunks
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on chunks');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_packs_insert
BEFORE INSERT ON context_packs
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on context_packs');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_packs_update
BEFORE UPDATE ON context_packs
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on context_packs');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_packs_delete
BEFORE DELETE ON context_packs
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on context_packs');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_control_plane_events_insert
BEFORE INSERT ON control_plane_events
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on control_plane_events');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_control_plane_events_update
BEFORE UPDATE ON control_plane_events
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on control_plane_events');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_control_plane_events_delete
BEFORE DELETE ON control_plane_events
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on control_plane_events');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_control_plane_manifests_insert
BEFORE INSERT ON control_plane_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on control_plane_manifests');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_control_plane_manifests_update
BEFORE UPDATE ON control_plane_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on control_plane_manifests');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_control_plane_manifests_delete
BEFORE DELETE ON control_plane_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on control_plane_manifests');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_entity_memory_links_insert
BEFORE INSERT ON entity_memory_links
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on entity_memory_links');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_entity_memory_links_update
BEFORE UPDATE ON entity_memory_links
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on entity_memory_links');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_entity_memory_links_delete
BEFORE DELETE ON entity_memory_links
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on entity_memory_links');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_graph_relationships_update
BEFORE UPDATE ON graph_relationships
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on graph_relationships');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_durable_jobs_delete
BEFORE DELETE ON omnivia_durable_jobs
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_durable_jobs');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_projection_ledger_delete
BEFORE DELETE ON omnivia_projection_ledger
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_projection_ledger');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_pattern_occurrences_insert
BEFORE INSERT ON pattern_occurrences
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on pattern_occurrences');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_pattern_occurrences_update
BEFORE UPDATE ON pattern_occurrences
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on pattern_occurrences');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_pattern_occurrences_delete
BEFORE DELETE ON pattern_occurrences
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on pattern_occurrences');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_pattern_relationships_insert
BEFORE INSERT ON pattern_relationships
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on pattern_relationships');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_pattern_relationships_update
BEFORE UPDATE ON pattern_relationships
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on pattern_relationships');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_pattern_relationships_delete
BEFORE DELETE ON pattern_relationships
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on pattern_relationships');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_patterns_insert
BEFORE INSERT ON patterns
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on patterns');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_patterns_update
BEFORE UPDATE ON patterns
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on patterns');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_patterns_delete
BEFORE DELETE ON patterns
WHEN NOT EXISTS (
    SELECT 1 FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on patterns');
END;
