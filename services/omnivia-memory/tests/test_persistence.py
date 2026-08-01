"""Tests for the persistence module (database and repository)."""

import tempfile
from pathlib import Path

import pytest

from omnivia_memory.lifecycle.models import LifecycleState
from omnivia_memory.lifecycle.rules import CreatedBy
from omnivia_memory.memory.models import Memory
from omnivia_memory.persistence.database import (
    Database,
    DatabaseConfig,
    _ImplicitDatabasePathRefused,
    get_database,
    reset_database,
)
from omnivia_memory.persistence.repositories import MemoryRepository
from omnivia_memory.provenance.models import Source, SourceType


@pytest.fixture
def db_path():
    """Create a temporary database path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test.db"


@pytest.fixture
def database(db_path):
    """Create a database for testing."""
    config = DatabaseConfig(db_path=db_path)
    db = Database(config)
    db.connect()
    yield db
    db.close()


@pytest.fixture
def repository(database):
    """Create a repository for testing."""
    return MemoryRepository(database)


@pytest.fixture
def sample_memory():
    """Create a sample memory for testing."""

    def _create_memory(
        content="Test memory content",
        source_type=SourceType.FILE,
        source_ref="test.py",
        created_by=CreatedBy.AGENT,
    ):
        return Memory(
            content=content,
            source=Source(type=source_type, reference=source_ref),
            created_by=created_by,
        )

    return _create_memory


class TestDatabase:
    """Tests for Database class."""

    def test_database_creates_file(self, db_path):
        """Database creates the database file."""
        config = DatabaseConfig(db_path=db_path)
        db = Database(config)
        db.connect()

        assert db_path.exists()

        db.close()

    def test_database_schema_initialized(self, database):
        """Database initializes the schema on connect."""
        cursor = database.execute("""
            SELECT name FROM sqlite_master WHERE type='table' AND name='memories'
        """)
        result = cursor.fetchone()

        assert result is not None
        assert result["name"] == "memories"

    def test_database_indexes_created(self, database):
        """Database creates indexes."""
        cursor = database.execute("""
            SELECT name FROM sqlite_master WHERE type='index'
        """)
        indexes = [row["name"] for row in cursor.fetchall()]

        assert "idx_memories_lifecycle" in indexes
        assert "idx_memories_created_at" in indexes
        assert "idx_memories_type" in indexes

    def test_database_commit_and_rollback(self, db_path):
        """Database commit and rollback work correctly."""
        config = DatabaseConfig(db_path=db_path)
        db = Database(config)
        db.connect()

        # Execute without auto-commit
        config.auto_commit = False
        db.execute(
            "INSERT INTO memories (id, content, source_type, source_reference, "
            "source_description, lifecycle_state, memory_type, created_by, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test-id",
                "Test content",
                "file",
                "test.py",
                None,
                "proposed",
                "general",
                "agent",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )

        # Rollback
        db.rollback()

        # Memory should not exist
        cursor = db.execute("SELECT * FROM memories WHERE id = ?", ("test-id",))
        assert cursor.fetchone() is None

        db.close()

    def test_immediate_transaction_commits_rolls_back_and_restores_auto_commit(
        self, database
    ):
        """Immediate transactions are explicit and restore database settings."""
        assert database.config.auto_commit is True
        database.execute("CREATE TABLE immediate_txn_test (id TEXT PRIMARY KEY)")

        with database.immediate_transaction():
            database.execute(
                "INSERT INTO immediate_txn_test (id) VALUES (?)",
                ("committed",),
            )
            assert database.config.auto_commit is False

        assert database.config.auto_commit is True
        rows = database.execute("SELECT id FROM immediate_txn_test").fetchall()
        assert [row["id"] for row in rows] == ["committed"]

        with pytest.raises(RuntimeError, match="boom"):
            with database.immediate_transaction():
                database.execute(
                    "INSERT INTO immediate_txn_test (id) VALUES (?)",
                    ("rolled-back",),
                )
                raise RuntimeError("boom")

        assert database.config.auto_commit is True
        rows = database.execute("SELECT id FROM immediate_txn_test").fetchall()
        assert [row["id"] for row in rows] == ["committed"]


class TestMemoryRepository:
    """Tests for MemoryRepository class."""

    def test_create_memory(self, repository, sample_memory):
        """Can create a memory."""
        memory = sample_memory()
        result = repository.create(memory)

        assert result.id == memory.id

    def test_create_duplicate_raises_error(self, repository, sample_memory):
        """Creating memory with same ID raises error."""
        memory = sample_memory()
        repository.create(memory)

        with pytest.raises(ValueError, match="already exists"):
            repository.create(memory)

    def test_get_by_id(self, repository, sample_memory):
        """Can retrieve memory by ID."""
        memory = sample_memory()
        repository.create(memory)

        result = repository.get_by_id(memory.id)

        assert result is not None
        assert result.id == memory.id
        assert result.content == memory.content

    def test_get_by_id_not_found(self, repository):
        """Returns None for non-existent memory."""
        result = repository.get_by_id("non-existent-id")

        assert result is None

    def test_list_all(self, repository, sample_memory):
        """Can list all memories."""
        # Create several memories
        for i in range(5):
            memory = sample_memory(content=f"Memory {i}")
            repository.create(memory)

        result = repository.list_all()

        assert len(result) == 5

    def test_list_all_with_limit(self, repository, sample_memory):
        """Can limit list results."""
        for i in range(10):
            memory = sample_memory(content=f"Memory {i}")
            repository.create(memory)

        result = repository.list_all(limit=3)

        assert len(result) == 3

    def test_list_all_with_lifecycle_filter(self, repository, sample_memory):
        """Can filter by lifecycle state."""
        # Create proposed memories
        for i in range(3):
            memory = sample_memory(created_by=CreatedBy.AGENT)
            repository.create(memory)

        # Create approved memory (human-created)
        memory = sample_memory(created_by=CreatedBy.HUMAN)
        memory.lifecycle_state = LifecycleState.APPROVED
        repository.create(memory)

        result = repository.list_all(lifecycle_state=LifecycleState.PROPOSED)

        assert len(result) == 3

    def test_update_memory(self, repository, sample_memory):
        """Can update a memory."""
        memory = sample_memory(content="Original")
        repository.create(memory)

        memory.update_content("Updated")
        repository.update(memory)

        result = repository.get_by_id(memory.id)
        assert result.content == "Updated"

    def test_update_non_existent_raises_error(self, repository, sample_memory):
        """Updating non-existent memory raises error."""
        memory = sample_memory()

        with pytest.raises(ValueError, match="not found"):
            repository.update(memory)

    def test_delete_memory(self, repository, sample_memory):
        """Can delete a memory."""
        memory = sample_memory()
        repository.create(memory)

        result = repository.delete(memory.id)

        assert result is True
        assert repository.get_by_id(memory.id) is None

    def test_delete_non_existent_returns_false(self, repository):
        """Deleting non-existent memory returns False."""
        result = repository.delete("non-existent-id")

        assert result is False

    def test_search(self, repository, sample_memory):
        """Can search memories by keyword."""
        memory1 = sample_memory(content="Python is great for AI work")
        memory2 = sample_memory(content="TypeScript for web frontend")
        memory3 = sample_memory(content="Rust for systems programming")

        repository.create(memory1)
        repository.create(memory2)
        repository.create(memory3)

        result = repository.search("Python")

        assert len(result) >= 1
        assert any("Python" in m.content for m in result)

    def test_search_with_limit(self, repository, sample_memory):
        """Can limit search results."""
        for i in range(10):
            memory = sample_memory(content=f"Memory with keyword {i}")
            repository.create(memory)

        result = repository.search("keyword", limit=3)

        assert len(result) == 3

    def test_count(self, repository, sample_memory):
        """Can count memories."""
        for i in range(5):
            memory = sample_memory()
            repository.create(memory)

        total = repository.count()
        proposed = repository.count(LifecycleState.PROPOSED)

        assert total == 5
        assert proposed == 5

    def test_count_with_filter(self, repository, sample_memory):
        """Can count with lifecycle filter."""
        # Create proposed
        for i in range(3):
            memory = sample_memory(created_by=CreatedBy.AGENT)
            repository.create(memory)

        # Create approved
        for i in range(2):
            memory = sample_memory(created_by=CreatedBy.HUMAN)
            memory.lifecycle_state = LifecycleState.APPROVED
            repository.create(memory)

        proposed_count = repository.count(LifecycleState.PROPOSED)
        approved_count = repository.count(LifecycleState.APPROVED)

        assert proposed_count == 3
        assert approved_count == 2


class TestGetDatabase:
    """Tests for get_database function."""

    def test_get_database_refuses_an_implicit_path(self):
        """get_database has no default path (T-0629F).

        This test previously asserted the opposite: that a bare call created a
        database "in the default location". That location was the developer's real
        ~/.omnivia/memories.db, created as a side effect of a getter, and it sits
        outside any workspace so it can be neither migrated nor fenced. Removing the
        fallback is the mutation cutover, so the test now pins the refusal.
        """
        reset_database()

        with pytest.raises(_ImplicitDatabasePathRefused) as raised:
            get_database()

        assert "explicit db_path" in str(raised.value)
        assert "~/.omnivia/memories.db" in str(raised.value)

        reset_database()

    def test_get_database_returns_same_instance(self, db_path):
        """get_database returns a singleton once given an explicit path."""
        reset_database()

        db1 = get_database(db_path)
        db2 = get_database(db_path)

        assert db1 is db2
        assert isinstance(db1, Database)

        reset_database()

    def test_get_database_with_custom_path(self, db_path):
        """get_database can use custom path."""
        reset_database()

        db = get_database(db_path)

        assert db.config.db_path == db_path

        reset_database()
