"""KI-06: a knowledge consumer dependency profile (spec 11.1-11.2).

Extends the existing consumer-dependency model only as far as representing
governed-knowledge references and bounded dynamic selection scopes (spec
11.1): this composes `semantic_registry.consumers`' existing `Consumer`,
`ConsumerDependency` and `ExactBinding` unchanged rather than declaring a
second, competing dependency graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from omnivia_core.governed_knowledge.errors import (
    GovernedKnowledgeErrorCode,
    require,
)
from omnivia_core.semantic_registry.consumers import (
    Consumer,
    ConsumerDependency,
    ExactBinding,
)

KNOWLEDGE_DEPENDENCY_PROFILE_VERSION = "governed-knowledge-consumer-dependency-v1"


def _require_str_tuple(field_name: str, value: tuple[str, ...]) -> None:
    require(
        isinstance(value, tuple),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{field_name} must be a tuple",
    )
    for index, item in enumerate(value):
        require(
            isinstance(item, str) and item.strip() != "",
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"{field_name}[{index}] must be a non-empty string",
        )


class KnowledgeDependencyClass(str, Enum):
    """The dependency classes and their treatment (spec 11.2's table)."""

    EXPLICIT_EXACT_REFERENCE = "explicit_exact_reference"
    EXPLICIT_LOGICAL_REFERENCE = "explicit_logical_reference"
    BOUNDED_DYNAMIC_QUERY = "bounded_dynamic_query"
    OBSERVED_RUNTIME_REFERENCE = "observed_runtime_reference"
    SUPPORTING_EVIDENCE_RELATION = "supporting_evidence_relation"
    PROCEDURE_PROFILE_REFERENCE = "procedure_profile_reference"


@dataclass(frozen=True, slots=True)
class KnowledgeConsumerDependency:
    """One consumer's declared dependency on governed knowledge (spec 11.2).

    `exact_binding` is required exactly when `dependency_class` is
    `EXPLICIT_EXACT_REFERENCE` (spec 15.3's "a supported range alone is
    insufficient runtime evidence" applies here too): a class that only claims
    a logical/bounded/observed/evidentiary relation must not carry one, so a
    caller cannot silently upgrade a weaker dependency class to exact-binding
    evidence it never established. `bounded_scope_refs` is required, and
    required non-empty, exactly for `BOUNDED_DYNAMIC_QUERY` -- the declared
    domain/type/tag/query bound spec 11.2 requires for that class.
    """

    consumer: Consumer
    dependency: ConsumerDependency
    dependency_class: KnowledgeDependencyClass
    exact_binding: ExactBinding | None = None
    bounded_scope_refs: tuple[str, ...] = ()
    profile_version: str = field(
        init=False, default=KNOWLEDGE_DEPENDENCY_PROFILE_VERSION
    )

    def __post_init__(self) -> None:
        require(
            isinstance(self.consumer, Consumer),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "consumer must be a Consumer",
        )
        require(
            isinstance(self.dependency, ConsumerDependency),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "dependency must be a ConsumerDependency",
        )
        require(
            self.consumer.consumer_id == self.dependency.consumer_id,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "dependency.consumer_id must match consumer.consumer_id",
        )
        require(
            isinstance(self.dependency_class, KnowledgeDependencyClass),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "dependency_class must be a KnowledgeDependencyClass",
        )
        if self.dependency_class is KnowledgeDependencyClass.EXPLICIT_EXACT_REFERENCE:
            require(
                isinstance(self.exact_binding, ExactBinding),
                GovernedKnowledgeErrorCode.MISSING_FIELD,
                "explicit_exact_reference requires an exact_binding",
            )
            assert self.exact_binding is not None
            require(
                self.exact_binding.consumer_id == self.consumer.consumer_id,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "exact_binding.consumer_id must match consumer.consumer_id",
            )
        else:
            require(
                self.exact_binding is None,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"{self.dependency_class.value} must not carry an exact_binding",
            )
        if self.dependency_class is KnowledgeDependencyClass.BOUNDED_DYNAMIC_QUERY:
            require(
                len(self.bounded_scope_refs) >= 1,
                GovernedKnowledgeErrorCode.MISSING_FIELD,
                "bounded_dynamic_query requires at least one bounded_scope_ref",
            )
        else:
            require(
                len(self.bounded_scope_refs) == 0,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"{self.dependency_class.value} must not carry bounded_scope_refs",
            )
        _require_str_tuple("bounded_scope_refs", self.bounded_scope_refs)


__all__ = [
    "KNOWLEDGE_DEPENDENCY_PROFILE_VERSION",
    "KnowledgeConsumerDependency",
    "KnowledgeDependencyClass",
]
