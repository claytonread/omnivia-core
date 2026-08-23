"""Keyword search service for Phase 1.

Phase 1 uses simple keyword search. Vector/semantic search will be added later.
"""

from __future__ import annotations

from omnivia_memory.memory.models import Memory
from omnivia_memory.persistence.repositories import MemoryRepository


class SearchService:
    """Keyword-based search service.

    Phase 1 uses simple keyword matching against memory content.
    This provides a baseline that can be upgraded to vector search later.

    Note: Actual search is delegated to MemoryRepository which has
    direct database access for efficiency.
    """

    def __init__(self) -> None:
        pass

    def search(
        self,
        query: str,
        repository: MemoryRepository | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        """Search memories by keyword.

        Args:
            query: Search query string
            repository: Repository to search (optional)
            limit: Maximum results to return

        Returns:
            List of matching memories
        """
        if repository is None:
            return []

        return repository.search(query, limit)

    def rank_results(
        self,
        memories: list[Memory],
        query: str,
    ) -> list[Memory]:
        """Rank search results by relevance.

        Current implementation returns results in order from repository.
        Future versions could implement more sophisticated ranking.

        Args:
            memories: List of memories to rank
            query: Original search query

        Returns:
            Ranked list of memories
        """
        # Phase 1: Return as-is from repository
        # TODO: Implement relevance ranking based on:
        # - Exact matches
        # - Frequency of query terms
        # - Recency
        # - Approval state
        return memories

    def highlight_matches(
        self,
        memory: Memory,
        query: str,
    ) -> str:
        """Create a highlighted excerpt showing match context.

        Args:
            memory: The memory to excerpt
            query: Search query for highlighting

        Returns:
            Excerpt with match context
        """
        content: str = memory.content
        query_lower = query.lower()
        content_lower = content.lower()

        # Find the first occurrence
        idx = content_lower.find(query_lower)
        if idx == -1:
            return content[:200] + "..." if len(content) > 200 else content

        # Extract context around the match
        start = max(0, idx - 50)
        end = min(len(content), idx + len(query) + 100)

        excerpt = content[start:end]
        if start > 0:
            excerpt = "..." + excerpt
        if end < len(content):
            excerpt = excerpt + "..."

        return excerpt
