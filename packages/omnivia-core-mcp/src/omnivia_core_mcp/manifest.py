"""The curated MCP exposure manifest (R004-06).

**An allow-list, not a projection of the catalogue.** ``OPERATION_CATALOGUE``
holds twenty operations. This module names six of them. A newly registered Core
operation is absent from MCP until somebody adds it here and tests it, which is
the whole difference between an application capability catalogue and an
agent-facing security decision: the catalogue says what Core *can* do, and this
says what a model may *ask* it to do.

**What is deliberately not here**, and stays not here: service start, stop,
health, readiness, status and discovery; bootstrap and workspace initialisation;
unrestricted filesystem path selection; administrative configuration; and every
destructive or persistent mutation. None of those is a tool a model calls.

**The six.** ``workspace.inspect`` is the attached workspace's own descriptor.
The other five are V06-3's retrieval and context-pack reads --
``evidence.search``, ``knowledge.search``, ``memory.search``, ``graph.traverse``
and ``context_pack.build`` -- which have landed, classify themselves
``side_effect="none"`` and ``audit_category="read"``, and are the operations an
agent needs to answer a question from a governed workspace. Every identifier
below is the catalogue's own; none is invented here.

**Read-first is enforced, not asserted.** :func:`_admit` refuses at import time
any entry whose catalogue metadata is not ``side_effect="none"`` and
``audit_category="read"``. A future editor who adds ``memory.create`` here does
not ship a mutation tool with a wrong comment; the package fails to import.

**Projection, not redefinition.** A tool's input and output schemas come from
the canonical Application Contract v1 documents, reached through the catalogue
entry's own ``input_schema_ref`` and ``result_schema_ref`` and looked up in
:mod:`omnivia_core_mcp.generated_schema_projection` -- which
``scripts/generate-mcp-exposure-schemas.py`` emits from those documents and
``--check`` holds to them. Nothing here transcribes a field, and nothing here
reads the packaged canonical schemas: they are force-included into the
``omnivia-core`` *wheel* and absent from an editable install, so reading them
would make `tools/list` depend on how Core was installed. The generated module
is present and identical in both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from mcp import types

from omnivia_core.contracts.v1 import OperationMetadata, get_operation_metadata
from omnivia_core_mcp.generated_schema_projection import SCHEMAS

__all__ = [
    "EXPOSURE_MANIFEST",
    "MANIFEST_VERSION",
    "ExposedOperation",
    "exposed_by_tool_name",
    "input_schema",
    "output_schema",
    "tools",
]

#: Version of this exposure manifest, independent of the distribution version.
#: Bumped when the exposed set or a projected schema changes, so a host that
#: cached a tool listing can tell that it is stale. R004-06 requires the listing
#: to be deterministic *for a given package version*; this is the narrower fact
#: that actually changed when it is not. ``1.0`` advertised ``workspace.inspect``
#: alone with no output schema; ``1.1`` is the six-operation surface below.
MANIFEST_VERSION: Final = "1.1"

#: The side effect and audit category an operation must declare to be exposable
#: at all. Read from the catalogue entry, never from an opinion held here.
_ADMITTED_SIDE_EFFECT: Final = "none"
_ADMITTED_AUDIT_CATEGORY: Final = "read"


@dataclass(frozen=True, slots=True)
class ExposedOperation:
    """One allow-listed Core operation and the MCP tool name it answers to.

    ``tool_name`` is stable MCP-facing vocabulary and is deliberately *not*
    derived from ``operation``: the operation identifier is Core's, may be
    renamed on Core's schedule, and carries a ``.`` that reads as a namespace
    separator to some hosts. Mapping them explicitly is what lets one move
    without the other.

    ``purpose`` is the claim the request states. The authorised application path
    grants a fixed allowlist of purposes and refuses anything outside it, so it
    has to be stated per operation rather than assumed -- and it is only a claim
    either way: the service decides from its own grant.

    Nothing else about the request is here. The required scopes, the capability
    identifier and its minimum version, the side effect, the audit category and
    the idempotency posture are all read off the catalogue entry at the point of
    use, so a renamed capability fails as a rename rather than as a mystery
    refusal two files away.
    """

    tool_name: str
    operation: str
    purpose: str
    title: str
    description: str


#: The allow-list. Adding a line here is the whole act of exposing an operation,
#: and it is the only one: nothing enumerates the catalogue.
EXPOSURE_MANIFEST: Final[tuple[ExposedOperation, ...]] = (
    ExposedOperation(
        tool_name="workspace_inspect",
        operation="workspace.inspect",
        purpose="workspace_inspection",
        title="Inspect the OmniVia workspace",
        description=(
            "Return the descriptor of the OmniVia Core workspace this server is "
            "attached to: its identifier, display name, status, compatibility "
            "versions and timestamps. Read-only. Takes no arguments -- the "
            "workspace is the one the attached service owns and cannot be "
            "selected by the caller."
        ),
    ),
    ExposedOperation(
        tool_name="evidence_search",
        operation="evidence.search",
        purpose="knowledge_retrieval",
        title="Search workspace evidence",
        description=(
            "Search the workspace's L0 evidence artifacts -- captured source "
            "material with exact provenance -- and return complete artifacts "
            "with their capture history. Evidence is raw material, not governed "
            "knowledge: an artifact asserts only that something was captured. "
            "Read-only."
        ),
    ),
    ExposedOperation(
        tool_name="knowledge_search",
        operation="knowledge.search",
        purpose="knowledge_retrieval",
        title="Search governed knowledge",
        description=(
            "Search accepted, sealed governed records. Returns the current "
            "canonical view unless another view is asked for explicitly, so "
            "candidate, rejected and superseded knowledge is never returned by "
            "omission. Read-only."
        ),
    ),
    ExposedOperation(
        tool_name="memory_search",
        operation="memory.search",
        purpose="knowledge_retrieval",
        title="Search governed memory",
        description=(
            "Search the workspace's governed memory records with the requested "
            "ordering and paging. Returns the current canonical view unless "
            "another view is asked for explicitly. Read-only."
        ),
    ),
    ExposedOperation(
        tool_name="graph_traverse",
        operation="graph.traverse",
        purpose="knowledge_retrieval",
        title="Traverse governed knowledge relationships",
        description=(
            "Follow sealed governed relations out from one or more starting "
            "record versions, returning the reached nodes with their shortest-path "
            "depth and the edges that reached them. Bounded by depth, node and "
            "edge limits. Read-only."
        ),
    ),
    ExposedOperation(
        tool_name="context_pack_build",
        operation="context_pack.build",
        purpose="knowledge_retrieval",
        title="Build a cited context pack",
        description=(
            "Build a deterministic, fully cited context pack for a query from "
            "the workspace's authorised evidence and governed records, within a "
            "token budget. Every selected passage carries its citation, and the "
            "pack requires fresh authorization before reuse: holding one grants "
            "nothing. Persists nothing. Read-only."
        ),
    ),
)


def _admit(exposed: ExposedOperation) -> OperationMetadata:
    """The catalogue entry for one allow-listed operation, or a refusal.

    Three ways to fail, and each is a mistake this module exists to make
    impossible rather than to document: an operation that is not in the landed
    catalogue at all, one that mutates, and one that is not audited as a read.
    """
    entry = get_operation_metadata(exposed.operation)  # raises on an unknown name
    if entry.scope.side_effect != _ADMITTED_SIDE_EFFECT:
        raise ValueError(
            f"{exposed.operation}: side_effect={entry.scope.side_effect!r}; the MCP "
            f"exposure manifest admits {_ADMITTED_SIDE_EFFECT!r} only"
        )
    if entry.audit.audit_category != _ADMITTED_AUDIT_CATEGORY:
        raise ValueError(
            f"{exposed.operation}: audit_category={entry.audit.audit_category!r}; the "
            f"MCP exposure manifest admits {_ADMITTED_AUDIT_CATEGORY!r} only"
        )
    return entry


def _projected(schema_ref: str) -> dict[str, Any]:
    """The generated self-contained schema one canonical reference names.

    The reference comes from the catalogue entry and the document comes from the
    generator, so neither is written down beside the other: an operation whose
    contract is renamed upstream fails here instead of advertising a stale shape,
    and a canonical schema that changes fails
    ``scripts/generate-mcp-exposure-schemas.py --check`` before it reaches here.
    """
    projected = SCHEMAS.get(schema_ref)
    if projected is None:
        raise ValueError(
            f"{schema_ref}: no generated schema projection. Regenerate with "
            "scripts/generate-mcp-exposure-schemas.py"
        )
    return projected


def input_schema(entry: OperationMetadata) -> dict[str, Any]:
    """The advertised input schema for one operation.

    Public because :mod:`omnivia_core_mcp.server` enforces the document this
    returns at call time. What `tools/list` advertises and what `_request` accepts
    have to be one projection, not two that agree by inspection.
    """
    return _projected(entry.input_schema_ref)


def output_schema(entry: OperationMetadata) -> dict[str, Any]:
    """The advertised output schema for one operation.

    Advertised for every tool, so a host can validate what came back -- the
    official client does exactly that, against this document, on every successful
    call. The server's `structuredContent` is the contract-encoded result, which
    is the value this schema describes.
    """
    return _projected(entry.result_schema_ref)


def _tool(exposed: ExposedOperation) -> types.Tool:
    """One MCP tool, projected from one allow-listed operation's contract."""
    entry = _admit(exposed)
    return types.Tool(
        name=exposed.tool_name,
        title=exposed.title,
        description=exposed.description,
        input_schema=input_schema(entry),
        output_schema=output_schema(entry),
        annotations=types.ToolAnnotations(
            title=exposed.title,
            # Read off the catalogue, not asserted here. `_admit` has already
            # refused anything these would have to lie about.
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=entry.idempotency.safe_to_retry,
            # One local workspace this server is already attached to.
            open_world_hint=False,
        ),
        # `_meta` is the field's wire name and the one its constructor takes; the
        # attribute is `meta`. Passing the alias is what keeps this type-checked.
        _meta={
            "omnivia.manifestVersion": MANIFEST_VERSION,
            "omnivia.operation": entry.name,
            "omnivia.inputSchemaRef": entry.input_schema_ref,
            "omnivia.resultSchemaRef": entry.result_schema_ref,
        },
    )


#: The advertised tools, built once at import in manifest order.
#:
#: Built once because R004-06 requires `tools/list` to be deterministic for a
#: given package version: one tuple, one order, no per-request construction and
#: nothing read from the environment. Built at *import* because every refusal
#: above is then a failure to start rather than a tool that misdescribes itself.
_TOOLS: Final[tuple[types.Tool, ...]] = tuple(
    _tool(exposed) for exposed in EXPOSURE_MANIFEST
)

_BY_TOOL_NAME: Final[dict[str, ExposedOperation]] = {
    exposed.tool_name: exposed for exposed in EXPOSURE_MANIFEST
}

if len(_BY_TOOL_NAME) != len(EXPOSURE_MANIFEST):  # pragma: no cover - import-time guard
    raise ValueError("the MCP exposure manifest declares a tool name twice")


def tools() -> tuple[types.Tool, ...]:
    """Every advertised tool, in manifest order, identical on every call."""
    return _TOOLS


def exposed_by_tool_name(tool_name: str) -> ExposedOperation | None:
    """The allow-listed operation one MCP tool name maps to, or ``None``.

    ``None`` is the answer for every Core operation that is not on the
    allow-list, and it is the only lookup the call path has: there is no
    fallback that resolves a tool name to an operation some other way, so an
    operation absent from this manifest is not callable rather than merely
    unadvertised.
    """
    return _BY_TOOL_NAME.get(tool_name)
