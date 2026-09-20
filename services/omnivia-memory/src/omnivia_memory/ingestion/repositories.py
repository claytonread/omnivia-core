"""Source and Chunk repositories for SQLite persistence."""

from __future__ import annotations

from typing import Any, cast

from omnivia_memory.ingestion.models import Chunk, FileType, ParseStatus, Source


class SourceRepository:
    """Repository for persisting and retrieving source records."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def create(self, source: Source) -> Source:
        """Store a new source record in the database."""
        existing = self.get_by_id(source.id)
        if existing is not None:
            raise ValueError(f"Source with ID {source.id} already exists")

        if self._has_workspace_column():
            self.db.execute(
                """
                INSERT INTO sources (
                    id, workspace_id, file_path, extension, size_bytes, modified_time,
                    file_type, content_hash, parse_status, error_message,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.id,
                    source.workspace_id,
                    source.path,
                    "",
                    source.size,
                    source.modified_time,
                    source.file_type.value,
                    source.hash,
                    source.status.value,
                    source.error,
                    source.created_at,
                    source.updated_at,
                ),
            )
        else:
            self.db.execute(
                """
                INSERT INTO sources (
                    id, file_path, extension, size_bytes, modified_time,
                    file_type, content_hash, parse_status, error_message,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.id,
                    source.path,
                    "",
                    source.size,
                    source.modified_time,
                    source.file_type.value,
                    source.hash,
                    source.status.value,
                    source.error,
                    source.created_at,
                    source.updated_at,
                ),
            )
        return source

    def get_by_id(self, source_id: str) -> Source | None:
        """Retrieve a source record by its ID."""
        cursor = self.db.execute(
            "SELECT * FROM sources WHERE id = ?",
            (source_id,),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return self._row_to_source(row)

    def get_by_path(self, path: str, workspace_id: str | None = None) -> Source | None:
        """Retrieve a source record by file path."""
        if workspace_id is None:
            cursor = self.db.execute(
                "SELECT * FROM sources WHERE file_path = ?",
                (path,),
            )
        elif not self._has_workspace_column():
            return None
        else:
            cursor = self.db.execute(
                "SELECT * FROM sources WHERE file_path = ? AND workspace_id = ?",
                (path, workspace_id),
            )
        row = cursor.fetchone()

        if row is None:
            return None

        return self._row_to_source(row)

    def get_by_hash(self, content_hash: str) -> Source | None:
        """Retrieve a source record by content hash."""
        cursor = self.db.execute(
            "SELECT * FROM sources WHERE content_hash = ?",
            (content_hash,),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return self._row_to_source(row)

    def list_all(
        self,
        limit: int = 100,
        offset: int = 0,
        file_type: FileType | None = None,
        status: ParseStatus | None = None,
        workspace_id: str | None = None,
    ) -> list[Source]:
        """List all source records, optionally filtered."""
        query = "SELECT * FROM sources"
        params: list[str | int] = []

        conditions = []
        if file_type is not None:
            conditions.append("file_type = ?")
            params.append(file_type.value)
        if status is not None:
            conditions.append("parse_status = ?")
            params.append(status.value)
        if workspace_id is not None:
            if not self._has_workspace_column():
                return []
            conditions.append("workspace_id = ?")
            params.append(workspace_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = self.db.execute(query, tuple(params))
        return [self._row_to_source(row) for row in cursor.fetchall()]

    def update(self, source: Source) -> Source:
        """Update an existing source record."""
        existing = self.get_by_id(source.id)
        if existing is None:
            raise ValueError(f"Source with ID {source.id} not found")

        source.touch()

        if self._has_workspace_column():
            self.db.execute(
                """
                UPDATE sources SET
                    file_path = ?,
                    workspace_id = ?,
                    file_type = ?,
                    size_bytes = ?,
                    content_hash = ?,
                    parse_status = ?,
                    modified_time = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    source.path,
                    source.workspace_id,
                    source.file_type.value,
                    source.size,
                    source.hash,
                    source.status.value,
                    source.modified_time,
                    source.error,
                    source.updated_at,
                    source.id,
                ),
            )
        else:
            self.db.execute(
                """
                UPDATE sources SET
                    file_path = ?,
                    file_type = ?,
                    size_bytes = ?,
                    content_hash = ?,
                    parse_status = ?,
                    modified_time = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    source.path,
                    source.file_type.value,
                    source.size,
                    source.hash,
                    source.status.value,
                    source.modified_time,
                    source.error,
                    source.updated_at,
                    source.id,
                ),
            )
        return source

    def delete(self, source_id: str) -> bool:
        """Delete a source record from the database."""
        cursor = self.db.execute(
            "DELETE FROM sources WHERE id = ?",
            (source_id,),
        )
        return bool(cursor.rowcount)

    def _row_to_source(self, row: Any) -> Source:
        """Convert a database row to a Source object."""
        return Source(
            id=row["id"],
            workspace_id=row["workspace_id"] if "workspace_id" in row.keys() else None,
            path=row["file_path"],
            file_type=FileType(row["file_type"]),
            size=row["size_bytes"],
            hash=row["content_hash"],
            status=ParseStatus(row["parse_status"]),
            modified_time=row["modified_time"],
            error=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_by_workspace(self, workspace_id: str) -> list[Source]:
        """List all source records for a workspace."""
        return self.list_all(workspace_id=workspace_id)

    def _has_workspace_column(self) -> bool:
        cursor = self.db.execute("PRAGMA table_info(sources)")
        return any(row["name"] == "workspace_id" for row in cursor.fetchall())


class ChunkRepository:
    """Repository for persisting and retrieving content chunks."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def create(self, chunk: Chunk) -> Chunk:
        """Store a new chunk in the database."""
        existing = self.get_by_id(chunk.id)
        if existing is not None:
            raise ValueError(f"Chunk with ID {chunk.id} already exists")

        self.db.execute(
            """
            INSERT INTO chunks (
                id, source_id, chunk_index, content,
                start_offset, end_offset, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk.id,
                chunk.source_id,
                chunk.chunk_index,
                chunk.content,
                chunk.start_offset,
                chunk.end_offset,
                chunk.content_hash,
            ),
        )
        return chunk

    def get_by_id(self, chunk_id: str) -> Chunk | None:
        """Retrieve a chunk by its ID."""
        cursor = self.db.execute(
            "SELECT * FROM chunks WHERE id = ?",
            (chunk_id,),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return self._row_to_chunk(row)

    def get_by_source_id(self, source_id: str) -> list[Chunk]:
        """Retrieve all chunks for a source record."""
        cursor = self.db.execute(
            "SELECT * FROM chunks WHERE source_id = ? ORDER BY chunk_index",
            (source_id,),
        )
        return [self._row_to_chunk(row) for row in cursor.fetchall()]

    def get_by_content_hash(self, content_hash: str) -> Chunk | None:
        """Retrieve a chunk by its content hash."""
        cursor = self.db.execute(
            "SELECT * FROM chunks WHERE content_hash = ? LIMIT 1",
            (content_hash,),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return self._row_to_chunk(row)

    def delete_by_source_id(self, source_id: str) -> int:
        """Delete all chunks for a source record."""
        cursor = self.db.execute(
            "DELETE FROM chunks WHERE source_id = ?",
            (source_id,),
        )
        return cast(int, cursor.rowcount)

    def _row_to_chunk(self, row: Any) -> Chunk:
        """Convert a database row to a Chunk object."""
        return Chunk(
            id=row["id"],
            source_id=row["source_id"],
            chunk_index=row["chunk_index"],
            content=row["content"],
            start_offset=row["start_offset"],
            end_offset=row["end_offset"],
            content_hash=row["content_hash"],
        )
