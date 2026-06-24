"""SQLite database connection and schema management.

Provides a simple, portable SQLite-backed persistence layer for memories.
The database file is stored in the user's data directory.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator


@dataclass
class DatabaseConfig:
    """Configuration for the SQLite database.

    Attributes:
        db_path: Path to the SQLite database file
        auto_commit: Whether to auto-commit transactions (default True)
    """

    db_path: Path
    auto_commit: bool = True


class Database:
    """SQLite database wrapper for OmniVia memory storage.

    Provides connection management, schema initialization, and basic
    transaction support. Thread-safe for read operations.

    Attributes:
        config: Database configuration
    """

    def __init__(self, config: DatabaseConfig) -> None:
        self.config = config
        self._connection: sqlite3.Connection | None = None

    def connect(self) -> None:
        """Establish connection to the database.

        Creates the database file and schema if they don't exist.
        """
        # Ensure parent directory exists
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            str(self.config.db_path),
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        """Get the database connection.

        Raises:
            RuntimeError: If not connected
        """
        if self._connection is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._connection

    def _init_schema(self) -> None:
        """Initialize the database schema.

        Creates the memories table and any other required tables.
        """
        cursor = self.connection.cursor()

        # Create memories table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
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
            )
        """)

        # Create indexes for common queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_lifecycle
            ON memories(lifecycle_state)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_created_at
            ON memories(created_at)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_type
            ON memories(memory_type)
        """)
        if self._ensure_column("memories", "workspace_id", "TEXT"):
            self._try_execute_schema("""
                CREATE INDEX IF NOT EXISTS idx_memories_workspace
                ON memories(workspace_id)
            """)

        # Create workspaces table for OmniVia Local workspace boundaries
        self._try_execute_schema("""
            CREATE TABLE IF NOT EXISTS workspaces (
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
            )
        """)
        self._try_execute_schema("""
            CREATE INDEX IF NOT EXISTS idx_workspaces_root_path
            ON workspaces(root_path)
        """)
        self._try_execute_schema("""
            CREATE INDEX IF NOT EXISTS idx_workspaces_status
            ON workspaces(index_status)
        """)

        # Create context_packs table for deterministic workspace exports
        self._try_execute_schema("""
            CREATE TABLE IF NOT EXISTS context_packs (
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
            )
        """)
        self._try_execute_schema("""
            CREATE INDEX IF NOT EXISTS idx_context_packs_workspace
            ON context_packs(workspace_id)
        """)
        self._try_execute_schema("""
            CREATE INDEX IF NOT EXISTS idx_context_packs_purpose
            ON context_packs(purpose)
        """)

        # Create graph_entities table for knowledge graph nodes
        # Agent-created entities start in "proposed" state for human review
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS graph_entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                source_id TEXT,
                approval_status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Create graph_relationships table for knowledge graph edges
        # Agent-created relationships start in "proposed" state for human review
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS graph_relationships (
                id TEXT PRIMARY KEY,
                source_entity_id TEXT NOT NULL,
                target_entity_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL,
                source_id TEXT,
                approval_status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Create indexes for graph queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_entities_type
            ON graph_entities(entity_type)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_entities_source
            ON graph_entities(source_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_entities_status
            ON graph_entities(approval_status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_relationships_source
            ON graph_relationships(source_entity_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_relationships_target
            ON graph_relationships(target_entity_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_relationships_type
            ON graph_relationships(relationship_type)
        """)

        # Create entity_memory_links table for tracking provenance
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entity_memory_links (
                entity_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (entity_id, memory_id)
            )
        """)

        # Create sources table for tracking ingested files
        # Enables provenance-backed recall: memories cite source files
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sources (
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
            )
        """)

        # Create chunks table for content segments from ingested files
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                start_offset INTEGER DEFAULT 0,
                end_offset INTEGER DEFAULT 0,
                content_hash TEXT,
                FOREIGN KEY (source_id) REFERENCES sources(id)
            )
        """)

        # Create indexes for source/chunk queries
        self._try_execute_schema("""
            CREATE INDEX IF NOT EXISTS idx_sources_path
            ON sources(file_path)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sources_hash
            ON sources(content_hash)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sources_status
            ON sources(parse_status)
        """)
        if self._ensure_column("sources", "workspace_id", "TEXT"):
            self._try_execute_schema("""
                CREATE INDEX IF NOT EXISTS idx_sources_workspace
                ON sources(workspace_id)
            """)
            self._try_execute_schema("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_workspace_path
                ON sources(workspace_id, file_path)
            """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_source
            ON chunks(source_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_hash
            ON chunks(content_hash)
        """)

        # Create patterns table for detected recurring knowledge
        # Agent-created patterns start in "proposed" state for human review
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS patterns (
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
            )
        """)

        # Create pattern_occurrences table for tracking which memories
        # triggered pattern detection
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pattern_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                evidence TEXT,
                detected_at TEXT NOT NULL,
                FOREIGN KEY (pattern_id) REFERENCES patterns(id)
            )
        """)

        # Create pattern_relationships table for pattern-to-pattern connections
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pattern_relationships (
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
            )
        """)

        # Create indexes for pattern queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_patterns_type
            ON patterns(pattern_type)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_patterns_source
            ON patterns(source_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_patterns_status
            ON patterns(approval_status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_patterns_confidence
            ON patterns(confidence)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_pattern_occurrences_pattern
            ON pattern_occurrences(pattern_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_pattern_occurrences_memory
            ON pattern_occurrences(memory_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_pattern_relationships_source
            ON pattern_relationships(source_pattern_id)
        """)

        self._try_execute_schema("""
            CREATE TABLE IF NOT EXISTS control_plane_manifests (
                workspace_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._try_execute_schema("""
            CREATE TABLE IF NOT EXISTS control_plane_resources (
                workspace_id TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workspace_id, resource_type, resource_id)
            )
        """)
        self._try_execute_schema("""
            CREATE INDEX IF NOT EXISTS idx_control_plane_resources_workspace
            ON control_plane_resources(workspace_id)
        """)
        self._try_execute_schema("""
            CREATE INDEX IF NOT EXISTS idx_control_plane_resources_type
            ON control_plane_resources(resource_type)
        """)
        self._try_execute_schema("""
            CREATE INDEX IF NOT EXISTS idx_control_plane_resources_lifecycle
            ON control_plane_resources(lifecycle)
        """)
        self._try_execute_schema("""
            CREATE TABLE IF NOT EXISTS control_plane_events (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        self._try_execute_schema("""
            CREATE INDEX IF NOT EXISTS idx_control_plane_events_workspace
            ON control_plane_events(workspace_id)
        """)
        self._try_execute_schema("""
            CREATE INDEX IF NOT EXISTS idx_control_plane_events_type
            ON control_plane_events(event_type)
        """)

        if self.config.auto_commit:
            self.connection.commit()

    def _ensure_column(
        self,
        table_name: str,
        column_name: str,
        column_definition: str,
    ) -> bool:
        """Add a column to an existing table if it is missing.

        Returns True when the column exists or was added. Read-only databases
        are tolerated for legacy inspection paths that do not need new schema.
        """
        cursor = self.connection.execute(f"PRAGMA table_info({table_name})")
        columns = {row["name"] for row in cursor.fetchall()}
        if column_name not in columns:
            try:
                self.connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
                )
            except sqlite3.OperationalError as exc:
                if "readonly database" in str(exc).lower():
                    return False
                raise
        return True

    def _try_execute_schema(self, query: str) -> None:
        """Execute optional schema SQL, tolerating read-only existing databases."""
        try:
            self.connection.execute(query)
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "readonly database" in message or message.startswith("no such table: main."):
                return
            raise

    def execute(
        self,
        query: str,
        params: tuple[Any, ...] | dict[str, Any] | None = None,
    ) -> sqlite3.Cursor:
        """Execute a SQL query.

        Args:
            query: SQL query string
            params: Query parameters

        Returns:
            Cursor with query results
        """
        cursor = self.connection.cursor()
        cursor.execute(query, params or ())
        if self.config.auto_commit:
            self.connection.commit()
        return cursor

    def executemany(
        self,
        query: str,
        params: list[tuple[Any, ...]],
    ) -> sqlite3.Cursor:
        """Execute a SQL query with multiple parameter sets.

        Args:
            query: SQL query string
            params: List of parameter tuples

        Returns:
            Cursor with query results
        """
        cursor = self.connection.cursor()
        cursor.executemany(query, params)
        if self.config.auto_commit:
            self.connection.commit()
        return cursor

    def commit(self) -> None:
        """Commit the current transaction."""
        self.connection.commit()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        self.connection.rollback()

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        """Context manager for explicit transactions.

        Usage:
            with db.transaction():
                db.execute("INSERT INTO ...", params)
                db.execute("UPDATE ...", params)
        """
        try:
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    @contextmanager
    def immediate_transaction(self) -> Generator[None, None, None]:
        """Context manager that holds a SQLite ``BEGIN IMMEDIATE`` write lock.

        This acquires SQLite's reserved write lock for the duration of the
        block, so a single local worker process holds it while it selects and
        conditionally writes a row. A second process that tries to start its own
        immediate transaction against the same database file blocks until this
        one commits/rolls back, or fails with ``sqlite3.OperationalError``
        ("database is locked") once the busy timeout elapses. Callers that want
        to fail closed on contention should catch that error.

        ``auto_commit`` is suspended for the block so :meth:`execute` does not
        commit (and end the transaction) between statements; the whole block
        commits on success and rolls back on any exception. The previous
        ``auto_commit`` setting and connection ``isolation_level`` are always
        restored.

        Scope/limitation: this is a local single-file SQLite write lock for a
        local worker fleet. It is NOT a distributed lock; a Cloud worker fleet
        spanning hosts/processes against a shared service still needs real
        distributed locking. It complements (does not replace) the payload
        compare-and-set guard used by the control-plane claim path.

        Only SQLite connections support ``BEGIN IMMEDIATE``; this wrapper assumes
        the connection is SQLite (the only backend this Database manages).
        """

        connection = self.connection
        # Flush any implicit pending transaction so BEGIN IMMEDIATE starts clean.
        if connection.in_transaction:
            connection.commit()

        previous_auto_commit = self.config.auto_commit
        previous_isolation = connection.isolation_level
        # Take manual control of transaction boundaries so the explicit
        # BEGIN IMMEDIATE/COMMIT/ROLLBACK statements are issued verbatim.
        connection.isolation_level = None
        self.config.auto_commit = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                # No active transaction to roll back (e.g. BEGIN IMMEDIATE itself
                # failed on lock contention); nothing to undo.
                pass
            raise
        finally:
            self.config.auto_commit = previous_auto_commit
            connection.isolation_level = previous_isolation


# Global database instance for convenience
_global_db: Database | None = None


def get_database(db_path: Path | str | None = None) -> Database:
    """Get or create the global database instance.

    Args:
        db_path: Optional path to the database file.
                 Defaults to ~/.omnivia/memories.db

    Returns:
        The global Database instance
    """
    global _global_db

    if _global_db is None:
        if db_path is None:
            # Default to ~/.omnivia/memories.db
            home = Path.home()
            db_dir = home / ".omnivia"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "memories.db"

        config = DatabaseConfig(db_path=Path(db_path))
        _global_db = Database(config)
        _global_db.connect()

    return _global_db


def reset_database() -> None:
    """Reset the global database instance.

    Useful for testing or when switching to a different database.
    """
    global _global_db

    if _global_db is not None:
        _global_db.close()
        _global_db = None
