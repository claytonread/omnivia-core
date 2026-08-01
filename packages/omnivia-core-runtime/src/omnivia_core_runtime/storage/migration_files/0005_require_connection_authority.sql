-- Require connection-local authority to mutate (T-0629F, SRB-09).
--
-- 0004 bound the guard row to the lease, which stopped a single forged INSERT. It did
-- not close the crash window: a service killed between `open_guard` and `close_guard`
-- leaves a guard row and a held lease that agree with each other, and no predicate
-- over rows alone can tell that pair from a live owner -- liveness is not a fact
-- about the database.
--
-- It does not have to be. `omnivia_service_writer` is registered with `create_function`
-- on connections this runtime opens, so it exists per-connection and cannot be
-- created by writing rows. A stock `sqlite3` client evaluating this predicate fails
-- with "no such function" before any row is touched, whatever the guard and lease
-- say. That is the invariant the trigger layer was always reaching for: writers this
-- runtime did not create do not mutate this workspace.
--
-- Reads are untouched -- these are DML triggers -- so ordinary tooling can still
-- inspect a workspace, and `VACUUM INTO` backups still work.
--
-- This is a fail-closed check, not a security boundary. ADR-037 is unchanged: the OS
-- principal that owns the file can drop these triggers offline. What it can no longer
-- do is walk in with `sqlite3` and write through a guard row left behind by a crash.
--
-- Triggers cannot be altered in place, so each is dropped and recreated.

DROP TRIGGER IF EXISTS omnivia_guard_chunks_delete;

CREATE TRIGGER omnivia_guard_chunks_delete
BEFORE DELETE ON chunks
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on chunks');
END;

DROP TRIGGER IF EXISTS omnivia_guard_chunks_insert;

CREATE TRIGGER omnivia_guard_chunks_insert
BEFORE INSERT ON chunks
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on chunks');
END;

DROP TRIGGER IF EXISTS omnivia_guard_chunks_update;

CREATE TRIGGER omnivia_guard_chunks_update
BEFORE UPDATE ON chunks
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on chunks');
END;

DROP TRIGGER IF EXISTS omnivia_guard_context_packs_delete;

CREATE TRIGGER omnivia_guard_context_packs_delete
BEFORE DELETE ON context_packs
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on context_packs');
END;

DROP TRIGGER IF EXISTS omnivia_guard_context_packs_insert;

CREATE TRIGGER omnivia_guard_context_packs_insert
BEFORE INSERT ON context_packs
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on context_packs');
END;

DROP TRIGGER IF EXISTS omnivia_guard_context_packs_update;

CREATE TRIGGER omnivia_guard_context_packs_update
BEFORE UPDATE ON context_packs
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on context_packs');
END;

DROP TRIGGER IF EXISTS omnivia_guard_control_plane_events_delete;

CREATE TRIGGER omnivia_guard_control_plane_events_delete
BEFORE DELETE ON control_plane_events
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on control_plane_events');
END;

DROP TRIGGER IF EXISTS omnivia_guard_control_plane_events_insert;

CREATE TRIGGER omnivia_guard_control_plane_events_insert
BEFORE INSERT ON control_plane_events
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on control_plane_events');
END;

DROP TRIGGER IF EXISTS omnivia_guard_control_plane_events_update;

CREATE TRIGGER omnivia_guard_control_plane_events_update
BEFORE UPDATE ON control_plane_events
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on control_plane_events');
END;

DROP TRIGGER IF EXISTS omnivia_guard_control_plane_manifests_delete;

CREATE TRIGGER omnivia_guard_control_plane_manifests_delete
BEFORE DELETE ON control_plane_manifests
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on control_plane_manifests');
END;

DROP TRIGGER IF EXISTS omnivia_guard_control_plane_manifests_insert;

CREATE TRIGGER omnivia_guard_control_plane_manifests_insert
BEFORE INSERT ON control_plane_manifests
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on control_plane_manifests');
END;

DROP TRIGGER IF EXISTS omnivia_guard_control_plane_manifests_update;

CREATE TRIGGER omnivia_guard_control_plane_manifests_update
BEFORE UPDATE ON control_plane_manifests
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on control_plane_manifests');
END;

DROP TRIGGER IF EXISTS omnivia_guard_control_plane_resources_delete;

CREATE TRIGGER omnivia_guard_control_plane_resources_delete
BEFORE DELETE ON control_plane_resources
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on control_plane_resources');
END;

DROP TRIGGER IF EXISTS omnivia_guard_control_plane_resources_insert;

CREATE TRIGGER omnivia_guard_control_plane_resources_insert
BEFORE INSERT ON control_plane_resources
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on control_plane_resources');
END;

DROP TRIGGER IF EXISTS omnivia_guard_control_plane_resources_update;

CREATE TRIGGER omnivia_guard_control_plane_resources_update
BEFORE UPDATE ON control_plane_resources
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on control_plane_resources');
END;

DROP TRIGGER IF EXISTS omnivia_guard_durable_jobs_insert;

CREATE TRIGGER omnivia_guard_durable_jobs_insert
BEFORE INSERT ON omnivia_durable_jobs
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_durable_jobs');
END;

DROP TRIGGER IF EXISTS omnivia_guard_durable_jobs_update;

CREATE TRIGGER omnivia_guard_durable_jobs_update
BEFORE UPDATE ON omnivia_durable_jobs
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_durable_jobs');
END;

DROP TRIGGER IF EXISTS omnivia_guard_entity_memory_links_delete;

CREATE TRIGGER omnivia_guard_entity_memory_links_delete
BEFORE DELETE ON entity_memory_links
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on entity_memory_links');
END;

DROP TRIGGER IF EXISTS omnivia_guard_entity_memory_links_insert;

CREATE TRIGGER omnivia_guard_entity_memory_links_insert
BEFORE INSERT ON entity_memory_links
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on entity_memory_links');
END;

DROP TRIGGER IF EXISTS omnivia_guard_entity_memory_links_update;

CREATE TRIGGER omnivia_guard_entity_memory_links_update
BEFORE UPDATE ON entity_memory_links
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on entity_memory_links');
END;

DROP TRIGGER IF EXISTS omnivia_guard_graph_entities_delete;

CREATE TRIGGER omnivia_guard_graph_entities_delete
BEFORE DELETE ON graph_entities
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on graph_entities');
END;

DROP TRIGGER IF EXISTS omnivia_guard_graph_entities_insert;

CREATE TRIGGER omnivia_guard_graph_entities_insert
BEFORE INSERT ON graph_entities
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on graph_entities');
END;

DROP TRIGGER IF EXISTS omnivia_guard_graph_entities_update;

CREATE TRIGGER omnivia_guard_graph_entities_update
BEFORE UPDATE ON graph_entities
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on graph_entities');
END;

DROP TRIGGER IF EXISTS omnivia_guard_graph_relationships_delete;

CREATE TRIGGER omnivia_guard_graph_relationships_delete
BEFORE DELETE ON graph_relationships
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on graph_relationships');
END;

DROP TRIGGER IF EXISTS omnivia_guard_graph_relationships_insert;

CREATE TRIGGER omnivia_guard_graph_relationships_insert
BEFORE INSERT ON graph_relationships
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on graph_relationships');
END;

DROP TRIGGER IF EXISTS omnivia_guard_graph_relationships_update;

CREATE TRIGGER omnivia_guard_graph_relationships_update
BEFORE UPDATE ON graph_relationships
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on graph_relationships');
END;

DROP TRIGGER IF EXISTS omnivia_guard_memories_delete;

CREATE TRIGGER omnivia_guard_memories_delete
BEFORE DELETE ON memories
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on memories');
END;

DROP TRIGGER IF EXISTS omnivia_guard_memories_insert;

CREATE TRIGGER omnivia_guard_memories_insert
BEFORE INSERT ON memories
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on memories');
END;

DROP TRIGGER IF EXISTS omnivia_guard_memories_update;

CREATE TRIGGER omnivia_guard_memories_update
BEFORE UPDATE ON memories
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on memories');
END;

DROP TRIGGER IF EXISTS omnivia_guard_omnivia_durable_jobs_delete;

CREATE TRIGGER omnivia_guard_omnivia_durable_jobs_delete
BEFORE DELETE ON omnivia_durable_jobs
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_durable_jobs');
END;

DROP TRIGGER IF EXISTS omnivia_guard_omnivia_projection_ledger_delete;

CREATE TRIGGER omnivia_guard_omnivia_projection_ledger_delete
BEFORE DELETE ON omnivia_projection_ledger
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_projection_ledger');
END;

DROP TRIGGER IF EXISTS omnivia_guard_pattern_occurrences_delete;

CREATE TRIGGER omnivia_guard_pattern_occurrences_delete
BEFORE DELETE ON pattern_occurrences
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on pattern_occurrences');
END;

DROP TRIGGER IF EXISTS omnivia_guard_pattern_occurrences_insert;

CREATE TRIGGER omnivia_guard_pattern_occurrences_insert
BEFORE INSERT ON pattern_occurrences
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on pattern_occurrences');
END;

DROP TRIGGER IF EXISTS omnivia_guard_pattern_occurrences_update;

CREATE TRIGGER omnivia_guard_pattern_occurrences_update
BEFORE UPDATE ON pattern_occurrences
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on pattern_occurrences');
END;

DROP TRIGGER IF EXISTS omnivia_guard_pattern_relationships_delete;

CREATE TRIGGER omnivia_guard_pattern_relationships_delete
BEFORE DELETE ON pattern_relationships
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on pattern_relationships');
END;

DROP TRIGGER IF EXISTS omnivia_guard_pattern_relationships_insert;

CREATE TRIGGER omnivia_guard_pattern_relationships_insert
BEFORE INSERT ON pattern_relationships
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on pattern_relationships');
END;

DROP TRIGGER IF EXISTS omnivia_guard_pattern_relationships_update;

CREATE TRIGGER omnivia_guard_pattern_relationships_update
BEFORE UPDATE ON pattern_relationships
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on pattern_relationships');
END;

DROP TRIGGER IF EXISTS omnivia_guard_patterns_delete;

CREATE TRIGGER omnivia_guard_patterns_delete
BEFORE DELETE ON patterns
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on patterns');
END;

DROP TRIGGER IF EXISTS omnivia_guard_patterns_insert;

CREATE TRIGGER omnivia_guard_patterns_insert
BEFORE INSERT ON patterns
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on patterns');
END;

DROP TRIGGER IF EXISTS omnivia_guard_patterns_update;

CREATE TRIGGER omnivia_guard_patterns_update
BEFORE UPDATE ON patterns
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on patterns');
END;

DROP TRIGGER IF EXISTS omnivia_guard_projection_ledger_insert;

CREATE TRIGGER omnivia_guard_projection_ledger_insert
BEFORE INSERT ON omnivia_projection_ledger
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_projection_ledger');
END;

DROP TRIGGER IF EXISTS omnivia_guard_projection_ledger_update;

CREATE TRIGGER omnivia_guard_projection_ledger_update
BEFORE UPDATE ON omnivia_projection_ledger
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_projection_ledger');
END;

DROP TRIGGER IF EXISTS omnivia_guard_sources_delete;

CREATE TRIGGER omnivia_guard_sources_delete
BEFORE DELETE ON sources
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on sources');
END;

DROP TRIGGER IF EXISTS omnivia_guard_sources_insert;

CREATE TRIGGER omnivia_guard_sources_insert
BEFORE INSERT ON sources
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on sources');
END;

DROP TRIGGER IF EXISTS omnivia_guard_sources_update;

CREATE TRIGGER omnivia_guard_sources_update
BEFORE UPDATE ON sources
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on sources');
END;

DROP TRIGGER IF EXISTS omnivia_guard_workspaces_delete;

CREATE TRIGGER omnivia_guard_workspaces_delete
BEFORE DELETE ON workspaces
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on workspaces');
END;

DROP TRIGGER IF EXISTS omnivia_guard_workspaces_insert;

CREATE TRIGGER omnivia_guard_workspaces_insert
BEFORE INSERT ON workspaces
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on workspaces');
END;

DROP TRIGGER IF EXISTS omnivia_guard_workspaces_update;

CREATE TRIGGER omnivia_guard_workspaces_update
BEFORE UPDATE ON workspaces
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on workspaces');
END;
