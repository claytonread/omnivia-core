"""Strict-mypy consumer fixture for the Knowledge compatibility facade.

``omnivia-memory`` ships ``py.typed``, so a downstream package running
``mypy --strict`` type-checks against these legacy import paths. This module is
that consumer for the three Knowledge leaves: it imports all 57 routed symbols
from ``omnivia_memory.knowledge.models``, ``.normalize`` and ``.validation``
(never from ``omnivia_core``) and proves the facades re-export usefully typed
objects -- the exact canonical types, not ``Any`` -- via ``typing.assert_type``.

The Knowledge domain gets its own fixture rather than joining
``accepted_legacy_facade_consumer.py`` because it is the domain the rest of the
migration reaches *into*: ``ContractVersion``,
``check_contract_version_compatibility`` and the three ``BUILTIN_*`` bounded
vocabularies are consumed by the run-ledger and control-plane facades, so the
static shape of these exports is worth pinning on its own terms. The consumer
partition is enforced by ``tests/test_typed_facade_consumers.py``.

It exists to be checked, not run: it is a mypy target in the acceptance
workflow's ``Run strict mypy`` step (see
``tests/test_core_acceptance_workflow.py``). If a facade ever stopped explicitly
re-exporting these names, or degraded them to ``Any``, strict mypy would fail
here.
"""

from typing import Any, assert_type

from omnivia_memory.knowledge.models import (
    BUILTIN_GRAPH_NODE_KINDS,
    BUILTIN_GRAPH_RELATIONS,
    BUILTIN_OBJECT_KINDS,
    EXTENSION_MANIFEST_CONTRACT_VERSION,
    GRAPH_CONTRACT_VERSION,
    KNOWLEDGE_CONTRACT_VERSION,
    AgentGraphContext,
    ContractVersion,
    GraphConfidence,
    GraphEdge,
    GraphEvidenceStrength,
    GraphFragment,
    GraphNode,
    GraphOrigin,
    GraphReviewStatus,
    GraphSensitivity,
    GraphSourceType,
    GraphVisibility,
    KnowledgeClaim,
    KnowledgeCollection,
    KnowledgeExtensionManifest,
    KnowledgeLink,
    KnowledgeObject,
    KnowledgeSource,
    KnowledgeSpace,
    SourceRef,
)
from omnivia_memory.knowledge.normalize import (
    normalize_extension_value,
    normalize_graph_edge_id,
    normalize_graph_node_id,
    normalize_graph_node_kind,
    normalize_graph_relation,
    normalize_identifier,
    normalize_label,
    normalize_object_id,
    normalize_object_kind,
    normalize_source_path,
    normalize_space_id,
    normalize_tags,
)
from omnivia_memory.knowledge.validation import (
    MAX_LABEL_LENGTH,
    MAX_QUOTE_PREVIEW_LENGTH,
    SCRIPT_LIKE_MARKERS,
    check_contract_version_compatibility,
    summarize_confidence,
    summarize_review_status,
    summarize_sensitivity,
    validate_agent_graph_context,
    validate_graph_edge,
    validate_graph_fragment,
    validate_graph_node,
    validate_knowledge_claim,
    validate_knowledge_collection,
    validate_knowledge_extension_manifest,
    validate_knowledge_link,
    validate_knowledge_object,
    validate_knowledge_source,
    validate_knowledge_space,
    validate_source_ref,
)


def build_source_ref() -> SourceRef:
    """Construct evidence through the facade's own types."""
    ref = SourceRef(
        source_id="source-1",
        source_type=GraphSourceType.DOCUMENT,
        path="notes/one.md",
        quote_preview="a"[:MAX_QUOTE_PREVIEW_LENGTH],
        confidence=GraphConfidence.EXTRACTED,
    )
    assert_type(ref.source_type, GraphSourceType)
    assert_type(ref.path, str | None)
    assert_type(ref.missing_evidence, bool)
    assert_type(ref.metadata, dict[str, Any])
    return ref


def build_space() -> KnowledgeSpace:
    """The models facade composes a whole space out of its own exports."""
    ref = build_source_ref()
    source = KnowledgeSource(
        id="source-1",
        space_id="space-1",
        source_type=GraphSourceType.FILE,
        title="Notes",
        relative_path="notes/one.md",
        origin=GraphOrigin.MANUAL,
    )
    obj = KnowledgeObject(
        id="object-1",
        space_id="space-1",
        kind="concept",
        title="Concept",
        source_refs=[ref],
        confidence=GraphConfidence.INFERRED,
        review_status=GraphReviewStatus.REVIEWED,
        visibility=GraphVisibility.TEAM,
        sensitivity=GraphSensitivity.INTERNAL,
    )
    other = KnowledgeObject(
        id="object-2",
        space_id="space-1",
        kind="decision",
        title="Decision",
        source_refs=[ref],
    )
    collection = KnowledgeCollection(
        id="collection-1",
        space_id="space-1",
        title="Both",
        member_ids=[obj.id, other.id],
        source_refs=[ref],
    )
    link = KnowledgeLink(
        id="link-1",
        space_id="space-1",
        source_object_id=obj.id,
        target_object_id=other.id,
        relation="related_to",
        source_refs=[ref],
        evidence_strength=GraphEvidenceStrength.PRIMARY,
    )
    claim = KnowledgeClaim(
        id="claim-1",
        space_id="space-1",
        subject_object_id=obj.id,
        predicate="supports_claim",
        object_object_id=other.id,
        source_refs=[ref],
    )
    node = GraphNode(
        id="node-1",
        space_id="space-1",
        label="Concept",
        kind="concept",
        object_id=obj.id,
        source_refs=[ref],
    )
    target = GraphNode(
        id="node-2",
        space_id="space-1",
        label="Decision",
        kind="decision",
        object_id=other.id,
    )
    edge = GraphEdge(
        id="edge-1",
        space_id="space-1",
        source=node.id,
        target=target.id,
        relation="related_to",
        source_refs=[ref],
    )
    fragment = GraphFragment(
        id="fragment-1",
        space_id="space-1",
        contract_version=GRAPH_CONTRACT_VERSION,
        nodes=[node, target],
        edges=[edge],
        origin=GraphOrigin.OMNIVIA,
    )
    manifest = KnowledgeExtensionManifest(
        id="manifest-1",
        contract_version=EXTENSION_MANIFEST_CONTRACT_VERSION,
        namespace="acme",
        title="Acme",
        version="1.0.0",
        object_kinds=["acme:widget"],
        node_kinds=["acme:widget"],
        relations=["acme:powers"],
    )
    context = AgentGraphContext(
        id="context-1",
        space_id="space-1",
        summary="One concept, one decision.",
        object_ids=[obj.id, other.id],
        link_ids=[link.id],
        claim_ids=[claim.id],
        source_refs=[ref],
    )
    return KnowledgeSpace(
        id="space-1",
        title="Space",
        space_type="workspace",
        contract_version=KNOWLEDGE_CONTRACT_VERSION,
        tags=normalize_tags([" Alpha ", "alpha", "Beta"]),
        sources=[source],
        objects=[obj, other],
        collections=[collection],
        links=[link],
        claims=[claim],
        graph_fragments=[fragment],
        extension_manifests=[manifest],
        agent_contexts=[context],
    )


def normalize_identifiers() -> list[str]:
    """Every normalizer keeps its precise ``str``/``list[str]`` typing."""
    space_id = normalize_space_id("Space One")
    object_id = normalize_object_id("Object One")
    node_id = normalize_graph_node_id("Node One")
    edge_id = normalize_graph_edge_id("Edge One")
    identifier = normalize_identifier("Some Identifier")
    label = normalize_label("  A   label  ")
    path = normalize_source_path("./notes/one.md")
    object_kind = normalize_object_kind("Concept")
    node_kind = normalize_graph_node_kind("Concept")
    relation = normalize_graph_relation("Related To")

    assert_type(space_id, str)
    assert_type(normalize_tags([" Alpha "]), list[str])

    # ``MAX_LABEL_LENGTH`` is the validators' own bound. It stays a precise
    # ``int`` through the facade, so it composes in arithmetic and comparison
    # without a cast.
    assert_type(MAX_LABEL_LENGTH, int)
    assert_type(MAX_QUOTE_PREVIEW_LENGTH, int)
    assert len(label) <= MAX_LABEL_LENGTH

    # The three bounded vocabularies reach this leaf through its sibling
    # ``models``, and each is a ``frozenset[str]`` -- the exact parameter type
    # ``normalize_extension_value`` requires as a keyword-only argument.
    assert_type(BUILTIN_OBJECT_KINDS, frozenset[str])
    assert_type(BUILTIN_GRAPH_NODE_KINDS, frozenset[str])
    assert_type(BUILTIN_GRAPH_RELATIONS, frozenset[str])
    extension_kind = normalize_extension_value(
        "Acme:Widget", builtins=BUILTIN_OBJECT_KINDS
    )
    extension_node_kind = normalize_extension_value(
        "Acme:Widget", builtins=BUILTIN_GRAPH_NODE_KINDS
    )
    extension_relation = normalize_extension_value(
        "Acme:Powers", builtins=BUILTIN_GRAPH_RELATIONS
    )
    assert_type(extension_kind, str)

    return [
        space_id,
        object_id,
        node_id,
        edge_id,
        identifier,
        label,
        path,
        object_kind,
        node_kind,
        relation,
        extension_kind,
        extension_node_kind,
        extension_relation,
    ]


def validate_space() -> list[str]:
    """Every validator returns the shared ``ValidationResult`` primitive.

    ``ValidationResult`` is deliberately *not* imported here: this leaf never
    owned one and it is not a routed symbol, so what has to hold statically is
    that the returned object keeps the shared primitive's precise
    ``valid``/``errors``/``warnings`` typing without the consumer naming the
    class.
    """
    space = build_space()
    results = [
        validate_knowledge_space(space),
        *(validate_knowledge_source(source) for source in space.sources),
        *(validate_knowledge_object(obj) for obj in space.objects),
        *(
            validate_knowledge_collection(collection)
            for collection in space.collections
        ),
        *(validate_knowledge_link(link) for link in space.links),
        *(validate_knowledge_claim(claim) for claim in space.claims),
        *(
            validate_knowledge_extension_manifest(manifest)
            for manifest in space.extension_manifests
        ),
        *(
            validate_graph_fragment(fragment)
            for fragment in space.graph_fragments
        ),
        *(
            validate_graph_node(node)
            for fragment in space.graph_fragments
            for node in fragment.nodes
        ),
        *(
            validate_graph_edge(edge)
            for fragment in space.graph_fragments
            for edge in fragment.edges
        ),
        *(
            validate_agent_graph_context(context)
            for context in space.agent_contexts
        ),
        *(
            validate_source_ref(ref)
            for obj in space.objects
            for ref in obj.source_refs
        ),
    ]

    first = results[0]
    assert_type(first.valid, bool)
    assert_type(first.errors, list[str])
    assert_type(first.warnings, list[str])

    messages: list[str] = []
    for result in results:
        if not result.valid:
            messages.extend(result.errors)
        messages.extend(result.warnings)
    return messages


def summarize_space() -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """The three summarizers keep their precise ``dict[str, int]`` return type."""
    space = build_space()
    confidences: list[GraphConfidence | float | None] = [
        obj.confidence for obj in space.objects
    ]
    confidence = summarize_confidence(confidences)
    review = summarize_review_status([obj.review_status for obj in space.objects])
    sensitivity = summarize_sensitivity(
        [obj.sensitivity for obj in space.objects]
    )

    assert_type(confidence, dict[str, int])
    assert_type(review, dict[str, int])
    assert_type(sensitivity, dict[str, int])
    return confidence, review, sensitivity


def check_versions() -> bool:
    """``ContractVersion`` and its compatibility check stay precisely typed.

    This is the cross-domain hop the run-ledger and control-plane facades depend
    on, so the exact ``ContractVersion`` type -- not ``Any`` -- has to survive
    the Knowledge models facade.
    """
    assert_type(KNOWLEDGE_CONTRACT_VERSION, ContractVersion)
    assert_type(GRAPH_CONTRACT_VERSION, ContractVersion)
    assert_type(EXTENSION_MANIFEST_CONTRACT_VERSION, ContractVersion)
    assert_type(KNOWLEDGE_CONTRACT_VERSION.major, int)
    assert_type(KNOWLEDGE_CONTRACT_VERSION.minor, int)

    candidate = ContractVersion(
        major=KNOWLEDGE_CONTRACT_VERSION.major,
        minor=KNOWLEDGE_CONTRACT_VERSION.minor + 1,
    )
    result = check_contract_version_compatibility(
        candidate, KNOWLEDGE_CONTRACT_VERSION
    )
    assert_type(result.valid, bool)
    assert_type(result.warnings, list[str])
    return result.valid


def scan_for_script_markers(text: str) -> list[str]:
    """``SCRIPT_LIKE_MARKERS`` stays a precise tuple of ``str``.

    The annotated assignment is the assertion: it proves every element is a
    ``str`` (not ``Any``) without pinning the tuple's arity, which is frozen
    contract data rather than a typing property.
    """
    markers: tuple[str, ...] = SCRIPT_LIKE_MARKERS
    return [marker for marker in markers if marker in text.lower()]


def roundtrip() -> str:
    """The three facade paths compose with each other, not just individually."""
    space = build_space()
    payload = space.to_dict()
    assert_type(payload, dict[str, Any])

    messages = validate_space()
    confidence, review, sensitivity = summarize_space()
    identifiers = normalize_identifiers()
    markers = scan_for_script_markers("<script>alert(1)</script>")

    return ":".join(
        [
            str(len(payload)),
            str(len(messages)),
            str(sum(confidence.values())),
            str(sum(review.values())),
            str(sum(sensitivity.values())),
            *identifiers,
            *markers,
            str(check_versions()),
        ]
    )
