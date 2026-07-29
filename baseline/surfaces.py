"""Declared inventories for baseline surfaces Core does not own.

Core ships no HTTP server, no MCP server, and no product CLI: those surfaces
live in Platform and Dev. This module records, mechanically:

- which operations of the Phase 0 slice each external surface must baseline;
- which Core contract each operation is grounded in, verified by importing it;
- the full neutral (Platform) or base (Dev) operation inventory as reviewed at
  source in the owning repository, plus the exclusion rules that keep
  Module-specific operations out of it;
- the descriptor for each required operation, together with where that
  descriptor was read from;
- the exact command that closes each remaining gap, and the gap id it closes.

Descriptors here come from a read-only review of the owning repositories, not
from a live capture. Every artifact records that distinction: captured evidence
carries ``capture_kind: "reviewed_static_source"`` and
``live_response_captured: false``. A descriptor with no reviewed source stays
null and keeps an *open* gap, and the guard in :func:`_verify_descriptor`
rejects any descriptor that appears without evidence.

Two further guards make the inventories checkable rather than declarative:
every recorded descriptor must appear in the surface's frozen inventory, and
nothing in that inventory — or in a later capture — may match an exclusion
rule. That is how ``/local/**``, ``/dev/**``, and the Dev-only MCP and CLI
extensions are kept out of the neutral and base surfaces by a check instead of
by a convention.

When Platform or Dev produces a live capture artifact,
``verify_external_capture`` diffs it against the declaration and fails with the
missing, extra, excluded, or un-inventoried operation ids by name.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from baseline import BASELINE_FORMAT_VERSION, BASELINE_TASK_ID, INVENTORY_DIR, REPO_ROOT
from baseline.determinism import load_json, write_artifact
from baseline.slices import REQUIRED_SLICE_IDS, require_known_slice

PLATFORM_HTTP_PATH = INVENTORY_DIR / "platform-http-routes.json"
MCP_TOOLS_PATH = INVENTORY_DIR / "mcp-tools.json"
CLI_COMMANDS_PATH = INVENTORY_DIR / "cli-commands.json"
EVIDENCE_GAPS_PATH = INVENTORY_DIR / "evidence-gaps.json"

CORE_REPO = "omnivia-core"

#: How a descriptor came to be recorded. Reviewed source is a read-only pass
#: over the owning repository; it is deliberately not the same thing as a live
#: response capture, and the artifacts say so.
CAPTURE_KIND_REVIEWED_SOURCE = "reviewed_static_source"

GAP_SCOPES = frozenset({"surface", "capability", "process", "evidence"})
GAP_STATUSES = frozenset({"open", "closed"})
EXCLUSION_KINDS = frozenset({"prefix", "exact"})


@dataclass(frozen=True)
class EvidenceSource:
    """Where an external surface's descriptors were read from."""

    repo: str
    path: str
    kind: str
    references: tuple[str, ...]
    live_response_captured: bool
    note: str

    @property
    def captured_from(self) -> str:
        return f"{self.repo}:{self.path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "path": self.path,
            "kind": self.kind,
            "captured_from": self.captured_from,
            "references": list(self.references),
            "live_response_captured": self.live_response_captured,
            "note": self.note,
        }


@dataclass(frozen=True)
class ExclusionRule:
    """One rule that keeps an operation out of a neutral or base inventory."""

    kind: str  # prefix | exact
    value: str
    reason: str

    def matches(self, identity: str) -> bool:
        if self.kind == "prefix":
            return identity.startswith(self.value)
        return identity == self.value

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value, "reason": self.reason}


@dataclass(frozen=True)
class InventoryEntry:
    """One operation on the owning repo's surface, as reviewed at source."""

    identity: str
    method: str | None = None
    kind: str = "operation"  # operation | protocol_method
    note: str | None = None

    def to_dict(self, spec: SurfaceSpec) -> dict[str, Any]:
        entry: dict[str, Any] = {spec.identity_field: self.identity, "kind": self.kind}
        if self.method is not None:
            entry["method"] = self.method
        if self.note is not None:
            entry["note"] = self.note
        return entry


@dataclass(frozen=True)
class SurfaceSpec:
    """How one external surface inventory is shaped and validated."""

    id: str
    title: str
    owner_repo: str
    artifact: Path
    #: Descriptor keys every captured operation must fill in.
    descriptor_fields: tuple[str, ...]
    #: Baseline slices this surface is responsible for.
    required_slices: tuple[str, ...]
    #: The descriptor field an exclusion rule is applied to.
    identity_field: str
    #: The descriptor fields that identify an inventory entry.
    match_fields: tuple[str, ...]
    inventory_kind: str
    inventory: tuple[InventoryEntry, ...]
    exclusions: tuple[ExclusionRule, ...]
    inventory_notes: tuple[str, ...]
    evidence_source: EvidenceSource
    status_note: str
    response_envelope: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SurfaceEntry:
    """One declared operation on an external surface."""

    operation_id: str
    baseline_slice: str
    title: str
    core_contracts: tuple[str, ...]
    gap_id: str
    notes: str
    #: Filled in from reviewed source. ``None`` means no evidence exists yet.
    descriptor: dict[str, str] | None = None

    def to_dict(self, spec: SurfaceSpec) -> dict[str, Any]:
        if self.descriptor is None:
            evidence: dict[str, Any] = {
                "captured": False,
                "gap_id": self.gap_id,
                "capture_command": CAPTURE_COMMANDS[spec.id],
            }
        else:
            evidence = {
                "captured": True,
                "capture_kind": spec.evidence_source.kind,
                "captured_from": spec.evidence_source.captured_from,
                "gap_id": self.gap_id,
                "live_response_captured": spec.evidence_source.live_response_captured,
            }
        return {
            "operation_id": self.operation_id,
            "baseline_slice": self.baseline_slice,
            "title": self.title,
            "owner_repo": spec.owner_repo,
            "core_contracts": list(self.core_contracts),
            "descriptor": dict(self.descriptor) if self.descriptor is not None else None,
            "descriptor_fields": list(spec.descriptor_fields),
            "notes": self.notes,
            "evidence": evidence,
        }


@dataclass(frozen=True)
class EvidenceGap:
    """A piece of external evidence, recorded rather than guessed."""

    id: str
    scope: str  # surface | capability | process | evidence
    title: str
    owner_repo: str
    why_open: str
    closes_when: str
    status: str = "open"  # open | closed
    #: What actually closed the gap. Required once ``status`` is ``closed``.
    closed_by: str | None = None
    #: The still-open gap a closed gap handed its remainder to, if any.
    residual_gap_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "title": self.title,
            "owner_repo": self.owner_repo,
            "why_open": self.why_open,
            "closes_when": self.closes_when,
            "status": self.status,
            "closed_by": self.closed_by,
            "residual_gap_id": self.residual_gap_id,
        }


CAPTURE_COMMANDS: dict[str, str] = {
    "platform_http": (
        "In omnivia-platform, export the neutral route table for this slice as JSON "
        "with descriptor {method, path}, evidence.captured=true and a live response "
        "body, then run `python -m baseline verify-external --surface platform_http "
        "--artifact <file>` from omnivia-core."
    ),
    "mcp_tools": (
        "In omnivia-dev, export the base MCP tool list for this slice as JSON with "
        "descriptor {tool_name, operation}, evidence.captured=true and a live tool "
        "result, then run `python -m baseline verify-external --surface mcp_tools "
        "--artifact <file>` from omnivia-core."
    ),
    "cli_commands": (
        "In omnivia-dev, export the base CLI command list for this slice as JSON with "
        "descriptor {command, operation}, evidence.captured=true and a live command "
        "output, then run `python -m baseline verify-external --surface cli_commands "
        "--artifact <file>` from omnivia-core."
    ),
}

# --------------------------------------------------------------------------- #
# Reviewed evidence: Platform HTTP
# --------------------------------------------------------------------------- #

PLATFORM_EVIDENCE = EvidenceSource(
    repo="omnivia-platform",
    path="services/omnivia-memory-platform/src/omnivia_memory_platform/http/server.py",
    kind=CAPTURE_KIND_REVIEWED_SOURCE,
    references=(
        "http/server.py lines 52-73",
        "http/server.py lines 83-180",
        "http/server.py lines 203-279",
        "http/app.py lines 214-220",
        "http/app.py lines 1370-1396",
        "http/app.py lines 2218-2550",
        "http/app.py lines 2428-2494",
        "http/app.py lines 2626-2784",
        "http/app.py lines 2786-2835",
    ),
    live_response_captured=False,
    note=(
        "Route table and response envelope read from the owning repository by a "
        "read-only review pass and applied here. No live HTTP response body was "
        "captured, so the golden fixtures for these slices remain Core-side "
        "equivalents rather than Platform responses."
    ),
)

_PLATFORM_DEPRECATED_GET = (
    "A GET variant of this operation exists in Platform but is deprecated; the "
    "baseline freezes the POST form."
)

_PLATFORM_ROUTES: tuple[InventoryEntry, ...] = (
    InventoryEntry("/health", method="GET"),
    InventoryEntry("/workspaces", method="GET"),
    InventoryEntry("/workspaces", method="POST"),
    InventoryEntry("/workspaces/{id}", method="GET"),
    InventoryEntry("/workspaces/{id}/index", method="POST"),
    InventoryEntry("/workspaces/{id}/ingestion/queue", method="GET"),
    InventoryEntry("/ingestion/runs", method="GET"),
    InventoryEntry("/memories", method="GET"),
    InventoryEntry("/memories", method="POST"),
    InventoryEntry("/memories/{id}", method="GET"),
    InventoryEntry("/memories/search", method="GET"),
    InventoryEntry("/memory/search", method="GET"),
    InventoryEntry("/memory/source", method="GET"),
    InventoryEntry("/memory/graph/preview", method="GET"),
    InventoryEntry("/memory/evidence/graph", method="GET"),
    InventoryEntry("/memory/evidence/explain", method="GET"),
    InventoryEntry("/agent/context/search", method="POST", note=_PLATFORM_DEPRECATED_GET),
    InventoryEntry("/agent/context/pack", method="POST", note=_PLATFORM_DEPRECATED_GET),
)

_PLATFORM_EXCLUSIONS: tuple[ExclusionRule, ...] = (
    ExclusionRule(
        kind="prefix",
        value="/local/",
        reason=(
            "Local-only Platform routes are Module surface, not the neutral surface "
            "Core is baselined against. They are excluded from this inventory by rule."
        ),
    ),
    ExclusionRule(
        kind="prefix",
        value="/dev/",
        reason=(
            "Dev-only Platform routes belong to the Dev Module, not the neutral "
            "surface Core is baselined against."
        ),
    ),
)

_PLATFORM_ENVELOPE: tuple[tuple[str, str], ...] = (
    ("success", 'Most success responses are {"ok": true, "data": <payload>}.'),
    ("health", 'GET /health responds {"ok": <bool>, "service": <string>, "message": <string>}.'),
    ("error", 'Errors are an HTTP status code plus {"ok": false, "error": <string>}.'),
    (
        "unexpected_error",
        (
            'Unexpected errors use the fixed message "Internal server error" rather than '
            "leaking detail."
        ),
    ),
)

# --------------------------------------------------------------------------- #
# Reviewed evidence: Dev MCP
# --------------------------------------------------------------------------- #

MCP_EVIDENCE = EvidenceSource(
    repo="omnivia-dev",
    path="services/omnivia-memory-dev/src/omnivia_memory_dev/mcp/server.py",
    kind=CAPTURE_KIND_REVIEWED_SOURCE,
    references=("mcp/server.py lines 234-1061",),
    live_response_captured=False,
    note=(
        "Base tool list read from the owning repository by a read-only review pass "
        "and applied here. No live MCP tool result was captured, so the mcp.* golden "
        "fixtures stay empty rather than claiming a Dev response."
    ),
)

_MCP_TOOLS: tuple[InventoryEntry, ...] = (
    InventoryEntry("tools/list", kind="protocol_method"),
    InventoryEntry("context_pack_generate"),
    InventoryEntry("context_pack_get"),
    InventoryEntry("context_pack_list"),
    InventoryEntry("context_search"),
    InventoryEntry("graph_create_entity"),
    InventoryEntry("graph_create_relationship"),
    InventoryEntry("graph_get_context"),
    InventoryEntry("graph_get_entity"),
    InventoryEntry("graph_get_neighbors"),
    InventoryEntry("graph_list_entities"),
    InventoryEntry("graph_search"),
    InventoryEntry("memory_approve"),
    InventoryEntry("memory_delete"),
    InventoryEntry("memory_get"),
    InventoryEntry("memory_list"),
    InventoryEntry("memory_reject"),
    InventoryEntry("memory_search"),
    InventoryEntry("memory_store"),
    InventoryEntry("memory_update"),
    InventoryEntry("platform_context_pack"),
    InventoryEntry("workspace_create"),
    InventoryEntry("workspace_delete"),
    InventoryEntry("workspace_get"),
    InventoryEntry("workspace_import"),
    InventoryEntry("workspace_list"),
    InventoryEntry("workspace_update"),
)

_MCP_CONSOLIDATION_REASON = (
    "Dev knowledge-consolidation tools. They are Dev Module surface and are excluded "
    "from the base tool inventory Core is baselined against."
)

_MCP_EXCLUSIONS: tuple[ExclusionRule, ...] = (
    ExclusionRule(
        kind="prefix",
        value="pattern_",
        reason="Dev pattern tools are Dev Module surface, not base Core-relevant tools.",
    ),
    ExclusionRule(
        kind="prefix",
        value="codebase_intelligence_",
        reason=(
            "Dev codebase-intelligence tools are Dev Module surface, not base "
            "Core-relevant tools."
        ),
    ),
    ExclusionRule(kind="exact", value="consolidate_knowledge", reason=_MCP_CONSOLIDATION_REASON),
    ExclusionRule(kind="exact", value="get_knowledge_unit", reason=_MCP_CONSOLIDATION_REASON),
    ExclusionRule(kind="exact", value="list_knowledge_units", reason=_MCP_CONSOLIDATION_REASON),
    ExclusionRule(kind="exact", value="detect_conflicts", reason=_MCP_CONSOLIDATION_REASON),
    ExclusionRule(kind="exact", value="get_consolidated_view", reason=_MCP_CONSOLIDATION_REASON),
    ExclusionRule(kind="exact", value="resolve_conflict", reason=_MCP_CONSOLIDATION_REASON),
)

# --------------------------------------------------------------------------- #
# Reviewed evidence: Dev CLI
# --------------------------------------------------------------------------- #

CLI_EVIDENCE = EvidenceSource(
    repo="omnivia-dev",
    path="services/omnivia-memory-dev/src/omnivia_memory_dev/cli/commands.py",
    kind=CAPTURE_KIND_REVIEWED_SOURCE,
    references=("cli/commands.py lines 1527-1722",),
    live_response_captured=False,
    note=(
        "Base command list read from the owning repository by a read-only review pass "
        "and applied here. No live command output was captured, so the cli.* golden "
        "fixtures stay empty rather than claiming a Dev response."
    ),
)

_CLI_COMMANDS: tuple[InventoryEntry, ...] = (
    InventoryEntry("approve"),
    InventoryEntry("context-pack generate"),
    InventoryEntry("context-pack get"),
    InventoryEntry("context-pack list"),
    InventoryEntry("create"),
    InventoryEntry("delete"),
    InventoryEntry("get"),
    InventoryEntry("ingest"),
    InventoryEntry("list"),
    InventoryEntry("reject"),
    InventoryEntry("search"),
    InventoryEntry("sources"),
    InventoryEntry("stats"),
    InventoryEntry("update"),
    InventoryEntry("workspace create"),
    InventoryEntry("workspace delete"),
    InventoryEntry("workspace get"),
    InventoryEntry("workspace import"),
    InventoryEntry("workspace list"),
    InventoryEntry("workspace update"),
)

_CLI_SEAM_REASON = (
    "Dev observability and evidence seams are Dev Module surface. The review named "
    "the seam group without enumerating its command names, so this stays a rule "
    "rather than a list of commands Core has not seen."
)

_CLI_EXCLUSIONS: tuple[ExclusionRule, ...] = (
    ExclusionRule(
        kind="prefix",
        value="codebase-intelligence",
        reason=(
            "Dev codebase-intelligence commands (search, map, context) are Dev Module "
            "surface, not base Core-relevant commands."
        ),
    ),
    ExclusionRule(kind="prefix", value="observability", reason=_CLI_SEAM_REASON),
    ExclusionRule(kind="prefix", value="evidence", reason=_CLI_SEAM_REASON),
)

_SHARED_STATUS_NOTE = (
    "Descriptors come from a read-only review of the owning repository, recorded "
    "with evidence.capture_kind='reviewed_static_source'. No live response body was "
    "captured, so live_response_captured stays false. An operation with no reviewed "
    "descriptor keeps a null descriptor and an open gap."
)

SURFACES: dict[str, SurfaceSpec] = {
    "platform_http": SurfaceSpec(
        id="platform_http",
        title="Neutral Platform HTTP routes relevant to Core",
        owner_repo="omnivia-platform",
        artifact=PLATFORM_HTTP_PATH,
        descriptor_fields=("method", "path"),
        required_slices=(
            "health.health",
            "health.readiness",
            "workspace.create",
            "workspace.list",
            "workspace.get",
            "memory.create",
            "memory.list",
            "memory.get",
            "memory.search",
            "ingestion.import_directory",
            "graph.preview",
            "graph.traversal",
            "context.search",
            "context.pack",
        ),
        identity_field="path",
        match_fields=("method", "path"),
        inventory_kind="neutral_http_routes",
        inventory=_PLATFORM_ROUTES,
        exclusions=_PLATFORM_EXCLUSIONS,
        inventory_notes=(
            "The full neutral route table, not only the routes this slice requires.",
            (
                "Every /local/** and /dev/** route is excluded by rule. They are Module "
                "surface and are not part of the neutral surface Core is baselined against."
            ),
        ),
        evidence_source=PLATFORM_EVIDENCE,
        status_note=_SHARED_STATUS_NOTE,
        response_envelope=_PLATFORM_ENVELOPE,
    ),
    "mcp_tools": SurfaceSpec(
        id="mcp_tools",
        title="Base MCP tools relevant to Core",
        owner_repo="omnivia-dev",
        artifact=MCP_TOOLS_PATH,
        descriptor_fields=("tool_name", "operation"),
        required_slices=("mcp.tools_discovery", "mcp.read", "mcp.write"),
        identity_field="tool_name",
        match_fields=("tool_name",),
        inventory_kind="base_mcp_tools",
        inventory=_MCP_TOOLS,
        exclusions=_MCP_EXCLUSIONS,
        inventory_notes=(
            "The full base tool list, plus the tools/list protocol method discovery uses.",
            (
                "Dev-only extensions — pattern tools, the knowledge-consolidation tools, "
                "and the codebase-intelligence tools — are excluded by rule."
            ),
        ),
        evidence_source=MCP_EVIDENCE,
        status_note=_SHARED_STATUS_NOTE,
    ),
    "cli_commands": SurfaceSpec(
        id="cli_commands",
        title="Base CLI commands relevant to Core",
        owner_repo="omnivia-dev",
        artifact=CLI_COMMANDS_PATH,
        descriptor_fields=("command", "operation"),
        required_slices=("cli.read", "cli.write"),
        identity_field="command",
        match_fields=("command",),
        inventory_kind="base_cli_commands",
        inventory=_CLI_COMMANDS,
        exclusions=_CLI_EXCLUSIONS,
        inventory_notes=(
            (
                "The full base command list, including the workspace and context-pack "
                "command groups."
            ),
            (
                "Dev-only extensions — the codebase-intelligence commands and the Dev "
                "observability and evidence seams — are excluded by rule."
            ),
        ),
        evidence_source=CLI_EVIDENCE,
        status_note=_SHARED_STATUS_NOTE,
    ),
}

_WORKSPACE_CONTRACTS = (
    "omnivia_memory.workspace.models.Workspace",
    "omnivia_memory.workspace.service.WorkspaceService",
)
_MEMORY_CONTRACTS = (
    "omnivia_memory.memory.models.Memory",
    "omnivia_memory.memory.service.MemoryService",
)

DECLARED_ENTRIES: dict[str, tuple[SurfaceEntry, ...]] = {
    "platform_http": (
        SurfaceEntry(
            operation_id="health.health",
            baseline_slice="health.health",
            title="Service health probe",
            core_contracts=(),
            gap_id="GAP-004",
            descriptor={"method": "GET", "path": "/health"},
            notes=(
                "Health is a Platform runtime concern. Core has no health primitive, so "
                "no Core-side fixture exists for it. This route is the one neutral "
                "operation that does not use the {ok, data} envelope."
            ),
        ),
        SurfaceEntry(
            operation_id="health.readiness",
            baseline_slice="health.readiness",
            title="Service readiness probe",
            core_contracts=(),
            gap_id="GAP-004",
            notes=(
                "Readiness depends on Platform storage and lifecycle wiring that Core "
                "does not own. The reviewed neutral route table exposes GET /health only "
                "and has no separate readiness route, so the descriptor stays null rather "
                "than being guessed from the health route."
            ),
        ),
        SurfaceEntry(
            operation_id="workspace.create",
            baseline_slice="workspace.create",
            title="Create a workspace",
            core_contracts=(
                "omnivia_memory.workspace.models.WorkspaceCreate",
                *_WORKSPACE_CONTRACTS,
            ),
            gap_id="GAP-001",
            descriptor={"method": "POST", "path": "/workspaces"},
            notes="Core-side behaviour is frozen by the workspace.create golden fixture.",
        ),
        SurfaceEntry(
            operation_id="workspace.list",
            baseline_slice="workspace.list",
            title="List workspaces",
            core_contracts=_WORKSPACE_CONTRACTS,
            gap_id="GAP-001",
            descriptor={"method": "GET", "path": "/workspaces"},
            notes="Core-side behaviour is frozen by the workspace.list golden fixture.",
        ),
        SurfaceEntry(
            operation_id="workspace.get",
            baseline_slice="workspace.get",
            title="Get a workspace by id",
            core_contracts=_WORKSPACE_CONTRACTS,
            gap_id="GAP-001",
            descriptor={"method": "GET", "path": "/workspaces/{id}"},
            notes="Core-side behaviour is frozen by the workspace.get golden fixture.",
        ),
        SurfaceEntry(
            operation_id="memory.create",
            baseline_slice="memory.create",
            title="Create a memory",
            core_contracts=("omnivia_memory.memory.models.MemoryCreate", *_MEMORY_CONTRACTS),
            gap_id="GAP-001",
            descriptor={"method": "POST", "path": "/memories"},
            notes="Core-side behaviour is frozen by the memory.create golden fixture.",
        ),
        SurfaceEntry(
            operation_id="memory.list",
            baseline_slice="memory.list",
            title="List memories",
            core_contracts=_MEMORY_CONTRACTS,
            gap_id="GAP-001",
            descriptor={"method": "GET", "path": "/memories"},
            notes="Core-side behaviour is frozen by the memory.list golden fixture.",
        ),
        SurfaceEntry(
            operation_id="memory.get",
            baseline_slice="memory.get",
            title="Get a memory by id",
            core_contracts=_MEMORY_CONTRACTS,
            gap_id="GAP-001",
            descriptor={"method": "GET", "path": "/memories/{id}"},
            notes="Core-side behaviour is frozen by the memory.get golden fixture.",
        ),
        SurfaceEntry(
            operation_id="memory.search",
            baseline_slice="memory.search",
            title="Keyword-search memories",
            core_contracts=("omnivia_memory.search.service.SearchService", *_MEMORY_CONTRACTS),
            gap_id="GAP-001",
            descriptor={"method": "GET", "path": "/memories/search"},
            notes=(
                "Core-side behaviour is frozen by the memory.search golden fixture. The "
                "neutral table also carries GET /memory/search as an agent-facing variant; "
                "this slice is mapped to the collection route."
            ),
        ),
        SurfaceEntry(
            operation_id="ingestion.import_directory",
            baseline_slice="ingestion.import_directory",
            title="Import a directory into a workspace",
            core_contracts=(
                "omnivia_memory.ingestion.pipeline.IngestionPipeline",
                "omnivia_memory.workspace.models.ImportSummary",
            ),
            gap_id="GAP-001",
            descriptor={"method": "POST", "path": "/workspaces/{id}/index"},
            notes=(
                "Core-side behaviour is frozen by the ingestion.import_directory fixture. "
                "Platform runs the import through its indexing route; the queue and run "
                "routes in the inventory are Platform-side scheduling that Core has no "
                "equivalent for."
            ),
        ),
        SurfaceEntry(
            operation_id="graph.preview",
            baseline_slice="graph.preview",
            title="Bounded graph preview response",
            core_contracts=(
                "omnivia_memory.memory_graph.assembly.assemble_graph_preview",
                "omnivia_memory.memory_graph.models.GraphPreviewResponse",
            ),
            gap_id="GAP-001",
            descriptor={"method": "GET", "path": "/memory/graph/preview"},
            notes="Core-side behaviour is frozen by the graph.preview golden fixture.",
        ),
        SurfaceEntry(
            operation_id="graph.traversal",
            baseline_slice="graph.traversal",
            title="Graph neighbours, backlinks, and path finding",
            core_contracts=("omnivia_memory.graph.service.GraphService",),
            gap_id="GAP-001",
            descriptor={"method": "GET", "path": "/memory/evidence/graph"},
            notes=(
                "Core-side behaviour is frozen by the graph.traversal golden fixture. The "
                "reviewed neutral table has no dedicated neighbours, backlinks, or "
                "path-finding route; GET /memory/evidence/graph is the neutral operation "
                "that exposes traversal, so the slice is mapped to it. Treat the mapping, "
                "not the route, as the reviewable claim here."
            ),
        ),
        SurfaceEntry(
            operation_id="context.search",
            baseline_slice="context.search",
            title="Workspace-scoped context retrieval",
            core_contracts=(
                "omnivia_memory.persistence.repositories.MemoryRepository",
                *_MEMORY_CONTRACTS,
            ),
            gap_id="GAP-001",
            descriptor={"method": "POST", "path": "/agent/context/search"},
            notes=(
                "Core-side behaviour is frozen by the context.search golden fixture. The "
                "POST form is frozen because the GET variant is deprecated in Platform."
            ),
        ),
        SurfaceEntry(
            operation_id="context.pack",
            baseline_slice="context.pack",
            title="Context pack assembly and storage",
            core_contracts=("omnivia_memory.persistence.database.Database",),
            gap_id="GAP-005",
            descriptor={"method": "POST", "path": "/agent/context/pack"},
            notes=(
                "The route is recorded from reviewed source, but Core owns the "
                "context_packs storage schema and implements no pack assembly, so only "
                "the storage-schema half is frozen here. The POST form is frozen because "
                "the GET variant is deprecated in Platform."
            ),
        ),
    ),
    "mcp_tools": (
        SurfaceEntry(
            operation_id="mcp.tools_discovery",
            baseline_slice="mcp.tools_discovery",
            title="MCP tool discovery",
            core_contracts=(),
            gap_id="GAP-002",
            descriptor={"tool_name": "tools/list", "operation": "discovery"},
            notes=(
                "Tool discovery is a Dev MCP server concern; Core exposes no MCP surface. "
                "tools/list is the MCP protocol method rather than one of the base tools, "
                "and the inventory records it as such."
            ),
        ),
        SurfaceEntry(
            operation_id="mcp.read",
            baseline_slice="mcp.read",
            title="One MCP read operation over Core memory",
            core_contracts=_MEMORY_CONTRACTS,
            gap_id="GAP-002",
            descriptor={"tool_name": "memory_get", "operation": "read"},
            notes=(
                "The underlying Core read behaviour is frozen by the memory.get and "
                "memory.search golden fixtures. memory_get is the required baseline "
                "mapping for the read slice."
            ),
        ),
        SurfaceEntry(
            operation_id="mcp.write",
            baseline_slice="mcp.write",
            title="One MCP write operation over Core memory",
            core_contracts=("omnivia_memory.memory.models.MemoryCreate", *_MEMORY_CONTRACTS),
            gap_id="GAP-002",
            descriptor={"tool_name": "memory_store", "operation": "write"},
            notes=(
                "The underlying Core write behaviour is frozen by the memory.create golden "
                "fixture. memory_store is the required baseline mapping for the write slice."
            ),
        ),
    ),
    "cli_commands": (
        SurfaceEntry(
            operation_id="cli.read",
            baseline_slice="cli.read",
            title="One CLI read operation over Core memory",
            core_contracts=_MEMORY_CONTRACTS,
            gap_id="GAP-003",
            descriptor={"command": "get", "operation": "read"},
            notes=(
                "The CLI equivalent of the MCP read operation, so the two surfaces stay "
                "comparable after the migration: `get` pairs with memory_get."
            ),
        ),
        SurfaceEntry(
            operation_id="cli.write",
            baseline_slice="cli.write",
            title="One CLI write operation over Core memory",
            core_contracts=("omnivia_memory.memory.models.MemoryCreate", *_MEMORY_CONTRACTS),
            gap_id="GAP-003",
            descriptor={"command": "create", "operation": "write"},
            notes=(
                "The CLI equivalent of the MCP write operation, so the two surfaces stay "
                "comparable after the migration: `create` pairs with memory_store."
            ),
        ),
    ),
}

EVIDENCE_GAPS: tuple[EvidenceGap, ...] = (
    EvidenceGap(
        id="GAP-001",
        scope="surface",
        title="Platform HTTP route descriptors were not observable from Core",
        owner_repo="omnivia-platform",
        why_open=(
            "Core ships no HTTP server, so the concrete method and path for each route "
            "could not be observed from this repository and the descriptors were left null."
        ),
        closes_when=(
            "The neutral Platform route table is recorded in Core from reviewed source, "
            "or a Platform-side capture artifact passes "
            "`python -m baseline verify-external --surface platform_http --artifact <file>`."
        ),
        status="closed",
        closed_by=(
            "A read-only review of omnivia-platform "
            "services/omnivia-memory-platform/src/omnivia_memory_platform/http/server.py "
            "(with http/app.py) supplied the neutral route table, the response envelope, "
            "and the /local/** and /dev/** exclusions. They are recorded in "
            "baseline/inventories/platform-http-routes.json with reviewed-source evidence."
        ),
        residual_gap_id="GAP-007",
    ),
    EvidenceGap(
        id="GAP-002",
        scope="surface",
        title="Base MCP tool descriptors were not observable from Core",
        owner_repo="omnivia-dev",
        why_open=(
            "MCP serving lives in Dev. Core has no MCP module, so tool names could not be "
            "observed from this repository and the descriptors were left null."
        ),
        closes_when=(
            "The base MCP tool list is recorded in Core from reviewed source, or a "
            "Dev-side capture artifact passes "
            "`python -m baseline verify-external --surface mcp_tools --artifact <file>`."
        ),
        status="closed",
        closed_by=(
            "A read-only review of omnivia-dev "
            "services/omnivia-memory-dev/src/omnivia_memory_dev/mcp/server.py supplied the "
            "base tool list, the tools/list discovery method, the memory_get and "
            "memory_store baseline mapping, and the Dev-only exclusions. They are recorded "
            "in baseline/inventories/mcp-tools.json with reviewed-source evidence."
        ),
        residual_gap_id="GAP-008",
    ),
    EvidenceGap(
        id="GAP-003",
        scope="surface",
        title="Base CLI command descriptors were not observable from Core",
        owner_repo="omnivia-dev",
        why_open=(
            "The product CLI lives in Dev. The only argparse entry points in Core are the "
            "benchmark runners, so the command names could not be observed here."
        ),
        closes_when=(
            "The base CLI command list is recorded in Core from reviewed source, or a "
            "Dev-side capture artifact passes "
            "`python -m baseline verify-external --surface cli_commands --artifact <file>`."
        ),
        status="closed",
        closed_by=(
            "A read-only review of omnivia-dev "
            "services/omnivia-memory-dev/src/omnivia_memory_dev/cli/commands.py supplied "
            "the base command list, the get and create baseline mapping, and the Dev-only "
            "exclusions. They are recorded in baseline/inventories/cli-commands.json with "
            "reviewed-source evidence."
        ),
        residual_gap_id="GAP-008",
    ),
    EvidenceGap(
        id="GAP-004",
        scope="capability",
        title="Readiness has no neutral route and health has no Core implementation",
        owner_repo="omnivia-platform",
        why_open=(
            "Core exposes no health or readiness primitive, so there is no Core code path "
            "to capture a golden fixture from. The reviewed neutral route table also "
            "exposes GET /health only, so the health.readiness operation has no descriptor."
        ),
        closes_when=(
            "Platform names a neutral readiness route and a live response is captured "
            "under GAP-007, or Core gains a health primitive in a later phase."
        ),
    ),
    EvidenceGap(
        id="GAP-005",
        scope="capability",
        title="Context pack assembly behaviour is not implemented in Core",
        owner_repo="omnivia-platform",
        why_open=(
            "Core owns the context_packs table schema but no pack assembly code. The "
            "context.pack fixture therefore freezes the storage schema captured from "
            "Database._init_schema, not a pack response body, even though the Platform "
            "route POST /agent/context/pack is now recorded."
        ),
        closes_when=(
            "A Platform context pack response is captured under GAP-007, or pack assembly "
            "moves into Core in a later phase."
        ),
    ),
    EvidenceGap(
        id="GAP-006",
        scope="process",
        title="PM operating-model documents were not read in this lane",
        owner_repo="omnivia-pm",
        why_open=(
            "AGENTS.md points at omnivia-pm operating-model and task-template documents. "
            "This task was scoped to omnivia-core only, so those documents were not opened "
            "by the implementation lane."
        ),
        closes_when=(
            "A reviewer with omnivia-pm access confirms the Phase 0 artifacts match the "
            "operating model."
        ),
        status="closed",
        closed_by=(
            "Codex reviewed and applied the accepted T-0627 task, the readiness "
            "assessment, and ADR-036, ADR-037 and ADR-038 against these artifacts."
        ),
    ),
    EvidenceGap(
        id="GAP-007",
        scope="evidence",
        title="Live Platform HTTP responses are not captured in Core",
        owner_repo="omnivia-platform",
        why_open=(
            "The neutral route table and response envelope are recorded from reviewed "
            "source, which fixes the shape of the surface but not its output. No live "
            "Platform response body has been captured, so the Platform-facing slices are "
            "frozen by Core-side equivalents only."
        ),
        closes_when=(
            "A Platform-side capture artifact carrying live response bodies passes "
            "`python -m baseline verify-external --surface platform_http --artifact <file>`."
        ),
    ),
    EvidenceGap(
        id="GAP-008",
        scope="evidence",
        title="Live MCP and CLI responses are not captured in Core",
        owner_repo="omnivia-dev",
        why_open=(
            "The base MCP tool list and CLI command list are recorded from reviewed "
            "source, which fixes the shape of both surfaces but not their output. No live "
            "tool result or command output has been captured, so the mcp.* and cli.* "
            "fixtures record no response."
        ),
        closes_when=(
            "Dev-side capture artifacts carrying live tool results and command output pass "
            "`python -m baseline verify-external` for the mcp_tools and cli_commands "
            "surfaces."
        ),
    ),
)

GAPS_BY_ID: dict[str, EvidenceGap] = {gap.id: gap for gap in EVIDENCE_GAPS}


def excluded_by(surface_id: str, identity: str) -> ExclusionRule | None:
    """Return the exclusion rule an operation identity matches, if any."""
    for rule in SURFACES[surface_id].exclusions:
        if rule.matches(identity):
            return rule
    return None


def build_surface_inventory(surface_id: str) -> dict[str, Any]:
    """Return the canonical declared inventory for one external surface."""
    spec = SURFACES[surface_id]
    entries = DECLARED_ENTRIES[surface_id]
    return {
        "format_version": BASELINE_FORMAT_VERSION,
        "task": BASELINE_TASK_ID,
        "surface": spec.id,
        "title": spec.title,
        "owner_repo": spec.owner_repo,
        "descriptor_fields": list(spec.descriptor_fields),
        "required_slices": sorted(spec.required_slices),
        "status": "reviewed_static_evidence",
        "status_note": spec.status_note,
        "evidence_source": spec.evidence_source.to_dict(),
        "inventory": {
            "kind": spec.inventory_kind,
            "identity_field": spec.identity_field,
            "match_fields": list(spec.match_fields),
            "entries": [entry.to_dict(spec) for entry in spec.inventory],
            "exclusions": [rule.to_dict() for rule in spec.exclusions],
            "notes": list(spec.inventory_notes),
        },
        "response_envelope": dict(spec.response_envelope) if spec.response_envelope else None,
        "operations": [entry.to_dict(spec) for entry in entries],
    }


def build_evidence_gap_register() -> dict[str, Any]:
    """Return the canonical evidence gap register."""
    return {
        "format_version": BASELINE_FORMAT_VERSION,
        "task": BASELINE_TASK_ID,
        "gaps": [gap.to_dict() for gap in sorted(EVIDENCE_GAPS, key=lambda gap: gap.id)],
    }


def write_surface_inventories() -> list[Path]:
    """Regenerate every tracked surface inventory and the gap register."""
    written = []
    for surface_id, spec in sorted(SURFACES.items()):
        write_artifact(spec.artifact, build_surface_inventory(surface_id))
        written.append(spec.artifact)
    write_artifact(EVIDENCE_GAPS_PATH, build_evidence_gap_register())
    written.append(EVIDENCE_GAPS_PATH)
    return written


def verify_surface_inventories() -> list[str]:
    """Validate the tracked surface inventories and gap register.

    Returns precise problem strings; an empty list means the declarations are
    internally consistent, grounded in real Core contracts, free of excluded or
    un-inventoried operations, and free of descriptors that no evidence backs.
    """
    problems: list[str] = []
    gap_register = load_json(EVIDENCE_GAPS_PATH)
    gaps = {gap["id"]: gap for gap in gap_register.get("gaps", [])}
    problems.extend(_verify_gap_register(gap_register))

    referenced_gaps: set[str] = set()
    for surface_id, spec in sorted(SURFACES.items()):
        document = load_json(spec.artifact)
        expected = build_surface_inventory(surface_id)
        if document != expected:
            problems.append(
                f"{_rel(spec.artifact)}: tracked inventory differs from baseline.surfaces "
                "declarations; regenerate with `python -m baseline capture`"
            )
        problems.extend(_verify_surface_document(document, spec, gaps, referenced_gaps))

    for gap in gap_register.get("gaps", []):
        if gap.get("scope") == "surface" and gap["id"] not in referenced_gaps:
            problems.append(
                f"{_rel(EVIDENCE_GAPS_PATH)}: gap {gap['id']} has scope 'surface' but no "
                "surface operation references it; remove it or reference it"
            )
    return problems


def verify_external_capture(surface_id: str, artifact: Path) -> list[str]:
    """Validate a capture artifact produced by Platform or Dev.

    This is how the remaining live-response gaps close: the owning repo exports
    its live surface in the declared shape, and Core checks coverage, descriptor
    completeness, inventory membership, and the exclusion rules without ever
    importing private code.
    """
    if surface_id not in SURFACES:
        known = ", ".join(sorted(SURFACES))
        return [f"unknown surface {surface_id!r}; expected one of {known}"]
    spec = SURFACES[surface_id]
    try:
        document = load_json(artifact)
    except (FileNotFoundError, ValueError) as exc:
        return [f"{artifact}: {exc}"]

    problems: list[str] = []
    if document.get("surface") != spec.id:
        problems.append(
            f"{artifact}: surface is {document.get('surface')!r}, expected {spec.id!r}"
        )

    operations = document.get("operations")
    if not isinstance(operations, list):
        return [*problems, f"{artifact}: 'operations' must be a list"]

    declared_ids = {entry.operation_id for entry in DECLARED_ENTRIES[surface_id]}
    captured_ids: set[str] = set()
    for index, operation in enumerate(operations):
        location = f"{artifact}: operations[{index}]"
        if not isinstance(operation, Mapping):
            problems.append(f"{location}: must be an object")
            continue
        operation_id = operation.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            # Without an id the entry cannot be matched to a declared operation,
            # so there is nothing further worth reporting about it.
            problems.append(f"{location}: operation_id must be a non-empty string")
            continue
        captured_ids.add(operation_id)
        evidence = operation.get("evidence")
        if not isinstance(evidence, Mapping) or evidence.get("captured") is not True:
            problems.append(
                f"{location} ({operation_id}): evidence.captured must be true in a "
                "capture artifact"
            )
            continue
        if not str(evidence.get("captured_from") or "").strip():
            problems.append(
                f"{location} ({operation_id}): evidence.captured_from must name the "
                "repo, commit, or command the descriptor came from"
            )
        descriptor_problems = _verify_descriptor(operation, spec, location, required=True)
        problems.extend(descriptor_problems)
        if not descriptor_problems:
            problems.extend(_verify_descriptor_inventory(operation["descriptor"], spec, location))

    for missing in sorted(declared_ids - captured_ids):
        problems.append(f"{artifact}: declared operation {missing!r} is missing from the capture")
    for extra in sorted(captured_ids - declared_ids):
        problems.append(
            f"{artifact}: captured operation {extra!r} is not declared in baseline.surfaces; "
            "extend DECLARED_ENTRIES deliberately before accepting it"
        )
    return problems


def _verify_gap_register(document: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    gaps = document.get("gaps")
    if not isinstance(gaps, list) or not gaps:
        return [f"{_rel(EVIDENCE_GAPS_PATH)}: 'gaps' must be a non-empty list"]
    by_id = {gap.get("id"): gap for gap in gaps}
    seen: set[str] = set()
    for gap in gaps:
        gap_id = gap.get("id")
        if gap_id in seen:
            problems.append(f"{_rel(EVIDENCE_GAPS_PATH)}: duplicate gap id {gap_id!r}")
        seen.add(gap_id)
        if gap.get("scope") not in GAP_SCOPES:
            problems.append(
                f"{_rel(EVIDENCE_GAPS_PATH)}: gap {gap_id!r} has scope "
                f"{gap.get('scope')!r}; expected one of {sorted(GAP_SCOPES)}"
            )
        for gap_field in ("title", "owner_repo", "why_open", "closes_when"):
            if not str(gap.get(gap_field) or "").strip():
                problems.append(
                    f"{_rel(EVIDENCE_GAPS_PATH)}: gap {gap_id!r} is missing {gap_field}"
                )
        if gap.get("owner_repo") == CORE_REPO:
            problems.append(
                f"{_rel(EVIDENCE_GAPS_PATH)}: gap {gap_id!r} is owned by {CORE_REPO}; an "
                "evidence gap records work outside this repository"
            )
        problems.extend(_verify_gap_status(gap, by_id))
    return problems


def _verify_gap_status(
    gap: Mapping[str, Any],
    by_id: Mapping[Any, Mapping[str, Any]],
) -> list[str]:
    """A closed gap must say what closed it, and hand any remainder to an open gap."""
    problems: list[str] = []
    gap_id = gap.get("id")
    status = gap.get("status")
    if status not in GAP_STATUSES:
        return [
            (
                f"{_rel(EVIDENCE_GAPS_PATH)}: gap {gap_id!r} has status {status!r}; expected "
                f"one of {sorted(GAP_STATUSES)}"
            )
        ]
    if status == "closed" and not str(gap.get("closed_by") or "").strip():
        problems.append(
            f"{_rel(EVIDENCE_GAPS_PATH)}: gap {gap_id!r} is closed but does not say what "
            "closed it; record the evidence in closed_by"
        )
    if status == "open" and str(gap.get("closed_by") or "").strip():
        problems.append(
            f"{_rel(EVIDENCE_GAPS_PATH)}: gap {gap_id!r} is open but carries closed_by"
        )
    residual = gap.get("residual_gap_id")
    if residual is None:
        return problems
    if residual == gap_id:
        problems.append(f"{_rel(EVIDENCE_GAPS_PATH)}: gap {gap_id!r} names itself as residual")
    elif residual not in by_id:
        problems.append(
            f"{_rel(EVIDENCE_GAPS_PATH)}: gap {gap_id!r} names residual gap {residual!r}, "
            "which is not in the register"
        )
    elif by_id[residual].get("status") != "open":
        problems.append(
            f"{_rel(EVIDENCE_GAPS_PATH)}: gap {gap_id!r} names residual gap {residual!r}, "
            "which is not open; a remainder must be tracked by an open gap"
        )
    return problems


def _verify_surface_document(
    document: Mapping[str, Any],
    spec: SurfaceSpec,
    gaps: Mapping[str, Mapping[str, Any]],
    referenced_gaps: set[str],
) -> list[str]:
    problems: list[str] = []
    location = _rel(spec.artifact)
    if document.get("owner_repo") == CORE_REPO:
        problems.append(f"{location}: owner_repo must not be {CORE_REPO}")

    problems.extend(_verify_inventory(document, spec, location))

    operations = document.get("operations", [])
    seen_ids: set[str] = set()
    covered_slices: set[str] = set()
    for index, operation in enumerate(operations):
        operation_id = operation.get("operation_id")
        where = f"{location}: operations[{index}] ({operation_id})"
        if operation_id in seen_ids:
            problems.append(f"{where}: duplicate operation_id")
        seen_ids.add(operation_id)

        slice_id = operation.get("baseline_slice")
        if slice_id not in REQUIRED_SLICE_IDS:
            problems.append(f"{where}: baseline_slice {slice_id!r} is not a Phase 0 slice")
        else:
            covered_slices.add(slice_id)

        evidence = operation.get("evidence", {})
        gap_id = evidence.get("gap_id")
        gap = gaps.get(gap_id)
        if gap is None:
            problems.append(f"{where}: evidence.gap_id {gap_id!r} is not in the gap register")
        else:
            referenced_gaps.add(gap_id)
            if not evidence.get("captured") and gap.get("status") != "open":
                problems.append(
                    f"{where}: descriptor is not captured but gap {gap_id!r} is closed; "
                    "point the operation at an open gap or record the evidence"
                )

        descriptor_problems = _verify_descriptor(operation, spec, where, required=False)
        problems.extend(descriptor_problems)
        if not descriptor_problems and operation.get("descriptor") is not None:
            problems.extend(_verify_descriptor_inventory(operation["descriptor"], spec, where))
        problems.extend(_verify_core_contracts(operation, where))

    for missing in sorted(set(spec.required_slices) - covered_slices):
        problems.append(f"{location}: required baseline slice {missing!r} has no operation")
    return problems


def _verify_inventory(
    document: Mapping[str, Any],
    spec: SurfaceSpec,
    location: str,
) -> list[str]:
    """The recorded inventory must be complete, unique, and free of exclusions."""
    problems: list[str] = []
    inventory = document.get("inventory")
    if not isinstance(inventory, Mapping):
        return [f"{location}: 'inventory' must be an object"]
    entries = inventory.get("entries")
    if not isinstance(entries, list) or not entries:
        return [f"{location}: inventory.entries must be a non-empty list"]

    seen: set[tuple[str, ...]] = set()
    for index, entry in enumerate(entries):
        where = f"{location}: inventory.entries[{index}]"
        identity = str(entry.get(spec.identity_field) or "")
        if not identity:
            problems.append(f"{where}: missing {spec.identity_field}")
            continue
        rule = excluded_by(spec.id, identity)
        if rule is not None:
            problems.append(
                f"{where}: {spec.identity_field} {identity!r} matches exclusion "
                f"{rule.kind} {rule.value!r} and must not be in the {spec.inventory_kind} "
                f"inventory: {rule.reason}"
            )
        key = _descriptor_key(entry, spec)
        if key in seen:
            problems.append(f"{where}: duplicate inventory entry {list(key)}")
        seen.add(key)

    for rule in inventory.get("exclusions", []):
        if rule.get("kind") not in EXCLUSION_KINDS:
            problems.append(
                f"{location}: exclusion {rule.get('value')!r} has kind {rule.get('kind')!r}; "
                f"expected one of {sorted(EXCLUSION_KINDS)}"
            )
        if not str(rule.get("reason") or "").strip():
            problems.append(
                f"{location}: exclusion {rule.get('value')!r} must state why it is excluded"
            )
    return problems


def _verify_descriptor(
    operation: Mapping[str, Any],
    spec: SurfaceSpec,
    where: str,
    *,
    required: bool,
) -> list[str]:
    """Enforce the anti-invention guard on surface descriptors."""
    descriptor = operation.get("descriptor")
    captured = bool(operation.get("evidence", {}).get("captured"))
    if not required and not captured:
        if descriptor is not None:
            return [
                (
                    f"{where}: descriptor is filled in but evidence.captured is false. A "
                    "descriptor may only be recorded from reviewed source or a capture "
                    "artifact, never guessed."
                )
            ]
        capture_command = operation.get("evidence", {}).get("capture_command")
        if not str(capture_command or "").strip():
            return [f"{where}: evidence.capture_command must state how to close the gap"]
        return []

    problems: list[str] = []
    if not isinstance(descriptor, Mapping):
        return [f"{where}: descriptor must be an object once evidence.captured is true"]
    missing = [
        name for name in spec.descriptor_fields if not str(descriptor.get(name) or "").strip()
    ]
    if missing:
        problems.append(f"{where}: descriptor is missing {missing}")
    extra = sorted(set(descriptor) - set(spec.descriptor_fields))
    if extra:
        problems.append(
            f"{where}: descriptor has unexpected fields {extra}; expected exactly "
            f"{list(spec.descriptor_fields)}"
        )
    return problems


def _verify_descriptor_inventory(
    descriptor: Mapping[str, Any],
    spec: SurfaceSpec,
    where: str,
) -> list[str]:
    """A recorded descriptor must be a non-excluded member of the frozen inventory."""
    identity = str(descriptor.get(spec.identity_field) or "")
    rule = excluded_by(spec.id, identity)
    if rule is not None:
        return [
            (
                f"{where}: {spec.identity_field} {identity!r} matches exclusion {rule.kind} "
                f"{rule.value!r} and is not part of the {spec.inventory_kind} inventory: "
                f"{rule.reason}"
            )
        ]
    if _descriptor_key(descriptor, spec) not in _inventory_keys(spec):
        return [
            (
                f"{where}: descriptor {_descriptor_key(descriptor, spec)} is not in the frozen "
                f"{spec.inventory_kind} inventory for {spec.owner_repo}; record it there from "
                "reviewed source before mapping a slice to it"
            )
        ]
    return []


def _inventory_keys(spec: SurfaceSpec) -> set[tuple[str, ...]]:
    return {_entry_key(entry, spec) for entry in spec.inventory}


def _entry_key(entry: InventoryEntry, spec: SurfaceSpec) -> tuple[str, ...]:
    values = {spec.identity_field: entry.identity, "method": entry.method or ""}
    return tuple(values[name] for name in spec.match_fields)


def _descriptor_key(descriptor: Mapping[str, Any], spec: SurfaceSpec) -> tuple[str, ...]:
    return tuple(str(descriptor.get(name) or "") for name in spec.match_fields)


def _verify_core_contracts(operation: Mapping[str, Any], where: str) -> list[str]:
    """Every named Core contract must actually resolve in this checkout."""
    problems: list[str] = []
    for dotted in operation.get("core_contracts", []):
        if not _resolves(dotted):
            problems.append(
                f"{where}: core contract {dotted!r} does not resolve in this checkout"
            )
    return problems


def _resolves(dotted: str) -> bool:
    module_path, _, attribute = dotted.rpartition(".")
    while module_path:
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            module_path, _, head = module_path.rpartition(".")
            attribute = f"{head}.{attribute}"
            continue
        target: Any = module
        for part in attribute.split("."):
            target = getattr(target, part, None)
            if target is None:
                return False
        return True
    return False


def _rel(path: Path) -> str:
    """Repo-relative path for messages, falling back to the absolute path."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def require_declared_slices() -> None:
    """Fail fast if a declaration references a slice outside the frozen set."""
    for surface_id, entries in DECLARED_ENTRIES.items():
        for entry in entries:
            require_known_slice(entry.baseline_slice, context=f"{surface_id}:{entry.operation_id}")
