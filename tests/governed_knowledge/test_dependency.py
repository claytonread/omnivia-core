"""Tests for KI-06 `KnowledgeConsumerDependency` (spec 11.2).

Compatibility with the existing `Consumer`/`ConsumerDependency`/`ExactBinding`
concepts is the point: this profile composes them unchanged.
"""

from __future__ import annotations

import pytest

from omnivia_core.governed_knowledge.dependency import (
    KnowledgeConsumerDependency,
    KnowledgeDependencyClass,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeValidationError
from omnivia_core.governed_knowledge.profile_content import (
    dependency_from_content,
    dependency_to_content,
)
from omnivia_core.semantic_registry.consumers import (
    Consumer,
    ConsumerDependency,
    ConsumerKind,
    DependencyMode,
    ExactBinding,
)


def _consumer() -> Consumer:
    return Consumer(
        consumer_id="consumer-1",
        kind=ConsumerKind.APP,
        owner="team-a",
        deployment_identity="deployment-1",
    )


def _dependency() -> ConsumerDependency:
    return ConsumerDependency(
        consumer_id="consumer-1",
        model_id="model-1",
        element_id="position-1",
        dependency_mode=DependencyMode.REQUIRED,
        usage_location="renewal_brief.step_2",
    )


def test_explicit_exact_reference_requires_exact_binding() -> None:
    binding = ExactBinding(consumer_id="consumer-1", model_version_id="model-v1")
    dependency = KnowledgeConsumerDependency(
        consumer=_consumer(),
        dependency=_dependency(),
        dependency_class=KnowledgeDependencyClass.EXPLICIT_EXACT_REFERENCE,
        exact_binding=binding,
    )
    assert dependency.exact_binding is binding
    assert dependency_from_content(dependency_to_content(dependency)) == dependency


def test_explicit_exact_reference_without_binding_is_rejected() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        KnowledgeConsumerDependency(
            consumer=_consumer(),
            dependency=_dependency(),
            dependency_class=KnowledgeDependencyClass.EXPLICIT_EXACT_REFERENCE,
        )


def test_non_exact_class_must_not_carry_exact_binding() -> None:
    binding = ExactBinding(consumer_id="consumer-1", model_version_id="model-v1")
    with pytest.raises(GovernedKnowledgeValidationError):
        KnowledgeConsumerDependency(
            consumer=_consumer(),
            dependency=_dependency(),
            dependency_class=KnowledgeDependencyClass.EXPLICIT_LOGICAL_REFERENCE,
            exact_binding=binding,
        )


def test_bounded_dynamic_query_requires_scope_refs() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        KnowledgeConsumerDependency(
            consumer=_consumer(),
            dependency=_dependency(),
            dependency_class=KnowledgeDependencyClass.BOUNDED_DYNAMIC_QUERY,
        )
    dependency = KnowledgeConsumerDependency(
        consumer=_consumer(),
        dependency=_dependency(),
        dependency_class=KnowledgeDependencyClass.BOUNDED_DYNAMIC_QUERY,
        bounded_scope_refs=("domain:commercial",),
    )
    assert dependency.bounded_scope_refs == ("domain:commercial",)


def test_observed_runtime_reference_carries_neither() -> None:
    dependency = KnowledgeConsumerDependency(
        consumer=_consumer(),
        dependency=_dependency(),
        dependency_class=KnowledgeDependencyClass.OBSERVED_RUNTIME_REFERENCE,
    )
    assert dependency.exact_binding is None
    assert dependency.bounded_scope_refs == ()


def test_consumer_and_dependency_consumer_id_must_match() -> None:
    mismatched = ConsumerDependency(
        consumer_id="consumer-2",
        model_id="model-1",
        element_id="position-1",
        dependency_mode=DependencyMode.OPTIONAL,
        usage_location="somewhere",
    )
    with pytest.raises(GovernedKnowledgeValidationError):
        KnowledgeConsumerDependency(
            consumer=_consumer(),
            dependency=mismatched,
            dependency_class=KnowledgeDependencyClass.OBSERVED_RUNTIME_REFERENCE,
        )
