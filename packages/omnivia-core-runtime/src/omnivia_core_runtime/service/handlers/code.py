"""The `code.query` handler -- static code facts from one applicable analysis import.

A read beside `graph.traverse`, served by the same local-owner read family under the same
`knowledge_retrieval` purpose and the same `graph:read` scope. It reads one value from
`storage/code_query.py` and answers six closed intents over it:

* `definition` -- the subject itself;
* `references` -- inbound `code.references`, one step;
* `callers` / `callees` -- inbound / outbound `code.calls`, to the requested depth;
* `dependencies` -- outbound `code.imports` and `code.references`, to the requested depth,
  with `runtime_only` dropping the type-only ones from both the answer and the walk;
* `code_context` (W13-B) -- the subject's neighbourhood in both directions over all three
  relation types, plus the captured source of everything it discloses and a separately
  labelled architecture component.

**The binding is checked, not trusted.** The request names a repository, snapshot,
producer and profile; the storage read refuses unless the import 0074 holds applicable for
that scope at this instant analysed exactly that snapshot. A missing or no-longer-applicable
analysis is `projection_unavailable` with `details.readiness = "not_ready"` -- the same code
the engineering reads use for a projection that is not there -- never an empty success.

**A seed carries its own binding, and it is compared.** A `code_context` subject that came
out of a lexical or semantic search arrives with the repository, snapshot, producer and
profile it was selected against. If that is not the analysis applicable now, the answer
says `graph_expansion = "unavailable"` with a reason and discloses nothing: an old seed is
never walked against a newer graph, and the unavailability is always stated rather than
left to look like an empty neighbourhood.

**Visibility is enforced before the walk and again at output.** Every fact is a governed
version, and 0009 links every version to the evidence it cites: the snapshot's manifest and
the captured file the fact was read from. The effective caller's `EvidenceLabelGrant` is
evaluated against those labels *before* the subject is resolved, so a definition the caller
may not see is `not_found` exactly as a definition that does not exist, and an edge touching
one is not an edge, not a symbol, not a depth and not a count. A diagnostic is no governed
version, so it is held to the same two artifacts its own source binding names, and to the
visibility of every definition it names. After the answer is assembled the labels are read
again: a revocation that landed while it was being built makes it `not_found` as a whole,
because an answer rebuilt around the gap, or one that said how much it dropped, would
disclose the gap's shape.

**Freshness compares captures, never the working tree.** `freshness.stale` is true when the
analysed snapshot is no longer the newest admitted capture of its own stream -- a capture the
caller may see was recorded after it. The answer is still the analysed snapshot's, and says
so; Core never observes the live checkout between captures.

**Static, and said so.** Every relation is an accepted `syntactic`/`resolved_static`
edge of that one import. Candidates the import refused (unresolved, dynamic, external, out
of policy) are returned as diagnostics when they bear on the subject and are never walked.
Inbound intents always state `unattributed_inbound`, because an unresolved occurrence
elsewhere cannot be attributed to the subject by static analysis; `complete` is true only
when nothing bounded the answer. Nothing here is runtime behaviour or test coverage: a
reference an accepted fact reads out of a test file is a static reference like any other.

**Architecture knowledge stays architecture knowledge.** The `code_context` component is a
Context Pack built by `handlers/context_pack.build_task_context` -- the workspace's one
context authority, not a second one -- over governed knowledge that is independently
accepted, carries accepted `reconciliation.supports` and is visible to this caller. It is
returned under its own key, never merged into `symbols` or `relations`, and the code domain
is excluded from it: no prose claim becomes a code edge and no code fact becomes prose.

**Ambiguity is returned, not resolved.** A `name` shared by several definitions answers
`resolution = "ambiguous"` with every qualified choice in key order and walks nothing.

The workspace and principal are the authorized context's; the payload carries neither.
Refusal messages are frozen constants and the decode failure is raised after its handler
ends, so no caller value reaches `__context__`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    CODE_CONTEXT_MAX_SPAN_BYTES,
    CODE_QUERY_DEFAULT_DEPTH,
    CODE_QUERY_DEFAULT_LIMIT,
    CODE_QUERY_INTENT_CALLEES,
    CODE_QUERY_INTENT_CALLERS,
    CODE_QUERY_INTENT_CODE_CONTEXT,
    CODE_QUERY_INTENT_DEPENDENCIES,
    CODE_QUERY_INTENT_REFERENCES,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_PROJECTION_UNAVAILABLE,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    CodeAnalysisBinding,
    CodeArchitectureContext,
    CodeContext,
    CodeDiagnostic,
    CodeQueryCoverage,
    CodeQueryInput,
    CodeQueryResult,
    CodeRelation,
    CodeSourceSpan,
    CodeSpanContent,
    CodeSymbol,
    ContractDecodeError,
    ContractSemanticError,
    GovernedRecord,
    ProjectionFreshness,
    RecordVersionReference,
    validate_code_query_input,
)
from omnivia_core_runtime.service.handlers.context_pack import (
    CONTEXT_PACK_POLICY_VERSIONS,
    build_task_context,
)
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.code_analysis_import import (
    DOMAIN_SCOPE,
    RELATIONSHIPS,
    file_evidence_id,
)
from omnivia_core_runtime.storage.code_query import (
    NOT_READY_NO_ACTIVE_IMPORT,
    CapturedSource,
    CodeAnalysisNotReady,
    CodeEdge,
    CodeFact,
    CodeGraph,
    newer_captures,
    read_active_scope,
    read_code_graph,
)
from omnivia_core_runtime.storage.governed import GovernedRecordValue
from omnivia_core_runtime.storage.graph import read_graph_snapshot
from omnivia_core_runtime.storage.repository import (
    read_evidence_labels,
    read_governed_record_labels,
)
from omnivia_core_runtime.storage.retrieval import (
    EvidenceLabelGrant,
    local_owner_label_grant,
)

#: The logical projection this read reports its freshness against, and its shape version.
CODE_PROJECTION: Final = "code.analysis"
CODE_PROJECTION_VERSION: Final = "1"

#: The operation whose grant a caller must independently hold before this read will
#: compose a task-context component for it. `code.query` is authorised as a graph read;
#: the architecture component is the Context Pack authority's material, and nothing here
#: widens one grant into the other. The name is taken from the session's own
#: `granted_operations`, which is what `authorize_application_request` already decided --
#: never from a payload field and never from the request's declared scopes.
CONTEXT_PACK_BUILD_OPERATION: Final = "context_pack.build"

#: The retrieval `code_context` performs, named by the module that performs it, exactly as
#: `CONTEXT_PACK_RETRIEVAL_VERSION` is named by `handlers/context_pack.py`. It is a
#: different retrieval and so it is a different constant: this one is the applicable
#: code-analysis import read at the query instant, plus the authoritative six-read snapshot
#: the context authority takes at that same instant, plus the governed evidence-label and
#: accepted-support admission below.
CODE_CONTEXT_RETRIEVAL_VERSION: Final = (
    "code-query.retrieval.applicable-analysis-and-authoritative-snapshot.v1"
)

#: The policies in force over one architecture component, and their versions. The context
#: authority's own two, plus the two this handler adds -- a pack records what it was built
#: under, so a stage applied here and not named here would be the same drift as a name
#: here with no stage behind it.
CODE_CONTEXT_POLICY_VERSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        **CONTEXT_PACK_POLICY_VERSIONS,
        "governed_evidence_label_acl": "code-context.governed-record-labels.v1",
        "architecture_support": "code-context.accepted-support.v1",
    }
)

#: The governed relation that makes an architecture assertion supported. A claim nothing
#: visibly supports is not disclosed as architecture knowledge at all; similarity, prose
#: and proximity are none of them support, and 0009 has no other relation that is.
SUPPORTS_RELATION: Final = "reconciliation.supports"

#: Intent -> (relation types walked, inbound, outbound).
_WALKS: Final[Mapping[str, tuple[frozenset[str], bool, bool]]] = {
    CODE_QUERY_INTENT_REFERENCES: (frozenset({"code.references"}), True, False),
    CODE_QUERY_INTENT_CALLERS: (frozenset({"code.calls"}), True, False),
    CODE_QUERY_INTENT_CALLEES: (frozenset({"code.calls"}), False, True),
    CODE_QUERY_INTENT_DEPENDENCIES: (
        frozenset({"code.imports", "code.references"}),
        False,
        True,
    ),
    CODE_QUERY_INTENT_CODE_CONTEXT: (
        frozenset({"code.calls", "code.imports", "code.references"}),
        True,
        True,
    ),
}
#: Intents whose answer may be missing an inbound edge static analysis could not attribute.
_INBOUND_UNATTRIBUTED: Final = frozenset(
    {
        CODE_QUERY_INTENT_REFERENCES,
        CODE_QUERY_INTENT_CALLERS,
        CODE_QUERY_INTENT_CODE_CONTEXT,
    }
)

#: `coverage.graph_expansion`, and the one reason this read has for refusing to expand.
EXPANSION_APPLIED: Final = "applied"
EXPANSION_NOT_REQUESTED: Final = "not_requested"
EXPANSION_UNAVAILABLE: Final = "unavailable"
EXPANSION_SEED_MISMATCH: Final = "seed_binding_mismatch"

#: `context.architecture.availability`, and why a component was not produced.
ARCHITECTURE_AVAILABLE: Final = "available"
ARCHITECTURE_UNAVAILABLE: Final = "unavailable"
ARCHITECTURE_NOT_AUTHORISED: Final = "not_authorised"
ARCHITECTURE_NO_SUBJECT: Final = "subject_not_resolved"
#: Error code -> the reason an unavailable component reports for it. Every other code the
#: context authority can raise is a fault of this build rather than a state a caller can
#: read anything from, and is re-raised whole.
_ARCHITECTURE_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "projection_unavailable": "projection_unavailable",
        "stale_projection": "stale_projection",
        "size_limit_exceeded": "frontier_too_large",
        "token_limit_exceeded": "token_budget_exhausted",
    }
)

#: Resolutions this read can reach. The first three are W13-A's; the fourth is a seed that
#: named an analysis other than the applicable one.
RESOLUTION_AMBIGUOUS: Final = "ambiguous"
RESOLUTION_SEED_NOT_APPLICABLE: Final = "seed_not_applicable"

_MESSAGE_INVALID_INPUT: Final = "the request payload is not a valid code query"
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"
_MESSAGE_NO_EFFECTIVE_AUTHORITY: Final = (
    "this read cannot compose a task context without the effective authority, scopes and "
    "purpose the authorization seam produced"
)
_MESSAGE_NOT_READY: Final = (
    "no code analysis for that repository, snapshot, producer and profile is applicable"
)
_MESSAGE_NOT_FOUND: Final = "no definition in that analysis matches the requested subject"


def code_query(context: OperationContext) -> Mapping[str, Any]:
    """Answer one `code.query` from one applicable import."""
    request = _input(context)
    connection = getattr(getattr(context, "service", None), "connection", None)
    if connection is None:
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
    # One instant, floored to the millisecond a `Timestamp` can spell, for the same
    # reason `context_pack.build` floors its own: the component composed below attests
    # this instant, and an unfloored one would filter at a moment it could not state.
    instant_us = time.time_ns() // 1_000_000 * 1000
    grant = local_owner_label_grant(
        principal_id=context.principal,
        workspace_id=context.workspace_id,
        granted_workspace=context.workspace_id,
    )
    evidence_labels = read_evidence_labels(connection, workspace_id=context.workspace_id)
    # An analysis whose snapshot manifest this caller may not see is answered exactly as one
    # that does not exist: every one of its facts cites that manifest, so the caller could see
    # none of them, and a `not_found` here would disclose that there was something to find.
    active = read_active_scope(
        connection,
        workspace_id=context.workspace_id,
        repository_id=request.repository_id,
        producer_id=request.producer_id,
        profile_digest=request.profile_digest,
        instant_us=instant_us,
    )
    not_ready: str | None = None
    if active is not None and not grant.permits(
        evidence_labels.get(active[0].manifest_evidence_id, ())
    ):
        not_ready = NOT_READY_NO_ACTIVE_IMPORT
    else:
        try:
            whole = read_code_graph(
                connection,
                workspace_id=context.workspace_id,
                repository_id=request.repository_id,
                snapshot_id=request.snapshot_id,
                producer_id=request.producer_id,
                profile_digest=request.profile_digest,
                instant_us=instant_us,
            )
        except CodeAnalysisNotReady as refused:
            not_ready = refused.reason
    if not_ready is not None:
        raise OperationError(
            ERROR_CODE_PROJECTION_UNAVAILABLE,
            _MESSAGE_NOT_READY,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            details={"readiness": "not_ready", "reason": not_ready},
        )

    # The ACL, before the subject is resolved and therefore before anything is walked.
    labels = read_governed_record_labels(connection, workspace_id=context.workspace_id)
    graph = _visible(whole, grant, labels, evidence_labels)

    limit = CODE_QUERY_DEFAULT_LIMIT if request.limit is None else int(request.limit)
    contextual = request.intent == CODE_QUERY_INTENT_CODE_CONTEXT
    expansion = EXPANSION_NOT_REQUESTED
    expansion_reason: str | None = None
    resolution: str
    subjects: tuple[str, ...]
    if request.seed is not None and not _seed_applies(request.seed, graph):
        resolution, subjects = RESOLUTION_SEED_NOT_APPLICABLE, ()
        expansion, expansion_reason = EXPANSION_UNAVAILABLE, EXPANSION_SEED_MISMATCH
    else:
        resolution, subjects = _subjects(graph, request)
    walk = _WALKS.get(request.intent)
    depth = 0
    symbols: list[CodeSymbol] = []
    relations: list[CodeRelation] = []
    diagnostics: list[CodeDiagnostic] = []
    depth_bounded = False
    if subjects and resolution != RESOLUTION_AMBIGUOUS and walk is not None:
        expansion = EXPANSION_APPLIED
        types, inbound, outbound = walk
        depth = (
            1
            if request.intent == CODE_QUERY_INTENT_REFERENCES
            else int(request.depth or CODE_QUERY_DEFAULT_DEPTH)
        )
        runtime_only = bool(request.runtime_only)
        edges = [
            edge
            for edge in graph.edges
            if edge.relation_type in types
            and not (runtime_only and edge.fact.content["type_only"])
        ]
        depths, followed, depth_bounded = _walk(
            edges, subjects, inbound=inbound, outbound=outbound, depth=depth
        )
        expanded = {key for key, at in depths.items() if at < depth}
        symbols = [
            _symbol(key, graph.symbols[key], depth=at)
            for key, at in sorted(depths.items(), key=lambda item: (item[1], item[0]))
            if at > 0
        ]
        relations = [_relation(edge, at) for at, edge in followed]
        relations.sort(
            key=lambda r: (r.depth, r.source_key, r.relation_type, r.target_key, r.edge_key)
        )
        diagnostics = _diagnostics(
            graph, types, expanded, inbound=inbound, outbound=outbound, runtime_only=runtime_only
        )
    definitions = [_symbol(key, graph.symbols[key]) for key in subjects]

    truncated = any(len(items) > limit for items in (definitions, symbols, relations, diagnostics))
    definitions, symbols = definitions[:limit], symbols[:limit]
    assembled = None
    if contextual and expansion != EXPANSION_APPLIED:
        # Choices, or a seed this graph cannot expand. Either way there is no one subject
        # whose captured source and task context this answer could be: assembling them
        # around an unresolved subject is exactly the arbitrary context a caller asked a
        # disambiguating question to avoid. `resolution` and `graph_expansion` already
        # say which of the two happened, so the reason here does not repeat them.
        assembled = CodeContext(
            spans=(),
            architecture=CodeArchitectureContext(
                availability=ARCHITECTURE_UNAVAILABLE, reason=ARCHITECTURE_NO_SUBJECT
            ),
        )
    elif contextual:
        assembled = CodeContext(
            spans=tuple(_spans(context, graph, (*definitions, *symbols))),
            architecture=_architecture(
                context,
                connection,
                request,
                grant=grant,
                labels=labels,
                instant_us=instant_us,
            ),
        )
    unattributed = (
        request.intent in _INBOUND_UNATTRIBUTED and expansion == EXPANSION_APPLIED
    )
    result = CodeQueryResult(
        intent=request.intent,
        binding=_binding(graph),
        resolution=resolution,
        definitions=tuple(definitions),
        symbols=tuple(symbols),
        relations=tuple(relations[:limit]),
        diagnostics=tuple(diagnostics[:limit]),
        coverage=CodeQueryCoverage(
            analysis="static",
            complete=not (
                truncated
                or depth_bounded
                or diagnostics
                or unattributed
                or resolution == RESOLUTION_AMBIGUOUS
                or expansion == EXPANSION_UNAVAILABLE
            ),
            truncated=truncated,
            depth_bounded=depth_bounded,
            unattributed_inbound=unattributed,
            applied_depth=depth,
            applied_limit=limit,
            graph_expansion=expansion,
            expansion_reason=expansion_reason,
        ),
        freshness=ProjectionFreshness(
            as_of=_timestamp(instant_us),
            projection_versions={CODE_PROJECTION: CODE_PROJECTION_VERSION},
            projection_watermarks={CODE_PROJECTION: str(graph.watermark_us)},
            stale=newest_visible_capture(
                connection,
                workspace_id=context.workspace_id,
                snapshot_id=graph.scope.snapshot_id,
                grant=grant,
                evidence_labels=evidence_labels,
            )
            is not None,
        ),
        context=assembled,
    )
    _revalidate(connection, context, grant, result, graph)
    return result.to_wire()


def file_admitted(
    grant: EvidenceLabelGrant,
    evidence_labels: Mapping[str, tuple[str, ...]],
    *,
    repository_id: str,
    manifest_evidence_id: str,
    relative_path: str,
    content_digest: str,
) -> bool:
    """Whether `grant` admits one captured file of one analysed snapshot.

    The two artifacts every fact read out of that file cites: its own and the snapshot's
    manifest. A diagnostic, which is no governed version, is held to exactly these.
    """
    own = evidence_labels.get(file_evidence_id(repository_id, relative_path, content_digest), ())
    return grant.permits((*evidence_labels.get(manifest_evidence_id, ()), *own))


def newest_visible_capture(
    connection: Any,
    *,
    workspace_id: str,
    snapshot_id: str,
    grant: EvidenceLabelGrant,
    evidence_labels: Mapping[str, tuple[str, ...]],
) -> str | None:
    """The newest capture after `snapshot_id` on its stream that `grant` admits, or None.

    A capture whose manifest the caller may not see is skipped rather than reported, so a
    hidden newer capture neither names itself nor makes the analysis read as stale.
    """
    for later, manifest in newer_captures(
        connection, workspace_id=workspace_id, snapshot_id=snapshot_id
    ):
        if manifest is None or grant.permits(evidence_labels.get(manifest, ())):
            return later
    return None


def _visible(
    graph: CodeGraph,
    grant: EvidenceLabelGrant,
    labels: Mapping[tuple[str, str], tuple[str, ...]],
    evidence_labels: Mapping[str, tuple[str, ...]],
) -> CodeGraph:
    """`graph` narrowed to what this grant admits, nodes first and edges after.

    The order is the security property, and it is `engineering.expand`'s: a definition the
    grant refuses is removed from the universe, and every relation that touches one goes
    with it -- as the relation's own governed version must be admitted too. So a hidden
    endpoint cannot be a neighbour, cannot be a step on a path, cannot raise a degree,
    cannot consume a limit and cannot move `depth_bounded`. It is not *past* the depth; it
    is not in the answer's universe at all, which is the same thing `storage/graph.py`
    says about an endpoint its own filters removed. A relation's own version is checked
    even when every definition is admitted: it can cite evidence neither of its ends does.

    Diagnostics go the same way. A diagnostic is the import's record of a candidate it
    refused; it stays only if the grant admits the file its own source binding names and
    every definition it names is still in the universe, so a refused call read out of a
    hidden file, or one pointing at a hidden definition, is no diagnostic, no count and no
    `complete: false`. Those that stay are anchored below to definitions the walk
    actually expanded.
    """

    def admits(fact: CodeFact) -> bool:
        return grant.permits(labels.get((fact.record_id, fact.version_id), ()))

    symbols = {key: fact for key, fact in graph.symbols.items() if admits(fact)}
    hidden = graph.symbols.keys() - symbols.keys()
    scope = graph.scope
    return CodeGraph(
        scope=scope,
        watermark_us=graph.watermark_us,
        symbols=MappingProxyType(symbols),
        edges=tuple(
            edge
            for edge in graph.edges
            if edge.source_key in symbols and edge.target_key in symbols and admits(edge.fact)
        ),
        diagnostics=tuple(
            (reason, document)
            for reason, document in graph.diagnostics
            if document["source_key"] not in hidden
            and document["target_key"] not in hidden
            and file_admitted(
                grant,
                evidence_labels,
                repository_id=scope.repository_id,
                manifest_evidence_id=scope.manifest_evidence_id,
                relative_path=document["source"]["relative_path"],
                content_digest=document["source"]["content_digest"],
            )
        ),
    )


def _seed_applies(seed: Any, graph: CodeGraph) -> bool:
    """Whether a search's seed was selected against the analysis applicable now."""
    scope = graph.scope
    return (seed.repository_id, seed.snapshot_id, seed.producer_id, seed.profile_digest) == (
        scope.repository_id,
        scope.snapshot_id,
        scope.producer_id,
        scope.profile_digest,
    )


def _input(context: OperationContext) -> CodeQueryInput:
    decoded: CodeQueryInput | None
    try:
        candidate = CodeQueryInput.from_wire(context.request.input)
        validate_code_query_input(candidate)
        decoded = candidate
    except (ContractDecodeError, ContractSemanticError):
        decoded = None
    if decoded is None:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
    return decoded


def _subjects(graph: CodeGraph, request: CodeQueryInput) -> tuple[str, tuple[str, ...]]:
    """How the subject resolved, and its keys or every ambiguous choice, in key order.

    A seed whose binding already matched resolves to exactly the definitions it names, in
    key order. Every one of them must be a visible definition of this import: a seed that
    half resolves is a seed selected against material this answer does not hold, and
    answering it for the half that survives would silently narrow a caller's search.
    """
    if request.seed is not None:
        keys = tuple(sorted(set(request.seed.semantic_keys)))
        if not all(key in graph.symbols for key in keys):
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        return request.seed.origin, keys
    if request.semantic_key is not None:
        found = graph.symbols.get(request.semantic_key)
        if found is None or (request.name is not None and found.content["name"] != request.name):
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        return "semantic_key", (request.semantic_key,)
    matches = tuple(
        sorted(key for key, fact in graph.symbols.items() if fact.content["name"] == request.name)
    )
    if not matches:
        raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
    return ("unique_name" if len(matches) == 1 else RESOLUTION_AMBIGUOUS), matches


def _walk(
    edges: list[CodeEdge],
    seeds: Sequence[str],
    *,
    inbound: bool,
    outbound: bool,
    depth: int,
) -> tuple[dict[str, int], list[tuple[int, CodeEdge]], bool]:
    """Shortest-path depths from `seeds`, every edge followed with its step, and whether
    relations past `depth` were left unfollowed.

    An intent may walk one direction or both. Both is one breadth-first search over the
    union, not two searches joined afterwards, so a definition reachable either way gets
    the one shortest depth rather than whichever direction happened to run second.

    **One relationship is followed once.** A walk that goes both ways reaches an edge from
    either of its ends, and a walk from several seeds can have both ends of one edge at
    depth 0; a walk recording each arrival would return that relationship twice, at two
    depths, and double the subject's degree. `followed` therefore keeps the first arrival
    -- the shortest step, because the steps run in order -- and drops the rest.
    """
    leaving: dict[str, list[tuple[CodeEdge, str]]] = {}
    for edge in edges:
        if inbound:
            leaving.setdefault(edge.target_key, []).append((edge, edge.source_key))
        if outbound:
            leaving.setdefault(edge.source_key, []).append((edge, edge.target_key))
    depths = {seed: 0 for seed in seeds}
    followed: list[tuple[int, CodeEdge]] = []
    seen: set[str] = set()
    frontier = list(depths)
    for step in range(1, depth + 1):
        reached = []
        for key in frontier:
            for edge, other in leaving.get(key, ()):
                edge_key = str(edge.fact.content["edge_key"])
                if edge_key not in seen:
                    seen.add(edge_key)
                    followed.append((step, edge))
                if other not in depths:
                    depths[other] = step
                    reached.append(other)
        frontier = reached
    return depths, followed, any(key in leaving for key in frontier)


def _diagnostics(
    graph: CodeGraph,
    types: frozenset[str],
    expanded: set[str],
    *,
    inbound: bool,
    outbound: bool,
    runtime_only: bool,
) -> list[CodeDiagnostic]:
    """Refused candidates of the walked relation types at a definition the walk expanded."""
    mapping = RELATIONSHIPS.get(graph.scope.producer_id, {})
    found = []
    for reason, document in graph.diagnostics:
        mapped = mapping.get(str(document["relationship_type"]))
        anchors = {
            *((document["target_key"],) if inbound else ()),
            *((document["source_key"],) if outbound else ()),
        }
        if mapped is None or mapped[0] not in types or not anchors & expanded:
            continue
        if runtime_only and mapped[1]:
            continue
        found.append(
            CodeDiagnostic(
                edge_key=str(document["edge_key"]),
                reason=reason,
                producer_relationship=str(document["relationship_type"]),
                evidence_strength=str(document["evidence_strength"]),
                resolution_status=str(document["resolution_status"]),
                source_key=str(document["source_key"]),
                target_key=document["target_key"],
                target_descriptor=document["unresolved_target"],
                source=_span(document["source"]),
            )
        )
    return sorted(found, key=lambda diagnostic: diagnostic.edge_key)


def _span(source: Mapping[str, Any]) -> CodeSourceSpan:
    locator = source["locator"]
    return CodeSourceSpan(
        relative_path=source["relative_path"],
        content_digest=source["content_digest"],
        start_line=locator["start_line"],
        start_column=locator["start_column"],
        end_line=locator["end_line"],
        end_column=locator["end_column"],
        start_byte=locator.get("start_byte"),
        end_byte=locator.get("end_byte"),
    )


def _reference(fact: CodeFact) -> RecordVersionReference:
    return RecordVersionReference(record_id=fact.record_id, version=fact.version_id)


def _symbol(key: str, fact: CodeFact, *, depth: int | None = None) -> CodeSymbol:
    return CodeSymbol(
        semantic_key=key,
        node_kind=fact.content["node_kind"],
        display_name=fact.content["name"],
        source=_span(fact.content["source"]),
        record=_reference(fact),
        depth=depth,
    )


def _relation(edge: CodeEdge, depth: int) -> CodeRelation:
    content = edge.fact.content
    return CodeRelation(
        edge_key=content["edge_key"],
        relation_type=edge.relation_type,
        producer_relationship=content["producer_relationship"],
        type_only=bool(content["type_only"]),
        evidence_strength=content["evidence_strength"],
        source_key=edge.source_key,
        target_key=edge.target_key,
        depth=depth,
        source=_span(content["source"]),
        record=_reference(edge.fact),
    )


def _spans(
    context: OperationContext, graph: CodeGraph, disclosed: Sequence[CodeSymbol]
) -> list[CodeSpanContent]:
    """The captured source of everything this answer discloses, by content address.

    `disclosed` is already the bounded list, so the spans are bounded with it and in the
    same order. Each one is read from the blob the fact's own `content_digest` names, and
    a fact whose captured object cannot be read and verified contributes no span at all
    rather than a span of whatever is there now.
    """
    layout = getattr(getattr(context, "service", None), "layout", None)
    blobs_root = getattr(layout, "blobs_path", None)
    if not isinstance(blobs_root, Path):
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
    captured = CapturedSource(blobs_root)
    found = []
    for symbol in disclosed:
        source = graph.symbols[symbol.semantic_key].content["source"]
        span = captured.span(source, max_bytes=CODE_CONTEXT_MAX_SPAN_BYTES)
        if span is None:
            continue
        found.append(
            CodeSpanContent(
                semantic_key=symbol.semantic_key,
                source=symbol.source,
                byte_length=span.byte_length,
                truncated=span.truncated,
                text=span.text,
            )
        )
    return found


def _architecture(
    context: OperationContext,
    connection: Any,
    request: CodeQueryInput,
    *,
    grant: EvidenceLabelGrant,
    labels: Mapping[tuple[str, str], tuple[str, ...]],
    instant_us: int,
) -> CodeArchitectureContext:
    """The task-context component, through the workspace's one context authority.

    Three gates, in this order, and each of them is the reason the next one is reachable:

    1. **the caller holds the context authority independently.** `code.query` is granted
       as a graph read; the material below is the Context Pack authority's. The session's
       own `granted_operations` is what decides, so a principal granted the code read and
       not the context build gets `unavailable` / `not_authorised` -- never a component,
       and never a refusal that would make the whole code answer depend on it;
    2. **the authority is statable.** The pack records the authority, scopes and purpose
       it was built under verbatim, so an absent one is this build's wiring fault and is
       refused outright rather than reported as an unavailable component;
    3. **admission.** Only governed versions this caller may see, that carry accepted
       support, and that are not code. Applied before the freeze, so the manifest the
       pack attests is the admitted set rather than a wider one it then showed less of.

    Everything the authority itself refuses -- a projection that is missing or behind, a
    frontier wider than one pack, a budget that carries nothing -- becomes an unavailable
    component with that reason rather than a refusal of the code answer. The code facts
    are true whether or not a pack could be built beside them.
    """
    # `request.context` is required of this intent by semantic validation, so the first
    # disjunct is a type narrowing rather than a second refusal path.
    if (
        request.context is None
        or CONTEXT_PACK_BUILD_OPERATION not in context.granted_operations
    ):
        return CodeArchitectureContext(
            availability=ARCHITECTURE_UNAVAILABLE, reason=ARCHITECTURE_NOT_AUTHORISED
        )
    authority, scopes, purpose = context.authority, context.scopes, context.purpose
    if authority is None or scopes is None or purpose is None:
        raise OperationError(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_EFFECTIVE_AUTHORITY
        )
    refused: str | None = None
    try:
        pack = build_task_context(
            connection,
            workspace_id=context.workspace_id,
            principal=context.principal,
            authority=authority,
            scopes=scopes,
            purpose=purpose,
            request=request.context,
            resolved_at_us=instant_us,
            resolution=_timestamp(instant_us),
            retrieval_version=CODE_CONTEXT_RETRIEVAL_VERSION,
            policy_versions=CODE_CONTEXT_POLICY_VERSIONS,
            admit=_admission(
                connection,
                workspace_id=context.workspace_id,
                grant=grant,
                labels=labels,
                instant_us=instant_us,
            ),
        )
    except OperationError as unavailable:
        reason = _ARCHITECTURE_REASONS.get(unavailable.code)
        if reason is None:
            raise
        refused = reason
    if refused is not None:
        return CodeArchitectureContext(
            availability=ARCHITECTURE_UNAVAILABLE, reason=refused
        )
    return CodeArchitectureContext(availability=ARCHITECTURE_AVAILABLE, pack=pack)


def _admission(
    connection: Any,
    *,
    workspace_id: str,
    grant: EvidenceLabelGrant,
    labels: Mapping[tuple[str, str], tuple[str, ...]],
    instant_us: int,
) -> Callable[[tuple[GovernedRecordValue, ...]], tuple[GovernedRecordValue, ...]]:
    """What may enter the frozen governed partitions of an architecture component.

    **Supported, and visibly so.** 0009's `reconciliation.supports` is the one relation
    that says one governed record supports another, and D18 is that an architecture
    assertion needs accepted support. So a record is admitted only as the *target* of an
    accepted supports relation whose source record, and whose own relation record, this
    caller may also see. A hidden support is therefore not a weaker support: the claim it
    would have carried is simply not architecture knowledge for this caller, and its
    absence is indistinguishable from the claim never having been supported at all.

    **Not code.** `engineering.code` is excluded by name, not merely by the fact that no
    code fact carries support today. A code fact disclosed here would be the same fact
    appearing twice under two kinds of claim, and the ban is what makes "similarity and
    prose never become code edges" hold in the other direction too.

    The snapshot is read at the caller's own instant, so the supports it sees and the code
    facts beside it are one as-of read rather than two moments that happen to agree.
    """
    graph = read_graph_snapshot(
        connection, workspace_id=workspace_id, resolution_instant_us=instant_us
    )

    def admits(key: tuple[str, str]) -> bool:
        return grant.permits(labels.get(key, ()))

    supported = {
        relation.target
        for relation in graph.relations
        if relation.relation_type == SUPPORTS_RELATION
        and admits(relation.source)
        and admits(relation.target)
        and admits(relation.relation)
    }

    def admit(
        values: tuple[GovernedRecordValue, ...],
    ) -> tuple[GovernedRecordValue, ...]:
        return tuple(
            value
            for value in values
            if value.record.domain_scope != DOMAIN_SCOPE
            and _identity(value.record) in supported
            and admits(_identity(value.record))
        )

    return admit


def _identity(record: GovernedRecord) -> tuple[str, str]:
    identity = record.provenance.identity
    return identity.record_id, identity.version


def _revalidate(
    connection: Any,
    context: OperationContext,
    grant: EvidenceLabelGrant,
    result: CodeQueryResult,
    graph: CodeGraph,
) -> None:
    """Re-read the ACL and refuse unless every disclosed member is still visible.

    The selection above ran against labels read before the walk. This reads them again,
    after the answer exists and before it goes to wire, because a revocation committed in
    between would otherwise be disclosed by an answer that was authorised a moment ago.
    Both partitions are re-checked: every governed version this result names -- the
    definitions, the reached symbols, the relation records, the definitions a diagnostic
    names, and the component's own records, history and context models -- and every
    evidence artifact the component cites or a diagnostic's own source binding names.

    A member that is no longer visible makes the whole answer `not_found`, with the same
    frozen message a subject that does not exist gets. Recomputing the answer without it
    would disclose that something was dropped, and reporting a count would disclose how
    much; the refusal a caller sees is the one they would have seen had the subject never
    been there, which is `handlers/engineering.py`'s rule for a hidden exact target.
    """
    versions = {(item.record.record_id, item.record.version) for item in result.definitions}
    versions.update((item.record.record_id, item.record.version) for item in result.symbols)
    versions.update((item.record.record_id, item.record.version) for item in result.relations)
    evidence: set[str] = set()
    binding = result.binding
    for diagnostic in result.diagnostics:
        for key in (diagnostic.source_key, diagnostic.target_key):
            named = None if key is None else graph.symbols.get(key)
            if named is not None:
                versions.add((named.record_id, named.version_id))
        evidence.update(
            (
                binding.manifest_evidence_id,
                file_evidence_id(
                    binding.repository_id,
                    diagnostic.source.relative_path,
                    diagnostic.source.content_digest,
                ),
            )
        )
    pack = None if result.context is None else result.context.architecture.pack
    if pack is not None:
        for partition in (pack.records, pack.history, pack.context_models):
            versions.update(_identity(record) for record in partition)
        evidence.update(artifact.evidence_id for artifact in pack.evidence)

    labels = read_governed_record_labels(connection, workspace_id=context.workspace_id)
    if not all(grant.permits(labels.get(key, ())) for key in versions):
        raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
    if evidence:
        carried = read_evidence_labels(connection, workspace_id=context.workspace_id)
        if not all(grant.permits(carried.get(key, ())) for key in evidence):
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)


def _binding(graph: CodeGraph) -> CodeAnalysisBinding:
    scope = graph.scope
    return CodeAnalysisBinding(
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        producer_id=scope.producer_id,
        producer_version=scope.producer_version,
        profile_digest=scope.profile_digest,
        import_id=scope.import_id,
        batch_digest=scope.batch_digest,
        policy_id=scope.policy_id,
        policy_version=scope.policy_version,
        policy_digest=scope.policy_digest,
        manifest_evidence_id=scope.manifest_evidence_id,
        activated_at=_timestamp(scope.activated_at_us),
    )


def _timestamp(microseconds: int) -> str:
    """Millisecond `Timestamp`, truncated, as `graph.traverse` renders instants."""
    moment = datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


__all__ = [
    "CODE_CONTEXT_POLICY_VERSIONS",
    "CODE_CONTEXT_RETRIEVAL_VERSION",
    "CODE_PROJECTION",
    "CODE_PROJECTION_VERSION",
    "CONTEXT_PACK_BUILD_OPERATION",
    "SUPPORTS_RELATION",
    "code_query",
]
