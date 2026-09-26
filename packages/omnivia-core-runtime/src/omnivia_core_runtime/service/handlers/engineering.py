"""The `engineering.*` handlers (SPEC-CORE-ENGMEM-001, plans PR-D/PR-F).

All nine engineering-memory operations are durable here and in
`handlers.continuity`: the continuity vertical (register/append/close/handoff),
the retrieval reads (search/expand) served from the governed record store, the
supersession edge table and the continuity checkpoint index, the non-persisted
context pack builder, the priority writes and the review attestations.

The pack builder (§12) is a non-persisting read: one frozen frontier, one
resolution instant, exact budget reconciliation with mandatory notices rendered
first and optional sections dropped lowest-priority first, and a self-verifying
`pack_id` — the canonical SHA-256 of the result after removing exactly the root
`pack_id` and the nested `reproducibility.artifact_checksum`. The v1 renderer's
pinned token counting is a whitespace split, recomputable from the rendering.

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
6. `applicability` reports only what is known. With no assessment for the
   exact (record version, target snapshot), it is `not_evaluated`. With one,
   the stored status is re-assessed conservatively and never reported as
   `matched`, because nothing validates dependency equivalence yet. The
   coverage block stays `unavailable` rather than implying freshness (§15.1);
7. `working_context` reads the continuity checkpoint index — reported
   accomplishments are labelled as continuity evidence, never as governed
   knowledge (§12.3).
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ERROR_CODE_TOKEN_LIMIT_EXCEEDED,
    ContextPrioritySetInput,
    ContextPrioritySetResult,
    ContractDecodeError,
    ContractSemanticError,
    EngineeringContextBuildInput,
    EngineeringExpandInput,
    EngineeringReviewRecordInput,
    EngineeringReviewRecordResult,
    EngineeringSearchInput,
    idempotency_equivalence,
    to_canonical_json,
)
from omnivia_core_runtime.ownership.identity import Clock, SystemClock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.mutation import (
    MutationIdempotencyConflict,
    MutationPreconditionFailed,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
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
from omnivia_core_runtime.storage import engineering_applicability as app_storage
from omnivia_core_runtime.storage.governed import (
    read_governed_record_values,
    read_governed_supersessions,
)
from omnivia_core_runtime.storage.memory import IdentifierAllocator, random_identifier
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
_MESSAGE_PRECONDITION: Final = (
    "the engineering target moved under this request; re-read and re-decide"
)

#: The engineering domain this retrieval serves: observations ride the frozen
#: catalogue's finding/risk/decision types under this domain (§22.1).
OBSERVATION_DOMAIN: Final = "engineering.codebase"

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

_MESSAGE_BUDGET: Final = (
    "the minimum safe engineering context does not fit the effective budget"
)
#: The pack renderer and its pinned, deterministic token counting method. A
#: whitespace split is the v1 pinned tokenizer: recomputable by hand from the
#: rendering, and never reported as anything smarter than it is.
RENDERER_VERSION: Final = "eng-render-1"
BUILDER_VERSION: Final = "eng-build-1"

#: Server hard budget ceilings (§12.4). Effective budgets are the minimum of the
#: caller request and these ceilings; both token and byte caps are simultaneous.
BUDGET_CEILING_TOKENS: Final = 16000
BUDGET_CEILING_BYTES: Final = 65536
BUDGET_DEFAULT_TOKENS: Final = 4000
BUDGET_DEFAULT_BYTES: Final = 16384

#: Section drop order when the rendering exceeds the effective budget: optional
#: working-context material first, then history. Mandatory notices and accepted
#: knowledge are never dropped to fit (§12.5).
_SECTION_DROP_ORDER: Final[tuple[str, ...]] = (
    "working_context",
    "history",
    "candidate_findings",
)
_MESSAGE_PRIORITY: Final = (
    "context priority ships contracts first; the preference store lands in a "
    "later engineering-memory package"
)
_MESSAGE_REVIEW: Final = (
    "engineering review recording ships contracts first; the attestation "
    "producer lands in a later engineering-memory package"
)


def _as_result(outcome: Any) -> Mapping[str, Any] | AuditedOperationResult:
    if isinstance(outcome, Mapping):
        return outcome
    if isinstance(outcome, AuditedOperationResult):
        return outcome
    # A MutationOutcome from the coordinator: its `.result` is the answer.
    from typing import cast

    return cast(Mapping[str, Any], outcome.result)


def _bounded(value: str, limit: int = PREVIEW_MAX_CODEPOINTS) -> tuple[str, bool]:
    """One preview rendering: bounded text plus its honest truncation flag."""
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _plain(value: Any) -> Any:
    """Decode the contract's immutable containers into JSON-serialisable ones."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


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

    def __init__(
        self,
        service: Any,
        session: AuthenticatedSession | None = None,
        binding: ServiceBinding | None = None,
        clock: Clock | None = None,
        allocate_identifier: IdentifierAllocator = random_identifier,
    ) -> None:
        self.service = service
        self._issued_session = session
        self._issued_binding = binding
        self.clock = SystemClock() if clock is None else clock
        self.allocate_identifier = allocate_identifier

    def _session(self) -> AuthenticatedSession:
        if self._issued_session is None:
            raise OperationError("internal_non_recoverable", _MESSAGE_NO_STORAGE)
        return self._issued_session

    def _binding(self) -> ServiceBinding:
        if self._issued_binding is None:
            raise OperationError("internal_non_recoverable", _MESSAGE_NO_STORAGE)
        return self._issued_binding

    def _connection(self) -> Any:
        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise OperationError("internal_non_recoverable", _MESSAGE_NO_STORAGE)
        return connection

    def _timestamp_us(self, value: str) -> int:
        import datetime as _dt

        parsed = _dt.datetime.fromisoformat(value)
        return int(parsed.timestamp() * 1_000_000)

    def _require_visible_version(
        self,
        fenced: Any,
        *,
        workspace_id: str,
        record_id: str,
        version: str,
    ) -> str | None:
        """The exact record version must exist; returns its claimed repository.

        A priority or a review names an exact visible target: a reference that
        resolves under no governed view is `not_found`, never silently accepted.
        The claimed repository comes from the record's own applicability, so a
        review can be assessed against the registry without trusting the caller.
        """
        now_us = time.time_ns() // 1000
        for governed_view in ("current_canonical", "candidates", "history"):
            for value in read_governed_record_values(
                fenced,
                workspace_id=workspace_id,
                resolution_instant_us=now_us,
                view=governed_view,
            ):
                identity = value.record.provenance.identity
                if identity.record_id == record_id and identity.version == version:
                    content = value.record.content
                    if isinstance(content, Mapping):
                        applicability = content.get("applicability")
                        if isinstance(applicability, Mapping):
                            claimed = applicability.get("repository_id")
                            if isinstance(claimed, str) and claimed:
                                return claimed
                    return None
        raise app_storage.RecordVersionNotFound(record_id)

    def _execute(
        self,
        context: OperationContext,
        connection: Any,
        identity: Any,
        guard: Any,
        equivalence: Any,
        mutate: Any,
        valid_result: Any,
        precondition: Any = None,
    ) -> Any:
        grant = issue_mutation_grant(
            context.authorization,
            session=self._session(),
            binding=self._binding(),
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )
        try:
            return execute_mutation(
                connection,
                identity,
                grant=grant,
                context=context.authorization,
                equivalence=equivalence,
                precondition=precondition,
                mutate=mutate,
                validate_result=valid_result,
                clock=self.clock,
                allocate_identifier=self.allocate_identifier,
            )
        except MutationIdempotencyConflict as error:
            raise OperationError(
                error.code, error.message, retry_class=error.retry_class
            ) from error
        except (
            app_storage.RecordVersionNotFound,
            app_storage.AssessmentPreconditionFailed,
        ) as error:
            code = (
                ERROR_CODE_NOT_FOUND
                if isinstance(error, app_storage.RecordVersionNotFound)
                else ERROR_CODE_MUTATION_PRECONDITION_FAILED
            )
            raise OperationError(
                code,
                _MESSAGE_INVALID if code == ERROR_CODE_NOT_FOUND else _MESSAGE_PRECONDITION,
                retry_class=DEFAULT_RETRY_CLASSIFICATION[code],
            ) from error
        except MutationPreconditionFailed as error:
            raise OperationError(
                ERROR_CODE_MUTATION_PRECONDITION_FAILED,
                _MESSAGE_PRECONDITION,
                retry_class=DEFAULT_RETRY_CLASSIFICATION[
                    ERROR_CODE_MUTATION_PRECONDITION_FAILED
                ],
            ) from error

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
                if record.domain_scope != OBSERVATION_DOMAIN:
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
                        recorded_at_us=value.recorded_at_us,
                        record=dataclasses.replace(
                            record, content=_plain(record.content)
                        ),
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
            preferred = app_storage.preferred_targets(
                connection,
                workspace_id=context.workspace_id,
                principal_id=context.principal,
                now_us=resolved_at_us,
            )
            if preferred:
                ordered = tuple(
                    sorted(
                        ordered,
                        key=lambda record: (
                            0
                            if (
                                record.provenance.identity.record_id,
                                record.provenance.identity.version,
                            )
                            in preferred
                            else 1,
                        ),
                    )
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
                if rendered is None:
                    continue
                if request.repository_target is not None:
                    latest = app_storage.latest_assessment(
                        connection,
                        workspace_id=context.workspace_id,
                        record_id=rendered["record_id"],
                        version=rendered["version"],
                        target_snapshot_id=request.repository_target.snapshot_id,
                    )
                    if latest is not None:
                        # The stored status goes back through the conservative
                        # assessment rather than being replayed, so a legacy
                        # `matched` row is not certified by recency. This is
                        # a read and writes nothing to the history.
                        rendered["applicability"] = (
                            app_storage.assess_against_registered_head(
                                connection,
                                workspace_id=context.workspace_id,
                                claimed_repository_id=rendered.get("repository_id"),
                                target_snapshot_id=request.repository_target.snapshot_id,
                                prior_status=latest["status"],
                            )
                        )
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
        anchor_found = False
        for governed_view in ("current_canonical", "candidates", "history"):
            values = read_governed_record_values(
                connection,
                workspace_id=context.workspace_id,
                resolution_instant_us=now_us,
                view=governed_view,
            )
            for value in values:
                identity = value.record.provenance.identity
                if (
                    identity.record_id == request.anchor.record_id
                    and identity.version == request.anchor.version
                ):
                    anchor_found = True
                    break
            if anchor_found:
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

    # --- context.priority.set ------------------------------------------------------

    def context_priority_set(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        try:
            request = ContextPrioritySetInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection = self._connection()
        from omnivia_core_runtime.ownership.fencing import (
            read_guard as _read_guard,
        )

        guard = _read_guard(connection)
        identity = getattr(self.service, "identity", None)
        assert identity is not None and guard is not None
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )

        self._require_visible_version(
            connection,
            workspace_id=context.workspace_id,
            record_id=request.target.record_id,
            version=request.target.version,
        )

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            app_storage.set_priority(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                principal_id=context.principal,
                target_record_id=request.target.record_id,
                target_version=request.target.version,
                priority=request.priority,
                expires_at_us=(
                    None if request.expires_at is None
                    else self._timestamp_us(request.expires_at)
                ),
                updated_at_us=settlement.settled_at_us,
            )
            result: dict[str, Any] = {
                "target": request.target.to_wire(),
                "priority": request.priority,
                "audit_reference": settlement.audit_ref,
            }
            if request.expires_at is not None:
                result["expires_at"] = request.expires_at
            return result

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                ContextPrioritySetResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        return _as_result(
            self._execute(
                context, connection, identity, guard, equivalence, mutate, valid_result
            )
        )

    # --- engineering.review.record -------------------------------------------------

    def engineering_review_record(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        try:
            request = EngineeringReviewRecordInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection = self._connection()
        from omnivia_core_runtime.ownership.fencing import (
            read_guard as _read_guard,
        )

        guard = _read_guard(connection)
        identity = getattr(self.service, "identity", None)
        assert identity is not None and guard is not None
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )

        claimed_repository = self._require_visible_version(
            connection,
            workspace_id=context.workspace_id,
            record_id=request.record_ref.record_id,
            version=request.record_ref.version,
        )

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            latest = app_storage.latest_assessment(
                fenced,
                workspace_id=context.workspace_id,
                record_id=request.record_ref.record_id,
                version=request.record_ref.version,
                target_snapshot_id=request.target_snapshot.snapshot_id,
            )
            if (
                request.expected_assessment_version is not None
                and (latest is None or latest["assessment_id"] != request.expected_assessment_version)
            ):
                raise app_storage.AssessmentPreconditionFailed(
                    "the target's current assessment is not the version this review expects"
                )
            # §15.5: no qualified dependency validation exists yet, so no review
            # outcome or evidence id can mint `matched` or clear a prior
            # `invalid` / `potentially_stale`. The review is recorded and the
            # assessment stays conservative.
            status = app_storage.assess_against_registered_head(
                fenced,
                workspace_id=context.workspace_id,
                claimed_repository_id=claimed_repository,
                target_snapshot_id=request.target_snapshot.snapshot_id,
                prior_status=None if latest is None else latest["status"],
            )
            app_storage.record_assessment(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                assessment_id=self.allocate_identifier("eas"),
                record_id=request.record_ref.record_id,
                version=request.record_ref.version,
                target_snapshot_id=request.target_snapshot.snapshot_id,
                status=status,
                basis="review",
                assessed_at_us=settlement.settled_at_us,
            )
            app_storage.record_attestation(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                attestation_id=self.allocate_identifier("eat"),
                record_id=request.record_ref.record_id,
                version=request.record_ref.version,
                target_snapshot_id=request.target_snapshot.snapshot_id,
                outcome=request.review_outcome,
                review_evidence_id=request.review_evidence_id,
                recorded_at_us=settlement.settled_at_us,
            )
            return {
                "record_ref": request.record_ref.to_wire(),
                "applicability": status,
                "audit_reference": settlement.audit_ref,
            }

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                EngineeringReviewRecordResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        def precondition(fenced: Any) -> str:
            count = fenced.execute(
                "SELECT COUNT(*) FROM omnivia_engineering_assessments "
                "WHERE workspace_id = ? AND record_id = ? AND version = ? "
                "AND target_snapshot_id = ?",
                (
                    context.workspace_id,
                    request.record_ref.record_id,
                    request.record_ref.version,
                    request.target_snapshot.snapshot_id,
                ),
            ).fetchone()[0]
            return f"assessment-{count}"

        outcome = self._execute(
            context,
            connection,
            identity,
            guard,
            equivalence,
            mutate,
            valid_result,
            precondition=precondition,
        )
        return _as_result(outcome)

    # --- engineering.context.build ------------------------------------------------

    def engineering_context_build(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        """One non-persisted pack from a frozen frontier, under exact budgets.

        The builder never reads a clock and never opens a connection of its own:
        the resolution instant is captured once, the frontier is the same frozen
        authorised candidate set the search path freezes, and the rendering,
        citation ids and applicability statements are all derived from it under
        the effective budget. `pack_id` is the canonical SHA-256 of the result
        with exactly the root `pack_id` and the nested
        `reproducibility.artifact_checksum` removed (§12.3a).
        """
        try:
            request = EngineeringContextBuildInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection = self._connection()
        resolved_at_us = time.time_ns() // 1000

        effective_tokens = (
            BUDGET_DEFAULT_TOKENS
            if request.budget is None or request.budget.model_tokens is None
            else min(request.budget.model_tokens, BUDGET_CEILING_TOKENS)
        )
        effective_bytes = (
            BUDGET_DEFAULT_BYTES
            if request.budget is None or request.budget.model_bytes is None
            else min(request.budget.model_bytes, BUDGET_CEILING_BYTES)
        )

        normalized = " ".join(request.query.lower().split())

        # The authorised frontier: accepted observations matching the query,
        # plus (for the investigate profile, which explicitly requests them)
        # proposed candidates under the candidate_findings partition.
        values = read_governed_record_values(
            connection,
            workspace_id=context.workspace_id,
            resolution_instant_us=resolved_at_us,
            view="current_canonical",
        )
        if request.profile == "investigate":
            values = values + read_governed_record_values(
                connection,
                workspace_id=context.workspace_id,
                resolution_instant_us=resolved_at_us,
                view="candidates",
            )
        accepted_records = []
        for value in values:
            record = value.record
            if record.domain_scope != OBSERVATION_DOMAIN:
                continue
            partition = (
                "accepted_knowledge"
                if value.record.provenance.identity.governance_state == "canonical"
                else "candidate_findings"
            )
            content = record.content
            if not isinstance(content, Mapping):
                continue
            text = " ".join(
                str(content.get(key, "")) for key in ("title", "summary", "what")
            ).lower()
            if normalized and normalized not in text:
                continue
            accepted_records.append(record)

        # Working context (resume profile only, explicitly requested material).
        working: list[dict[str, Any]] = []
        if request.profile == "resume":
            rows = connection.execute(
                "SELECT checkpoint_id, sequence, payload_json FROM "
                "omnivia_engineering_checkpoints WHERE workspace_id = ? "
                "ORDER BY recorded_at_us DESC, sequence DESC LIMIT 5",
                (context.workspace_id,),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row[2])
                except ValueError:
                    continue
                working.append(
                    {
                        "checkpoint_id": row[0],
                        "sequence": row[1],
                        "objective": str(payload.get("objective", "")),
                        "unresolved": payload.get("unresolved_work", []),
                    }
                )

        sections: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        for ordinal, record in enumerate(accepted_records, 1):
            content = record.content if isinstance(record.content, Mapping) else {}
            title = str(content.get("title") or record.provenance.identity.record_id)
            body = str(content.get("summary") or content.get("what") or "")
            citation_id = f"cite-{ordinal}"
            sections.append(
                {
                    "section_id": f"sec-{ordinal}",
                    "kind": "decision_summary",
                    "partition": partition,
                    "content": f"{title}. {body}".strip(),
                    "citation_ids": [citation_id],
                }
            )
            citations.append(
                {
                    "citation_id": citation_id,
                    "record_ref": {
                        "record_id": record.provenance.identity.record_id,
                        "version": record.provenance.identity.version,
                    },
                }
            )
        for ordinal, item in enumerate(working, len(sections) + 1):
            sections.append(
                {
                    "section_id": f"sec-{ordinal}",
                    "kind": "working_context",
                    "partition": "working_context",
                    "content": (
                        f"{item['objective']} Unresolved: "
                        + "; ".join(str(u) for u in item["unresolved"])
                    ).strip(),
                    "citation_ids": [],
                }
            )

        uncertainties = [
            "Target applicability is not evaluated in this build; every applicability statement is `not_evaluated`.",
        ]
        omissions: list[dict[str, Any]] = []

        def render(
            pack_sections: list[dict[str, Any]],
            pack_citations: list[dict[str, Any]],
        ) -> str:
            # The uncertainty notice is mandatory: it is rendered before any
            # optional content and is never dropped to fit a budget (§12.5).
            notice = "[uncertainty] " + uncertainties[0]
            parts = [notice]
            for section in pack_sections:
                label = f"[{section['partition']}]"
                cites = " ".join(f"[{c}]" for c in section["citation_ids"])
                parts.append(f"{label} {section['content']} {cites}".strip())
            return "\n\n".join(parts)

        # Budget reconciliation: drop optional sections lowest-priority first,
        # bounded by the section count; mandatory notices are never dropped.
        while True:
            text = render(sections, citations)
            token_count = len(text.split())
            byte_count = len(text.encode("utf-8"))
            if token_count <= effective_tokens and byte_count <= effective_bytes:
                break
            droppable = [
                index
                for index, section in enumerate(sections)
                if section["partition"] in _SECTION_DROP_ORDER
            ]
            if not droppable or len(sections) <= 1:
                raise OperationError(
                    ERROR_CODE_TOKEN_LIMIT_EXCEEDED,
                    _MESSAGE_BUDGET,
                    retry_class=DEFAULT_RETRY_CLASSIFICATION[
                        ERROR_CODE_TOKEN_LIMIT_EXCEEDED
                    ],
                )
            drop = droppable[-1]
            dropped = sections.pop(drop)
            omissions.append(
                {"field": dropped["section_id"], "reason": "budget"}
            )
            if len(omissions) > len(_SECTION_DROP_ORDER) * 64:
                raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_BUDGET)

        rendering = {
            "text": text,
            "renderer_version": RENDERER_VERSION,
            "token_count": token_count,
            "byte_count": byte_count,
        }
        budget = {
            "requested": (
                None
                if request.budget is None
                else {
                    key: value
                    for key, value in {
                        "model_tokens": request.budget.model_tokens,
                        "model_bytes": request.budget.model_bytes,
                        "hydrations": request.budget.hydrations,
                        "evidence_bytes": request.budget.evidence_bytes,
                    }.items()
                    if value is not None
                }
            ),
            "effective": {"model_tokens": effective_tokens, "model_bytes": effective_bytes},
            "rendered_tokens": token_count,
            "rendered_bytes": byte_count,
            "source_bytes_read": 0,
            "hydrations": 0,
        }
        applicability = [
            {"snapshot": target.to_wire(), "status": "not_evaluated"}
            for target in request.targets
        ]

        pack: dict[str, Any] = {
            "format_version": "engineering_context.v1",
            "normalized_request": {
                "query": request.query,
                "profile": request.profile,
            },
            "targets": [target.to_wire() for target in request.targets],
            "profile": request.profile,
            "sections": sections,
            "citations": citations,
            "conflicts": [],
            "uncertainties": uncertainties,
            "omissions": omissions,
            "rendering": rendering,
            "budget": budget,
            "applicability": applicability,
            "authorization_context": {
                "workspace_id": context.workspace_id,
                "principal_id": context.principal,
            },
            "reproducibility": {
                "builder_version": BUILDER_VERSION,
                "renderer_version": RENDERER_VERSION,
                "artifact_canonicalization": "rfc8785",
                "resolution_instant_us": resolved_at_us,
            },
            "fresh_authorization_required": True,
        }
        canonical = to_canonical_json(pack)
        pack_id = "sha256:" + __import__("hashlib").sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        pack["pack_id"] = pack_id
        pack["reproducibility"]["artifact_checksum"] = pack_id
        return {"pack": pack}
