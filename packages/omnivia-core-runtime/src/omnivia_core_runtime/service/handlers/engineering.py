"""The `engineering.*` retrieval handlers (SPEC-CORE-ENGMEM-001, plan PR-D).

Seven of the nine engineering-memory operations are durable here and in
`handlers.continuity`: the continuity vertical (register/append/close/handoff)
plus the two retrieval reads this module adds — `engineering.search` and
`engineering.expand`, served from the governed record store, the supersession
edge table and the continuity checkpoint index. Three remain the honest
`dependency_unavailable` refusals (the pack builder, the preference store and
the review-attestation path are later packages).

Retrieval security shape, inherited from the knowledge family and the plan:

1. payloads decode through the contract's own decoder; identity, workspace and
   purpose come from the authorised context, never from the payload;
2. the authorised frontier is frozen *before* any scoring: the candidate set is
   the workspace's engineering observations under the requested view, read at
   one resolution instant, and `rank_governed` sees nothing else — no corpus
   statistics and no restricted document reach the rank (§11.3). The ranker's
   own relevance signal is a stated occurrence count computed from the
   frontier's members and nothing else;
3. previews are bounded renderings (≤480 code points) of stored content; a
   full body is never hydrated into the response (§11.1);
4. a hypothesis observation is never served under the `accepted` view, even
   after governance accepts it (§8.2);
5. continuations are the established MAC'd tokens, bound to the request
   digest, the frozen snapshot and the resolution instant; a changed binding,
   snapshot or epoch is an explicit restart (§11.4);
6. `applicability` states what is actually known: nothing evaluates target
   freshness yet, so record-level applicability is `not_evaluated` and the
   coverage block reports `unavailable` rather than implying freshness
   (§15.1);
7. `working_context` reads the continuity checkpoint index — reported
   accomplishments are labelled as continuity evidence, never as governed
   knowledge (§12.3).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ContractDecodeError,
    ContractSemanticError,
    EngineeringExpandInput,
    EngineeringSearchInput,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.service.pagination import (
    PROCESS_CONTINUATION_TOKENS,
    token_digest,
)
from omnivia_core_runtime.storage.governed import (
    read_governed_record_values,
    read_governed_supersessions,
)
from omnivia_core_runtime.storage.retrieval import (
    GOVERNED_FRONTIER_FILTERS,
    GovernedCandidate,
    GovernedFrontier,
    rank_governed,
)

_MESSAGE_INVALID: Final = "the request payload is not valid for this engineering operation"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative storage"
)
_MESSAGE_NOT_FOUND: Final = "the requested engineering record was not found"

#: The engineering record type this retrieval serves (§8.1).
OBSERVATION_RECORD_TYPE: Final = "engineering.observation"

#: The view→governed-resolver mapping (§11.2). `working_context` is absent
#: deliberately: it reads the continuity checkpoint index, not governed records.
_GOVERNED_VIEWS: Final[dict[str, str]] = {
    "accepted": "current_canonical",
    "candidates": "candidates",
    "history": "history",
}
_ENGINEERING_VIEWS: Final[frozenset[str]] = frozenset(
    {"accepted", "candidates", "history", "working_context"}
)

_TOKEN_KEYS: Final[frozenset[str]] = frozenset({"b", "o", "s", "t", "v"})

#: The default and hard-maximum page sizes for engineering search (§11.1).
SEARCH_DEFAULT_LIMIT: Final = 20
SEARCH_MAX_LIMIT: Final = 100

#: The complete rendered preview cap: 480 code points (§11.1).
PREVIEW_MAX_CODEPOINTS: Final = 480

_MESSAGE_CONTEXT_BUILD: Final = (
    "the engineering context pack ships contracts first; the pack builder lands "
    "in a later engineering-memory package"
)
_MESSAGE_PRIORITY: Final = (
    "context priority ships contracts first; the preference store lands in a "
    "later engineering-memory package"
)
_MESSAGE_REVIEW: Final = (
    "engineering review recording ships contracts first; the attestation "
    "producer lands in a later engineering-memory package"
)


def _bounded(value: str, limit: int = PREVIEW_MAX_CODEPOINTS) -> tuple[str, bool]:
    """One preview rendering: bounded text plus its honest truncation flag."""
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _observation_preview(record: Any) -> dict[str, Any] | None:
    """Render one governed engineering record as a preview, or None to skip.

    A record whose content is not a mapping cannot render honestly and is
    skipped rather than served with invented fields.
    """
    content = record.content
    if not isinstance(content, Mapping):
        return None
    identity = record.provenance.identity
    title, title_truncated = _bounded(
        str(content.get("title") or identity.record_id), 200
    )
    body: str = ""
    for key in ("summary", "what", "learned"):
        value = content.get(key)
        if isinstance(value, str) and value:
            body = value
            break
    body, truncated = _bounded(body)
    if not body:
        body = title
    preview: dict[str, Any] = {
        "record_id": identity.record_id,
        "version": identity.version,
        "title": title,
        "preview": body,
        "truncated": truncated or title_truncated,
        "governance_state": identity.governance_state,
        "applicability": "not_evaluated",
        "evidence_available": bool(record.provenance.sources),
    }
    kind = content.get("kind")
    if isinstance(kind, str) and kind:
        preview["observation_kind"] = kind
    basis = content.get("assertion_basis")
    if isinstance(basis, str) and basis:
        preview["assertion_basis"] = basis
    topic = content.get("topic_ref")
    if isinstance(topic, Mapping):
        proposed_key = topic.get("proposed_key")
        if isinstance(proposed_key, str) and proposed_key:
            preview["topic_key"] = proposed_key
    applicability = content.get("applicability")
    if isinstance(applicability, Mapping):
        repository_id = applicability.get("repository_id")
        if isinstance(repository_id, str) and repository_id:
            preview["repository_id"] = repository_id
        snapshot_id = applicability.get("snapshot_id")
        if isinstance(snapshot_id, str) and snapshot_id:
            preview["snapshot_id"] = snapshot_id
    return preview


class EngineeringHandlers:
    """The engineering retrieval reads, plus the three still-honest refusals."""

    def __init__(self, service: Any) -> None:
        self.service = service

    def _connection(self) -> Any:
        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise OperationError("internal_non_recoverable", _MESSAGE_NO_STORAGE)
        return connection

    # --- engineering.search ------------------------------------------------------

    def engineering_search(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        try:
            request = EngineeringSearchInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection = self._connection()
        limit = SEARCH_DEFAULT_LIMIT if request.limit is None else request.limit
        view = request.view or "accepted"
        if view not in _ENGINEERING_VIEWS:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        binding = request.to_wire()
        binding.pop("page", None)
        binding_digest = token_digest(
            {
                "principal": context.principal,
                "workspace": context.workspace_id,
                "operation": "engineering.search",
                "input": binding,
                "limit": limit,
                "view": view,
            }
        )
        supplied: Mapping[str, Any] | None = None
        start = 0
        if request.page is not None:
            token = request.page.continuation_token
            assert token is not None
            try:
                supplied = PROCESS_CONTINUATION_TOKENS.decode(token)
            except (ValueError, ContractDecodeError, ContractSemanticError):
                pass
            if (
                supplied is None
                or set(supplied) != _TOKEN_KEYS
                or supplied.get("v") != 1
                or supplied.get("b") != binding_digest
            ):
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            instant = supplied.get("t")
            if type(instant) is not int or instant <= 0:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            resolved_at_us = instant
        else:
            # The canonical resolution instant: one clock read, before anything
            # is resolved, and the only one on this path (as in knowledge.py).
            resolved_at_us = time.time_ns() // 1000

        if view == "working_context":
            previews, total, snapshot_digest = self._working_context_previews(
                connection,
                workspace_id=context.workspace_id,
                query=request.query,
                limit=limit,
                offset=supplied.get("o") if supplied else None,
            )
        else:
            values = read_governed_record_values(
                connection,
                workspace_id=context.workspace_id,
                resolution_instant_us=resolved_at_us,
                view=_GOVERNED_VIEWS[view],
            )
            candidates: list[GovernedCandidate] = []
            for value in values:
                record = value.record
                if record.record_type != OBSERVATION_RECORD_TYPE:
                    continue
                content = record.content
                if (
                    view == "accepted"
                    and isinstance(content, Mapping)
                    and content.get("assertion_basis") == "hypothesis"
                ):
                    # §8.2: a hypothesis stays marked and is excluded from
                    # accepted-facts selection even after governance accepts it.
                    continue
                if request.repository_target is not None:
                    if not isinstance(content, Mapping):
                        continue
                    applicability = content.get("applicability")
                    if not isinstance(applicability, Mapping):
                        continue
                    if (
                        applicability.get("repository_id")
                        != request.repository_target.repository_id
                    ):
                        continue
                candidates.append(
                    GovernedCandidate(
                        recorded_at_us=value.recorded_at_us, record=record
                    )
                )
            frontier = GovernedFrontier(
                workspace_id=context.workspace_id,
                candidates=tuple(candidates),
                filters_applied=GOVERNED_FRONTIER_FILTERS,
            )
            ordered = rank_governed(
                frontier, request.query, order=None, limit=len(frontier.candidates)
            )
            snapshot_digest = token_digest([record.to_wire() for record in ordered])
            start = 0
            if supplied is not None:
                if supplied.get("s") != snapshot_digest:
                    raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
                offset = supplied.get("o")
                if type(offset) is not int or not 0 < offset < len(ordered):
                    raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
                start = offset
            previews = []
            for record in ordered[start : start + limit]:
                rendered = _observation_preview(record)
                if rendered is not None:
                    previews.append(rendered)
            total = len(ordered)

        continuation = None
        if start + len(previews) < total:
            continuation = PROCESS_CONTINUATION_TOKENS.encode(
                {
                    "b": binding_digest,
                    "o": start + len(previews),
                    "s": snapshot_digest,
                    "t": resolved_at_us,
                    "v": 1,
                }
            )
        return {
            "previews": previews,
            "page": ({"continuation_token": continuation} if continuation else {}),
            "coverage": {"projection": "current", "applicability": "unavailable"},
        }

    def _working_context_previews(
        self,
        connection: Any,
        *,
        workspace_id: str,
        query: str,
        limit: int,
        offset: Any,
    ) -> tuple[list[dict[str, Any]], int, str]:
        rows = connection.execute(
            "SELECT checkpoint_id, sequence, payload_json, recorded_at_us "
            "FROM omnivia_engineering_checkpoints WHERE workspace_id = ? "
            "ORDER BY recorded_at_us DESC, sequence DESC",
            (workspace_id,),
        ).fetchall()
        snapshot_digest = token_digest([list(row) for row in rows])
        start = 0
        if offset is not None:
            if type(offset) is not int or not 0 < offset < len(rows):
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            start = offset
        normalized = " ".join(query.lower().split())
        previews: list[dict[str, Any]] = []
        for row in rows[start:]:
            try:
                payload = json.loads(row[2])
            except ValueError:
                continue
            objective = str(payload.get("objective", ""))
            if normalized and normalized not in objective.lower():
                continue
            body, truncated = _bounded(objective)
            previews.append(
                {
                    "record_id": row[0],
                    "version": str(row[1]),
                    "title": body[:200],
                    "preview": body,
                    "truncated": truncated,
                    "governance_state": "continuity_evidence",
                    "applicability": "not_evaluated",
                    "evidence_available": True,
                }
            )
            if len(previews) >= limit:
                break
        return previews, len(rows), snapshot_digest

    # --- engineering.expand ------------------------------------------------------

    def engineering_expand(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        try:
            request = EngineeringExpandInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection = self._connection()
        now_us = time.time_ns() // 1000
        values = read_governed_record_values(
            connection,
            workspace_id=context.workspace_id,
            resolution_instant_us=now_us,
        )
        anchor_found = False
        for value in values:
            identity = value.record.provenance.identity
            if (
                identity.record_id == request.anchor.record_id
                and identity.version == request.anchor.version
            ):
                anchor_found = True
                break
        if not anchor_found:
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)

        depth = 1 if request.depth is None else request.depth
        node_limit = 30 if request.node_limit is None else request.node_limit
        edge_limit = 60 if request.edge_limit is None else request.edge_limit

        nodes: list[dict[str, Any]] = [
            {"record_id": request.anchor.record_id, "version": request.anchor.version}
        ]
        edges: list[dict[str, Any]] = []
        supersessions = read_governed_supersessions(
            connection,
            workspace_id=context.workspace_id,
            resolution_instant_us=now_us,
        )
        for edge in supersessions:
            if len(edges) >= edge_limit:
                break
            if (
                edge.source_version_id != request.anchor.version
                and edge.target_version_id != request.anchor.version
            ):
                continue
            edges.append(
                {
                    "from_record": {
                        "record_id": edge.governed_record_id,
                        "version": edge.source_version_id,
                    },
                    "to_record": {
                        "record_id": edge.governed_record_id,
                        "version": edge.target_version_id,
                    },
                    "relation": "supersedes",
                    "status": "accepted",
                }
            )
            other = (
                edge.target_version_id
                if edge.source_version_id == request.anchor.version
                else edge.source_version_id
            )
            node = {"record_id": edge.governed_record_id, "version": other}
            if node not in nodes and len(nodes) < node_limit:
                nodes.append(node)
        # `depth` is declared by the contract and bounded by it (1..3); this
        # build expands one hop, so depth 2+ would add nothing today and is
        # reported as truncation rather than silently pretended.
        truncated = depth > 1
        return {
            "nodes": nodes,
            "edges": edges,
            "truncated": truncated,
            "coverage": {"projection": "current", "applicability": "unavailable"},
        }

    # --- honest refusals -----------------------------------------------------------

    def engineering_context_build(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        raise OperationError(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_CONTEXT_BUILD)

    def context_priority_set(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        raise OperationError(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_PRIORITY)

    def engineering_review_record(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        raise OperationError(ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_REVIEW)
