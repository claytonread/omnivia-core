"""The `code.query` handler -- static code facts from one applicable analysis import (W13-A).

A read beside `graph.traverse`, served by the same local-owner read family under the same
`knowledge_retrieval` purpose and the same `graph:read` scope. It reads one value from
`storage/code_query.py` and answers five closed intents over it:

* `definition` -- the subject itself;
* `references` -- inbound `code.references`, one step;
* `callers` / `callees` -- inbound / outbound `code.calls`, to the requested depth;
* `dependencies` -- outbound `code.imports` and `code.references`, to the requested depth,
  with `runtime_only` dropping the type-only ones from both the answer and the walk.

**The binding is checked, not trusted.** The request names a repository, snapshot,
producer and profile; the storage read refuses unless the import 0074 holds applicable for
that scope at this instant analysed exactly that snapshot. A missing or no-longer-applicable
analysis is `projection_unavailable` with `details.readiness = "not_ready"` -- the same code
the engineering reads use for a projection that is not there -- never an empty success.

**Static, and said so.** Every relation is an accepted `syntactic`/`resolved_static`
edge of that one import. Candidates the import refused (unresolved, dynamic, external, out
of policy) are returned as diagnostics when they bear on the subject and are never walked.
Inbound intents always state `unattributed_inbound`, because an unresolved occurrence
elsewhere cannot be attributed to the subject by static analysis; `complete` is true only
when nothing bounded the answer. Nothing here is runtime behaviour or test coverage.

**Ambiguity is returned, not resolved.** A `name` shared by several definitions answers
`resolution = "ambiguous"` with every qualified choice in key order and walks nothing.

The workspace and principal are the authorized context's; the payload carries neither.
Refusal messages are frozen constants and the decode failure is raised after its handler
ends, so no caller value reaches `__context__`.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    CODE_QUERY_DEFAULT_DEPTH,
    CODE_QUERY_DEFAULT_LIMIT,
    CODE_QUERY_INTENT_CALLEES,
    CODE_QUERY_INTENT_CALLERS,
    CODE_QUERY_INTENT_DEPENDENCIES,
    CODE_QUERY_INTENT_REFERENCES,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_PROJECTION_UNAVAILABLE,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    CodeAnalysisBinding,
    CodeDiagnostic,
    CodeQueryCoverage,
    CodeQueryInput,
    CodeQueryResult,
    CodeRelation,
    CodeSourceSpan,
    CodeSymbol,
    ContractDecodeError,
    ContractSemanticError,
    ProjectionFreshness,
    RecordVersionReference,
    validate_code_query_input,
)
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.code_analysis_import import RELATIONSHIPS
from omnivia_core_runtime.storage.code_query import (
    CodeAnalysisNotReady,
    CodeEdge,
    CodeFact,
    CodeGraph,
    read_code_graph,
)

#: The logical projection this read reports its freshness against, and its shape version.
CODE_PROJECTION: Final = "code.analysis"
CODE_PROJECTION_VERSION: Final = "1"

#: Intent -> (relation types walked, inbound).
_WALKS: Final[Mapping[str, tuple[frozenset[str], bool]]] = {
    CODE_QUERY_INTENT_REFERENCES: (frozenset({"code.references"}), True),
    CODE_QUERY_INTENT_CALLERS: (frozenset({"code.calls"}), True),
    CODE_QUERY_INTENT_CALLEES: (frozenset({"code.calls"}), False),
    CODE_QUERY_INTENT_DEPENDENCIES: (frozenset({"code.imports", "code.references"}), False),
}
_INBOUND_UNATTRIBUTED: Final = frozenset({CODE_QUERY_INTENT_REFERENCES, CODE_QUERY_INTENT_CALLERS})

_MESSAGE_INVALID_INPUT: Final = "the request payload is not a valid code query"
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"
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
    instant_us = time.time_ns() // 1000
    not_ready: str | None = None
    try:
        graph = read_code_graph(
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

    limit = CODE_QUERY_DEFAULT_LIMIT if request.limit is None else int(request.limit)
    resolution, subjects = _subjects(graph, request)
    walk = _WALKS.get(request.intent)
    depth = 0
    symbols: list[CodeSymbol] = []
    relations: list[CodeRelation] = []
    diagnostics: list[CodeDiagnostic] = []
    depth_bounded = False
    if resolution != "ambiguous" and walk is not None:
        types, inbound = walk
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
        depths, followed, depth_bounded = _walk(edges, subjects[0], inbound=inbound, depth=depth)
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
            graph, types, expanded, inbound=inbound, runtime_only=runtime_only
        )
    definitions = [_symbol(key, graph.symbols[key]) for key in subjects]

    truncated = any(len(items) > limit for items in (definitions, symbols, relations, diagnostics))
    unattributed = request.intent in _INBOUND_UNATTRIBUTED and resolution != "ambiguous"
    result = CodeQueryResult(
        intent=request.intent,
        binding=_binding(graph),
        resolution=resolution,
        definitions=tuple(definitions[:limit]),
        symbols=tuple(symbols[:limit]),
        relations=tuple(relations[:limit]),
        diagnostics=tuple(diagnostics[:limit]),
        coverage=CodeQueryCoverage(
            analysis="static",
            complete=not (
                truncated
                or depth_bounded
                or diagnostics
                or unattributed
                or resolution == "ambiguous"
            ),
            truncated=truncated,
            depth_bounded=depth_bounded,
            unattributed_inbound=unattributed,
            applied_depth=depth,
            applied_limit=limit,
        ),
        freshness=ProjectionFreshness(
            as_of=_timestamp(instant_us),
            projection_versions={CODE_PROJECTION: CODE_PROJECTION_VERSION},
            projection_watermarks={CODE_PROJECTION: str(graph.watermark_us)},
            stale=False,
        ),
    )
    return result.to_wire()


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
    """How the subject resolved, and its key or every ambiguous choice, in key order."""
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
    return ("unique_name" if len(matches) == 1 else "ambiguous"), matches


def _walk(
    edges: list[CodeEdge], seed: str, *, inbound: bool, depth: int
) -> tuple[dict[str, int], list[tuple[int, CodeEdge]], bool]:
    """Shortest-path depths from `seed`, every edge followed with its step, and whether
    relations past `depth` were left unfollowed."""
    leaving: dict[str, list[CodeEdge]] = {}
    for edge in edges:
        leaving.setdefault(edge.target_key if inbound else edge.source_key, []).append(edge)
    depths = {seed: 0}
    followed: list[tuple[int, CodeEdge]] = []
    frontier = [seed]
    for step in range(1, depth + 1):
        reached = []
        for key in frontier:
            for edge in leaving.get(key, ()):
                followed.append((step, edge))
                other = edge.source_key if inbound else edge.target_key
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
    runtime_only: bool,
) -> list[CodeDiagnostic]:
    """Refused candidates of the walked relation types at a definition the walk expanded."""
    mapping = RELATIONSHIPS.get(graph.scope.producer_id, {})
    found = []
    for reason, document in graph.diagnostics:
        mapped = mapping.get(str(document["relationship_type"]))
        anchor = document["target_key"] if inbound else document["source_key"]
        if mapped is None or mapped[0] not in types or anchor not in expanded:
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


__all__ = ["CODE_PROJECTION", "CODE_PROJECTION_VERSION", "code_query"]
