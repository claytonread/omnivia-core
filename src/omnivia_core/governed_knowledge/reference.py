"""Reference Stage 1 journey over the accepted application-client call shape.

The protocol is intentionally structural: ``omnivia-core`` does not depend on the client
distribution, while ``omnivia_core_client.ServiceClient`` satisfies this call shape.  The
reference journey therefore demonstrates the supported public-client integration without
creating another transport or reversing package ownership.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    semantics_knowledge,
    to_canonical_json_document,
)
from omnivia_core.governed_knowledge.applicability import FactSnapshot
from omnivia_core.governed_knowledge.assembly import (
    Stage1ContextBundle,
    assemble_stage1_context,
)
from omnivia_core.governed_knowledge.context import TaskContextProfile
from omnivia_core.governed_knowledge.dependency import KnowledgeConsumerDependency
from omnivia_core.governed_knowledge.position import OrganisationalPosition
from omnivia_core.semantic_registry.temporal import TemporalInstant

_CONTEXT_OPERATION = "context_pack.build"
_MUTATION_OPERATIONS = frozenset(
    {"memory.create", "knowledge.propose", "candidate.approve", "record.supersede"}
)


class ApplicationCaller(Protocol):
    """The part of ``ServiceClient`` used by this integration."""

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Any,
        cancellation: Any = None,
    ) -> ResponseEnvelope: ...


class Stage1ReferenceClientError(ValueError):
    """The reference journey received an incompatible request or response."""


@dataclass(frozen=True, slots=True)
class Stage1ReferenceResult:
    response: ResponseEnvelope
    bundle: Stage1ContextBundle | None

    @property
    def succeeded(self) -> bool:
        return self.bundle is not None


@dataclass(frozen=True, slots=True)
class Stage1ReferenceClient:
    """Thin orchestration that preserves the shared application boundary."""

    caller: ApplicationCaller

    def submit_mutation(
        self,
        request: RequestEnvelope,
        *,
        deadline: Any,
        cancellation: Any = None,
    ) -> ResponseEnvelope:
        if request.operation not in _MUTATION_OPERATIONS:
            raise Stage1ReferenceClientError(
                "governed mutation must use an existing accepted write operation"
            )
        return self.caller.call(request, deadline=deadline, cancellation=cancellation)

    def build_context(
        self,
        request: RequestEnvelope,
        *,
        profile: TaskContextProfile,
        dependency: KnowledgeConsumerDependency,
        positions: tuple[OrganisationalPosition, ...],
        facts: FactSnapshot,
        semantic_model_version_ref: str,
        valid_at: TemporalInstant,
        recorded_as_of: TemporalInstant,
        query_time: TemporalInstant,
        deadline: Any,
        cancellation: Any = None,
    ) -> Stage1ReferenceResult:
        if request.operation != _CONTEXT_OPERATION:
            raise Stage1ReferenceClientError(
                "governed context requires the context_pack.build operation"
            )
        if request.metadata.workspace_id != profile.workspace_id:
            raise Stage1ReferenceClientError(
                "request and task profile must name the same workspace"
            )
        if request.metadata.purpose != profile.purpose:
            raise Stage1ReferenceClientError(
                "request and task profile must name the same purpose"
            )
        response = self.caller.call(
            request, deadline=deadline, cancellation=cancellation
        )
        if isinstance(response, ErrorResponseEnvelope):
            return Stage1ReferenceResult(response=response, bundle=None)
        if not isinstance(response, SuccessResponseEnvelope):
            raise Stage1ReferenceClientError(
                "service returned an unsupported response envelope"
            )
        if (
            response.metadata.request_id != request.metadata.request_id
            or response.metadata.correlation_id != request.metadata.correlation_id
        ):
            raise Stage1ReferenceClientError(
                "service response does not correlate with the request"
            )
        pack = semantics_knowledge.verify_context_pack_artifact_document(
            to_canonical_json_document(response.result)
        )
        return Stage1ReferenceResult(
            response=response,
            bundle=assemble_stage1_context(
                profile=profile,
                dependency=dependency,
                context_pack=pack,
                positions=positions,
                facts=facts,
                request_ref=request.metadata.request_id,
                semantic_model_version_ref=semantic_model_version_ref,
                valid_at=valid_at,
                recorded_as_of=recorded_as_of,
                query_time=query_time,
            ),
        )


__all__ = [
    "ApplicationCaller",
    "Stage1ReferenceClient",
    "Stage1ReferenceClientError",
    "Stage1ReferenceResult",
]
