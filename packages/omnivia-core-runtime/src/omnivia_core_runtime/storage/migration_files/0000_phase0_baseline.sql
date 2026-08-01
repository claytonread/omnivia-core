-- OmniVia Phase 0 legacy baseline schema (T-0627 freeze).
-- FROZEN ARTIFACT. Extracted once from the legacy runtime at commit 55f2489 and
-- checked in so the Phase 0 oracle no longer regenerates itself from live code.
-- Do not edit: a change here silently moves the migration oracle it defines.

CREATE TABLE chunks (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                start_offset INTEGER DEFAULT 0,
                end_offset INTEGER DEFAULT 0,
                content_hash TEXT,
                FOREIGN KEY (source_id) REFERENCES sources(id)
            );
CREATE TABLE context_packs (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                purpose TEXT NOT NULL,
                query TEXT,
                memory_ids_json TEXT NOT NULL DEFAULT '[]',
                source_references_json TEXT NOT NULL DEFAULT '[]',
                format TEXT NOT NULL DEFAULT 'markdown',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
            );
CREATE TABLE control_plane_events (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
CREATE TABLE control_plane_manifests (
                workspace_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
CREATE TABLE control_plane_resources (
                workspace_id TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workspace_id, resource_type, resource_id)
            );
CREATE TABLE entity_memory_links (
                entity_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (entity_id, memory_id)
            );
CREATE TABLE graph_entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                source_id TEXT,
                approval_status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
CREATE TABLE graph_relationships (
                id TEXT PRIMARY KEY,
                source_entity_id TEXT NOT NULL,
                target_entity_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL,
                source_id TEXT,
                approval_status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                content TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_reference TEXT NOT NULL,
                source_description TEXT,
                lifecycle_state TEXT NOT NULL DEFAULT 'proposed',
                memory_type TEXT NOT NULL DEFAULT 'general',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
CREATE TABLE pattern_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                evidence TEXT,
                detected_at TEXT NOT NULL,
                FOREIGN KEY (pattern_id) REFERENCES patterns(id)
            );
CREATE TABLE pattern_relationships (
                id TEXT PRIMARY KEY,
                source_pattern_id TEXT NOT NULL,
                target_pattern_id TEXT,
                related_memory_id TEXT,
                relationship_type TEXT NOT NULL DEFAULT 'exemplifies',
                source_id TEXT,
                approval_status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (source_pattern_id) REFERENCES patterns(id)
            );
CREATE TABLE patterns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                pattern_type TEXT NOT NULL,
                description TEXT NOT NULL,
                confidence REAL NOT NULL,
                occurrence_count INTEGER NOT NULL,
                source_id TEXT,
                approval_status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
CREATE TABLE sources (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                file_path TEXT NOT NULL,
                extension TEXT,
                size_bytes INTEGER,
                modified_time TEXT,
                file_type TEXT NOT NULL,
                content_hash TEXT,
                parse_status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
CREATE TABLE workspaces (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                root_path TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                description TEXT,
                index_status TEXT NOT NULL DEFAULT 'unindexed',
                settings_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_indexed_at TEXT
            );
CREATE INDEX idx_chunks_hash
            ON chunks(content_hash);
CREATE INDEX idx_chunks_source
            ON chunks(source_id);
CREATE INDEX idx_context_packs_purpose
            ON context_packs(purpose);
CREATE INDEX idx_context_packs_workspace
            ON context_packs(workspace_id);
CREATE INDEX idx_control_plane_events_type
            ON control_plane_events(event_type);
CREATE INDEX idx_control_plane_events_workspace
            ON control_plane_events(workspace_id);
CREATE INDEX idx_control_plane_resources_lifecycle
            ON control_plane_resources(lifecycle);
CREATE INDEX idx_control_plane_resources_type
            ON control_plane_resources(resource_type);
CREATE INDEX idx_control_plane_resources_workspace
            ON control_plane_resources(workspace_id);
CREATE INDEX idx_graph_entities_source
            ON graph_entities(source_id);
CREATE INDEX idx_graph_entities_status
            ON graph_entities(approval_status);
CREATE INDEX idx_graph_entities_type
            ON graph_entities(entity_type);
CREATE INDEX idx_graph_relationships_source
            ON graph_relationships(source_entity_id);
CREATE INDEX idx_graph_relationships_target
            ON graph_relationships(target_entity_id);
CREATE INDEX idx_graph_relationships_type
            ON graph_relationships(relationship_type);
CREATE INDEX idx_memories_created_at
            ON memories(created_at);
CREATE INDEX idx_memories_lifecycle
            ON memories(lifecycle_state);
CREATE INDEX idx_memories_type
            ON memories(memory_type);
CREATE INDEX idx_memories_workspace
                ON memories(workspace_id);
CREATE INDEX idx_pattern_occurrences_memory
            ON pattern_occurrences(memory_id);
CREATE INDEX idx_pattern_occurrences_pattern
            ON pattern_occurrences(pattern_id);
CREATE INDEX idx_pattern_relationships_source
            ON pattern_relationships(source_pattern_id);
CREATE INDEX idx_patterns_confidence
            ON patterns(confidence);
CREATE INDEX idx_patterns_source
            ON patterns(source_id);
CREATE INDEX idx_patterns_status
            ON patterns(approval_status);
CREATE INDEX idx_patterns_type
            ON patterns(pattern_type);
CREATE INDEX idx_sources_hash
            ON sources(content_hash);
CREATE INDEX idx_sources_path
            ON sources(file_path);
CREATE INDEX idx_sources_status
            ON sources(parse_status);
CREATE INDEX idx_sources_workspace
                ON sources(workspace_id);
CREATE UNIQUE INDEX idx_sources_workspace_path
                ON sources(workspace_id, file_path);
CREATE INDEX idx_workspaces_root_path
            ON workspaces(root_path);
CREATE INDEX idx_workspaces_status
            ON workspaces(index_status);
