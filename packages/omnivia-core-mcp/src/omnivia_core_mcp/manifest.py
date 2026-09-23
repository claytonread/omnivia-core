"""The curated MCP exposure manifest (R004-06), in two fixed profiles.

**An allow-list, not a projection of the catalogue.** ``OPERATION_CATALOGUE``
holds twenty-eight operations. This module names six of them in the
``restricted`` profile and eleven in the ``authoring`` profile. A newly
registered Core operation is absent from MCP until somebody adds it here and
tests it, which is the whole difference between an application capability
catalogue and an agent-facing security decision: the catalogue says what Core
*can* do, and this says what a model may *ask* it to do.

**What is deliberately not here**, and stays not here: service start, stop,
health, readiness, status and discovery; bootstrap, workspace creation,
selection and enumeration; grant administration; governance decisions;
``job.cancel`` and ``job.retry``; chat, workflow and connector mutation;
unrestricted filesystem path selection; and administrative configuration. None
of those is a tool a model calls.

**The restricted six.** ``workspace.inspect`` is the attached workspace's own
descriptor. The other five are V06-3's retrieval and context-pack reads --
``evidence.search``, ``knowledge.search``, ``memory.search``, ``graph.traverse``
and ``context_pack.build`` -- which classify themselves ``side_effect="none"``
and ``audit_category="read"``, and are the operations an agent needs to answer a
question from a governed workspace. Every identifier below is the catalogue's
own; none is invented here.

**The authoring eleven** are those six plus exactly three mutations --
``memory.create``, ``evidence.capture`` and ``import.start`` -- and the two job
observations, ``job.get`` and ``job.events``, that make an asynchronous import
followable. The three mutations are the *only* side-effecting operations this
module can admit, and they are named as a literal set rather than inferred from
any catalogue property: a fourth mutation cannot arrive by a contract gaining a
field or an operation changing its audit category.

**Which profile a server advertises is decided once, at startup, by
:mod:`omnivia_core_mcp.configuration`** -- never by a prompt or by a tool call's
arguments. Every function here takes the profile as an argument and defaults it
to ``restricted``, so a caller that has not been taught about profiles gets the
read-only surface rather than the wider one.

**Read-first is enforced, not asserted.** :func:`_admit` refuses at import time
any entry that is neither a catalogue read (``side_effect="none"`` *and*
``audit_category="read"``) nor one of the three named mutations. A future editor
who adds ``record.supersede`` here does not ship a destructive tool with a wrong
comment; the package fails to import.

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

A mutation tool's advertised input is the one shape this module *composes*
rather than projects: the closed outer object ``{"input": ..., "idempotency_key":
...}``. Both halves are still generated -- ``input`` is the operation's own
canonical input document and the key is the canonical
``common.schema.json#/$defs/IdempotencyKey`` -- so the wrapper adds a shape and
transcribes no constraint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from mcp import types

from omnivia_core.contracts.v1 import OperationMetadata, get_operation_metadata
from omnivia_core_mcp.generated_schema_projection import SCHEMAS

__all__ = [
    "ADMITTED_MUTATIONS",
    "AUTHORING_MANIFEST",
    "AUTHORING_PROFILE",
    "EXPOSURE_MANIFEST",
    "IDEMPOTENCY_KEY_SCHEMA_REF",
    "MANIFEST_VERSION",
    "PROFILES",
    "RESTRICTED_MANIFEST",
    "RESTRICTED_PROFILE",
    "ExposedOperation",
    "exposed_by_tool_name",
    "exposure_manifest",
    "input_schema",
    "output_schema",
    "tools",
]

#: Version of this exposure manifest, independent of the distribution version.
#: Bumped when the exposed set or a projected schema changes, so a host that
#: cached a tool listing can tell that it is stale. R004-06 requires the listing
#: to be deterministic *for a given package version*; this is the narrower fact
#: that actually changed when it is not. ``1.0`` advertised ``workspace.inspect``
#: alone with no output schema; ``1.1`` was the six-operation read surface;
#: ``2.0`` is the major bump that adds a second, wider profile and the mutation
#: wrapper -- a host that cached an ``1.1`` listing has cached the whole surface.
MANIFEST_VERSION: Final = "2.0"

#: The two profiles, named exactly as the configuration document names them. A
#: profile selects a whole fixed inventory; it never filters one.
RESTRICTED_PROFILE: Final = "restricted"
AUTHORING_PROFILE: Final = "authoring"
PROFILES: Final[tuple[str, ...]] = (RESTRICTED_PROFILE, AUTHORING_PROFILE)

#: The side effect and audit category a *read* must declare to be exposable.
#: Read from the catalogue entry, never from an opinion held here.
_ADMITTED_SIDE_EFFECT: Final = "none"
_ADMITTED_AUDIT_CATEGORY: Final = "read"

#: The only side-effecting operations this manifest may admit, as a literal set.
#:
#: A set of names rather than a rule over catalogue metadata, because a rule
#: would admit the next operation that happened to satisfy it. Widening the
#: mutation surface therefore means editing this line, which is the point: there
#: are twelve other mutations in the catalogue and none of them is reachable by
#: an agent through any profile this module defines.
ADMITTED_MUTATIONS: Final[frozenset[str]] = frozenset(
    {
        "memory.create",
        "evidence.capture",
        "import.start",
        "decision.evaluate",
    }
)

#: The canonical constraint an MCP mutation wrapper's ``idempotency_key`` carries.
#:
#: The request envelope's own key definition, reached by reference and projected
#: by the generator like every other advertised schema. Named here rather than
#: transcribed so the advertised pattern, length bounds and description are the
#: envelope's and stay the envelope's.
IDEMPOTENCY_KEY_SCHEMA_REF: Final = (
    "https://contracts.omnivia.dev/application/v1/common.schema.json"
    "#/$defs/IdempotencyKey"
)

_JSON_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"


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


#: The read-only allow-list, and the surface every profile starts from. Adding a
#: line here is the whole act of exposing an operation, and it is the only one:
#: nothing enumerates the catalogue.
RESTRICTED_MANIFEST: Final[tuple[ExposedOperation, ...]] = (
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

    ExposedOperation(
        tool_name="decision_evaluate",
        operation="decision.evaluate",
        purpose="decision_evaluation",
        title="Submit a bounded decision evaluation",
        description=(
            "Submit one bounded advisory assessment over authorised workspace "
            "sources and return a durable evaluation record with its typed "
            "prediction, quality and disposition. This operation has durable "
            "side effects: it consumes resources and creates evaluation, job "
            "and audit records, though it never mutates business records or "
            "executes actions. Processing is advisory; a prediction never "
            "authorises an action."
        ),
    ),
    ExposedOperation(
        tool_name="decision_record_get",
        operation="decision.record.get",
        purpose="decision_record",
        title="Inspect one decision evaluation record",
        description=(
            "Return the durable record of one decision evaluation: its typed "
            "prediction, quality, disposition and execution facts. Read-only."
        ),
    ),
    ExposedOperation(
        tool_name="decision_record_list",
        operation="decision.record.list",
        purpose="decision_record",
        title="List decision evaluation records",
        description=(
            "List the authorised decision evaluation records for the selected "
            "workspace, newest first. Read-only and paginated."
        ),
    ),
    ExposedOperation(
        tool_name="decision_status",
        operation="decision.status",
        purpose="decision_read",
        title="Report Decision Runtime status",
        description=(
            "Report whether the local decision engine is available on the "
            "selected Core host, whether processing is enabled, and the "
            "installed profile count. Passive: this never downloads, warms, "
            "starts Core or processes records. Read-only."
        ),
    ),
)

#: What the `authoring` profile adds, and all it adds: three mutations and the
#: two observations that make an asynchronous one followable.
#:
#: The purposes are the service's own -- `memory_authoring` for memory,
#: `content_ingestion` for both ways content enters a workspace, and
#: `job_observation` for watching what that produced. A purpose invented here
#: would be refused at the first call rather than caught by review.
_AUTHORING_ADDITIONS: Final[tuple[ExposedOperation, ...]] = (
    ExposedOperation(
        tool_name="memory_create",
        operation="memory.create",
        purpose="memory_authoring",
        title="Create a governed memory record",
        description=(
            "Record one new governed memory in the workspace, with its assertion "
            "and the sources that support it. Writes. The call takes an outer "
            "object with the operation input under `input` and a caller-chosen "
            "`idempotency_key`; replaying the same key with the same input "
            "answers from the settled outcome instead of writing twice."
        ),
    ),
    ExposedOperation(
        tool_name="evidence_capture",
        operation="evidence.capture",
        purpose="content_ingestion",
        title="Capture one evidence artifact",
        description=(
            "Capture one submitted document as an L0 evidence artifact with its "
            "provenance, so it can be searched and cited. The content is carried "
            "in the call: no filesystem path, URL or credential is accepted. "
            "Writes. Takes an outer object with the operation input under "
            "`input` and a caller-chosen `idempotency_key`."
        ),
    ),
    ExposedOperation(
        tool_name="import_start",
        operation="import.start",
        purpose="content_ingestion",
        title="Start an import",
        description=(
            "Start an import of submitted content into the workspace. Always "
            "answers with a job rather than the finished result: follow it with "
            "`job_get` and `job_events`. Writes. Takes an outer object with the "
            "operation input under `input` and a caller-chosen `idempotency_key`."
        ),
    ),
    ExposedOperation(
        tool_name="job_get",
        operation="job.get",
        purpose="job_observation",
        title="Get a job's current state",
        description=(
            "Return the current state of one job the workspace is running or has "
            "run, by its identifier. Read-only, and an observation rather than a "
            "subscription: call it again to see a later state."
        ),
    ),
    ExposedOperation(
        tool_name="job_events",
        operation="job.events",
        purpose="job_observation",
        title="Read a job's events",
        description=(
            "Read one page of a job's ordered event history, oldest first, "
            "continuing from the page metadata the previous response returned. "
            "Snapshot-stable and bounded by the catalogue's page maximum. "
            "Read-only, and not a transport stream."
        ),
    ),
)

#: The `authoring` profile: the restricted surface, in its order, then the five.
#: Concatenated rather than restated so the two profiles cannot drift in the
#: operations they share.
AUTHORING_MANIFEST: Final[tuple[ExposedOperation, ...]] = (
    RESTRICTED_MANIFEST + _AUTHORING_ADDITIONS
)

#: The safe default, and what every caller that names no profile gets.
#:
#: Kept under its original name because it is what `omnivia_core_mcp.server` and
#: the operation-traceability ledger already reach for: a caller written before
#: profiles existed advertises the read-only six, which is the failure mode this
#: name should have.
EXPOSURE_MANIFEST: Final[tuple[ExposedOperation, ...]] = RESTRICTED_MANIFEST

_MANIFESTS: Final[dict[str, tuple[ExposedOperation, ...]]] = {
    RESTRICTED_PROFILE: RESTRICTED_MANIFEST,
    AUTHORING_PROFILE: AUTHORING_MANIFEST,
}


def _admit(exposed: ExposedOperation) -> OperationMetadata:
    """The catalogue entry for one allow-listed operation, or a refusal.

    An operation is admissible on exactly two grounds: the catalogue calls it a
    read -- ``side_effect="none"`` *and* ``audit_category="read"``, both, so an
    operation that mutates under a read's audit category or audits as a mutation
    while claiming no side effect is refused either way -- or it is one of the
    three mutations :data:`ADMITTED_MUTATIONS` names.

    Each refusal is a mistake this module exists to make impossible rather than
    to document: an operation that is not in the landed catalogue at all, and a
    side-effecting operation nobody reviewed.
    """
    entry = get_operation_metadata(exposed.operation)  # raises on an unknown name
    read = (
        entry.scope.side_effect == _ADMITTED_SIDE_EFFECT
        and entry.audit.audit_category == _ADMITTED_AUDIT_CATEGORY
    )
    if not read and exposed.operation not in ADMITTED_MUTATIONS:
        raise ValueError(
            f"{exposed.operation}: side_effect={entry.scope.side_effect!r} "
            f"audit_category={entry.audit.audit_category!r}; the MCP exposure "
            f"manifest admits catalogue reads and "
            f"{sorted(ADMITTED_MUTATIONS)} only"
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


def _mutation_input_schema(entry: OperationMetadata) -> dict[str, Any]:
    """The closed ``{input, idempotency_key}`` wrapper one mutation advertises.

    Composed from two generated documents and nothing else. The operation's own
    input goes under ``input`` verbatim, minus two keys that may not survive the
    move: its ``$schema``, because a dialect declaration is a resource-root fact
    and this document is no longer a root, and its ``$defs``, which is hoisted to
    the wrapper's root so the ``#/$defs/...`` references inside it keep resolving
    -- a local reference is resolved against the document root, not against the
    subschema it appears in, so leaving the closure nested would silently
    unresolve every one of them. The wrapper declares no ``$defs`` of its own and
    the key schema holds no references, so the hoist cannot collide.

    ``additionalProperties: false`` and both properties required, because the
    outer shape is exactly two fields: an unrecognised outer key is a caller
    trying to say something this seam does not accept -- an authority field, a
    workspace, a purpose -- and is refused rather than ignored.
    """
    inner = dict(_projected(entry.input_schema_ref))
    inner.pop("$schema", None)
    definitions = inner.pop("$defs", None)
    key = dict(_projected(IDEMPOTENCY_KEY_SCHEMA_REF))
    key.pop("$schema", None)

    wrapper: dict[str, Any] = {
        "$schema": _JSON_SCHEMA_DIALECT,
        "type": "object",
        "description": (
            f"MCP call wrapper for `{entry.name}`: the canonical operation input "
            "under `input`, and the caller-chosen idempotency key that makes a "
            "repeated submission answer from the settled outcome. No other "
            "property is accepted."
        ),
        "properties": {"input": inner, "idempotency_key": key},
        "required": ["input", "idempotency_key"],
        "additionalProperties": False,
    }
    if definitions is not None:
        wrapper["$defs"] = definitions
    return wrapper


def input_schema(entry: OperationMetadata) -> dict[str, Any]:
    """The advertised input schema for one operation.

    A read advertises its canonical operation input directly. A mutation
    advertises the closed wrapper, because the idempotency key belongs in the
    request envelope rather than in the operation input and there is nowhere else
    for a caller to put it.

    Public because :mod:`omnivia_core_mcp.server` enforces the document this
    returns at call time. What `tools/list` advertises and what `_request` accepts
    have to be one projection, not two that agree by inspection.
    """
    if entry.name in ADMITTED_MUTATIONS:
        return _mutation_input_schema(entry)
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
            # Read off the catalogue, not asserted here: a tool is read-only
            # exactly when its operation declares no side effect, which is the
            # same fact `_admit` checked rather than a second opinion about it.
            read_only_hint=entry.scope.side_effect == _ADMITTED_SIDE_EFFECT,
            # None of the eleven deletes or overwrites: the three mutations
            # create, and supersession and cancellation are not exposed at all.
            destructive_hint=False,
            # Only where the catalogue proves it. The three mutations declare
            # `safe_to_retry=False` -- a repeat is settled by the idempotency
            # key, which is not the same claim as an idempotent call -- so this
            # is false for them and true for the reads, without a line here
            # deciding either.
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


#: The advertised tools per profile, built once at import in manifest order.
#:
#: Built once because R004-06 requires `tools/list` to be deterministic for a
#: given package version: one tuple per profile, one order, no per-request
#: construction and nothing read from the environment. Built at *import* because
#: every refusal above is then a failure to start rather than a tool that
#: misdescribes itself -- and both profiles are built whichever one is selected,
#: so a broken authoring binding cannot hide behind a restricted install.
_TOOLS: Final[dict[str, tuple[types.Tool, ...]]] = {
    profile: tuple(_tool(exposed) for exposed in entries)
    for profile, entries in _MANIFESTS.items()
}

_BY_TOOL_NAME: Final[dict[str, dict[str, ExposedOperation]]] = {
    profile: {exposed.tool_name: exposed for exposed in entries}
    for profile, entries in _MANIFESTS.items()
}

for _profile, _index in _BY_TOOL_NAME.items():  # pragma: no cover - import-time guard
    if len(_index) != len(_MANIFESTS[_profile]):
        raise ValueError(f"the {_profile!r} MCP exposure manifest names a tool twice")


def _profiled(profile: str) -> str:
    """`profile`, or a refusal naming the two that exist.

    A refusal rather than a fallback to ``restricted``: an unrecognised profile
    is a configuration this module cannot serve, and quietly serving the narrow
    surface instead would hide it until somebody wondered why a tool was missing.
    """
    if profile not in _MANIFESTS:
        raise ValueError(
            f"{profile!r} is not an MCP exposure profile; the profiles are "
            f"{list(PROFILES)}"
        )
    return profile


def exposure_manifest(
    profile: str = RESTRICTED_PROFILE,
) -> tuple[ExposedOperation, ...]:
    """The allow-list one profile exposes, in its order."""
    return _MANIFESTS[_profiled(profile)]


def tools(profile: str = RESTRICTED_PROFILE) -> tuple[types.Tool, ...]:
    """Every tool one profile advertises, in manifest order, identical on every
    call. Defaults to ``restricted``: a caller that names no profile gets the
    read-only surface."""
    return _TOOLS[_profiled(profile)]


def exposed_by_tool_name(
    tool_name: str, profile: str = RESTRICTED_PROFILE
) -> ExposedOperation | None:
    """The allow-listed operation one MCP tool name maps to, or ``None``.

    ``None`` is the answer for every Core operation that this profile does not
    expose, and it is the only lookup the call path has: there is no fallback
    that resolves a tool name to an operation some other way, so an operation
    absent from the running profile's manifest is not callable rather than merely
    unadvertised. A restricted server therefore refuses `memory_create` at the
    call as well as omitting it from the listing.
    """
    return _BY_TOOL_NAME[_profiled(profile)].get(tool_name)
