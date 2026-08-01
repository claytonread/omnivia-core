-- Bind the persisted guard predicate to the lease (T-0629F, SRB-06).
--
-- 0002 and 0003 asked only whether a guard row existed whose generation matched
-- `omnivia_workspace_state`. Both of those rows are ordinary rows, so an outside
-- writer could INSERT a guard row copied from workspace state and then write to any
-- guarded table -- one statement, no lease involved.
--
-- SB-03 bound guard ownership to the authoritative lease in the Python path, but the
-- triggers were left on the weaker predicate, so the two layers disagreed about what
-- authority means. The triggers are the layer that covers writers this runtime did
-- not create, which is exactly the case the weaker predicate let through. This
-- migration replaces every guard trigger with one that additionally requires the
-- guard row to agree with the current lease on workspace, generation and service
-- instance, and requires that lease to be live rather than released.
--
-- This raises the cost of forgery from one INSERT to a coordinated rewrite of the
-- lease as well; it does not make the workspace safe against the OS principal that
-- owns the file. ADR-037 says so directly, and that remains true: such a principal
-- can rewrite any row or drop these triggers offline. Drift is detected before
-- writable readiness, not prevented.
--
-- Triggers cannot be altered in place, so each is dropped and recreated.

DROP TRIGGER IF EXISTS omnivia_guard_chunks_delete;

CREATE TRIGGER omnivia_guard_chunks_delete
BEFORE DELETE ON chunks
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
WHEN NOT EXISTS (
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
