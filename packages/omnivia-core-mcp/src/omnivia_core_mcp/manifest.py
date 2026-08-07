"""The curated MCP exposure manifest (R004-06).

**An allow-list, not a projection of the catalogue.** ``OPERATION_CATALOGUE``
holds twenty operations. This module names one of them. A newly registered Core
operation is absent from MCP until somebody adds it here and tests it, which is
the whole difference between an application capability catalogue and an
agent-facing security decision: the catalogue says what Core *can* do, and this
says what a model may *ask* it to do.

**What is deliberately not here**, and stays not here: service start, stop,
health, readiness, status and discovery; bootstrap and workspace initialisation;
unrestricted filesystem path selection; administrative configuration; and every
destructive or persistent mutation. None of those is a tool a model calls.

**Why only ``workspace.inspect`` in this slice.** R004-06 also names the V06-3
retrieval and context-pack operations, and requires that their identifiers come
from the landed catalogue. ``context_pack.build``, ``evidence.search``,
``knowledge.search`` and the ``memory.*`` reads are all present and all classify
themselves ``side_effect="none"``, but "explicitly classified as safe for MCP
use" is a V06-3 classification that has not landed, and this module does not get
to make it on V06-3's behalf. Adding one is an edit to :data:`EXPOSURE_MANIFEST`
and a test, and nothing else.

**Read-first is enforced, not asserted.** :func:`_admit` refuses at import time
any entry whose catalogue metadata is not ``side_effect="none"`` and
``audit_category="read"``. A future editor who adds ``memory.create`` here does
not ship a mutation tool with a wrong comment; the package fails to import.

**Projection, not redefinition.** The MCP tool's schema comes from the operation
contract, reached through the catalogue entry's own ``input_schema_ref``: the
``#/$defs/<Name>`` fragment names the generated contract dataclass, which is
looked up in the public package rather than transcribed. See
:func:`_input_schema` for what that projection can and cannot express today, and
:mod:`omnivia_core_mcp.server` for why no output schema is projected.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Final

from mcp import types

import omnivia_core.contracts.v1 as contracts_v1
from omnivia_core.contracts.v1 import OperationMetadata, get_operation_metadata

__all__ = [
    "EXPOSURE_MANIFEST",
    "MANIFEST_VERSION",
    "ExposedOperation",
    "exposed_by_tool_name",
    "tools",
]

#: Version of this exposure manifest, independent of the distribution version.
#: Bumped when the exposed set or a projected schema changes, so a host that
#: cached a tool listing can tell that it is stale. R004-06 requires the listing
#: to be deterministic *for a given package version*; this is the narrower fact
#: that actually changed when it is not.
MANIFEST_VERSION: Final = "1.0"

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


def _contract_type(schema_ref: str) -> type[Any]:
    """The generated contract dataclass one ``$defs`` reference names.

    The catalogue's ``input_schema_ref`` ends ``#/$defs/WorkspaceInspectInput``,
    and the generated package exports a dataclass of exactly that name -- the
    generator makes both from one source. So the type is *reached* from the
    contract rather than written down beside it, and an operation whose payload
    type is renamed fails here instead of projecting a stale shape.
    """
    name = schema_ref.rsplit("/", 1)[-1]
    contract_type = getattr(contracts_v1, name, None)
    # `isinstance(_, type)` as well as `is_dataclass`, because the latter is true
    # of a dataclass *instance* too and this must be the class itself.
    if not isinstance(contract_type, type) or not dataclasses.is_dataclass(
        contract_type
    ):
        raise ValueError(
            f"{schema_ref}: the public contract exports no dataclass named {name!r}"
        )
    return contract_type


def _input_schema(entry: OperationMetadata) -> dict[str, Any]:
    """Project one operation's input contract as a JSON Schema object.

    **This projects the empty payload and refuses everything else, on purpose.**
    The packaged JSON Schema documents (``read_schema``) are force-included into
    the ``omnivia-core`` *wheel* and are absent from the editable install this
    repository develops and gates against, so reading them would make
    ``tools/list`` depend on how Core was installed -- and R004-06 requires it to
    be deterministic. The remaining always-present projection source is the
    generated dataclass, and a faithful recursive dataclass-to-JSON-Schema
    projector is a second opinion about the shape of a contract, which is exactly
    what R004-06's "projected rather than manually redefined" rules out.

    So: a contract input with no fields projects to the empty object, exactly and
    provably, and an input with fields raises rather than guessing. Exposing an
    operation that takes arguments is therefore blocked on giving this package a
    real projector -- which is the right place for that to be blocked.
    """
    fields = dataclasses.fields(_contract_type(entry.input_schema_ref))
    if fields:
        raise ValueError(
            f"{entry.name}: {entry.input_schema_ref} declares "
            f"{[field.name for field in fields]}; this package can project the empty "
            "input payload only, and does not guess at a schema it cannot derive"
        )
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def _tool(exposed: ExposedOperation) -> types.Tool:
    """One MCP tool, projected from one allow-listed operation's contract."""
    entry = _admit(exposed)
    return types.Tool(
        name=exposed.tool_name,
        title=exposed.title,
        description=exposed.description,
        input_schema=_input_schema(entry),
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
