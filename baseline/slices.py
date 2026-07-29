"""The baseline slice required by the Phase 0 freeze.

T-0627 names the operations that must be baselined before the package,
contract, and workspace-ownership migrations. Encoding them as data means the
coverage requirement is checked rather than assumed: a slice with no golden
fixture and no declared surface entry fails the baseline check by name.

A slice is covered by one or both of:

- a golden fixture captured from a real Core code path (``baseline.scenarios``);
- a declared entry on a surface Core does not own (``baseline.surfaces``).

Slices whose behaviour lives entirely outside Core have no fixture. That is
expected and is recorded as an evidence gap, never as invented output.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BaselineSlice:
    """One required unit of the Phase 0 baseline."""

    id: str
    title: str
    group: str


REQUIRED_SLICES: tuple[BaselineSlice, ...] = (
    BaselineSlice("health.health", "Service health probe", "health"),
    BaselineSlice("health.readiness", "Service readiness probe", "health"),
    BaselineSlice("workspace.create", "Create a workspace", "workspace"),
    BaselineSlice("workspace.list", "List workspaces", "workspace"),
    BaselineSlice("workspace.get", "Get a workspace by id", "workspace"),
    BaselineSlice("memory.create", "Create a memory", "memory"),
    BaselineSlice("memory.list", "List memories", "memory"),
    BaselineSlice("memory.get", "Get a memory by id", "memory"),
    BaselineSlice("memory.search", "Keyword-search memories", "memory"),
    BaselineSlice("ingestion.import_directory", "Import a directory into a workspace", "ingestion"),
    BaselineSlice("graph.preview", "Bounded graph preview response", "graph"),
    BaselineSlice("graph.traversal", "Graph neighbours, backlinks, and path finding", "graph"),
    BaselineSlice("context.search", "Workspace-scoped context retrieval", "context"),
    BaselineSlice("context.pack", "Context pack assembly and storage", "context"),
    BaselineSlice("mcp.tools_discovery", "MCP tool discovery", "mcp"),
    BaselineSlice("mcp.read", "One MCP read operation", "mcp"),
    BaselineSlice("mcp.write", "One MCP write operation", "mcp"),
    BaselineSlice("cli.read", "One CLI read operation", "cli"),
    BaselineSlice("cli.write", "One CLI write operation", "cli"),
)

REQUIRED_SLICE_IDS: frozenset[str] = frozenset(item.id for item in REQUIRED_SLICES)

SLICES_BY_ID: dict[str, BaselineSlice] = {item.id: item for item in REQUIRED_SLICES}


def require_known_slice(slice_id: str, *, context: str) -> None:
    """Raise when a slice id is not part of the frozen Phase 0 slice."""
    if slice_id not in REQUIRED_SLICE_IDS:
        known = ", ".join(sorted(REQUIRED_SLICE_IDS))
        raise ValueError(f"{context}: unknown baseline slice {slice_id!r}; expected one of {known}")
