"""Validation helpers for portable knowledge contracts."""

from __future__ import annotations

from typing import Any

from omnivia_memory._shared.validation import (
    ValidationResult,
    scan_sensitive_fields,
    validate_optional_iso_timestamp as _validate_optional_iso,
)
from omnivia_memory.knowledge.models import (
    BUILTIN_GRAPH_RELATIONS,
    BUILTIN_GRAPH_NODE_KINDS,
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
    GraphReviewStatus,
    GraphSensitivity,
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
    normalize_identifier,
    normalize_label,
    normalize_object_id,
    normalize_source_path,
    normalize_space_id,
    normalize_tags,
)

SCRIPT_LIKE_MARKERS = ("<script", "javascript:", "data:text/html", "vbscript:")
MAX_LABEL_LENGTH = 160
MAX_QUOTE_PREVIEW_LENGTH = 280


def check_contract_version_compatibility(
    candidate: ContractVersion,
    current: ContractVersion,
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if candidate.major != current.major:
        errors.append(
            f"unsupported contract major version {candidate}; expected {current.major}.x"
        )
    elif candidate.minor > current.minor:
        warnings.append(
            f"contract minor version {candidate} is newer than validated {current}"
        )
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def summarize_confidence(values: list[GraphConfidence | float | None]) -> dict[str, int]:
    summary = {confidence.value: 0 for confidence in GraphConfidence}
    summary["NUMERIC"] = 0
    for value in values:
        if isinstance(value, GraphConfidence):
            summary[value.value] += 1
        elif isinstance(value, (float, int)):
            summary["NUMERIC"] += 1
    return summary


def summarize_review_status(values: list[GraphReviewStatus]) -> dict[str, int]:
    summary = {status.value: 0 for status in GraphReviewStatus}
    for value in values:
        summary[value.value] += 1
    return summary


def summarize_sensitivity(values: list[GraphSensitivity]) -> dict[str, int]:
    summary = {value.value: 0 for value in GraphSensitivity}
    for item in values:
        summary[item.value] += 1
    return summary


def validate_source_ref(source_ref: SourceRef) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("source_id", source_ref.source_id, errors)
    if source_ref.path is not None:
        _validate_source_path("path", source_ref.path, errors)
    if source_ref.quote_preview is not None:
        if len(source_ref.quote_preview) > MAX_QUOTE_PREVIEW_LENGTH:
            errors.append("quote_preview exceeds max length")
        _validate_public_text("quote_preview", source_ref.quote_preview, errors)
    _validate_confidence("confidence", source_ref.confidence, errors)
    _validate_public_safe_payload(source_ref.metadata, errors, "metadata")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_knowledge_source(source: KnowledgeSource) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", source.id, errors, normalize_object_id)
    _validate_identifier("space_id", source.space_id, errors, normalize_space_id)
    _validate_label("title", source.title, errors)
    if source.relative_path is not None:
        _validate_source_path("relative_path", source.relative_path, errors)
    _validate_optional_iso("created_at", source.created_at, errors)
    _validate_optional_iso("updated_at", source.updated_at, errors)
    _validate_public_safe_payload(source.metadata, errors, "metadata")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_knowledge_object(obj: KnowledgeObject) -> ValidationResult:
    return _validate_knowledge_object(obj, allowed_reserved_extensions=set())


def _validate_knowledge_object(
    obj: KnowledgeObject,
    *,
    allowed_reserved_extensions: set[str],
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", obj.id, errors, normalize_object_id)
    _validate_identifier("space_id", obj.space_id, errors, normalize_space_id)
    _validate_extension(
        "kind",
        obj.kind,
        BUILTIN_OBJECT_KINDS,
        errors,
        allowed_reserved_extensions=allowed_reserved_extensions,
    )
    _validate_label("title", obj.title, errors)
    if obj.summary:
        _validate_public_text("summary", obj.summary, errors)
    _validate_tags(obj.tags, errors)
    for index, source_ref in enumerate(obj.source_refs):
        _append_prefixed(validate_source_ref(source_ref), f"source_refs[{index}]", errors, warnings)
    _validate_confidence("confidence", obj.confidence, errors)
    _validate_optional_iso("created_at", obj.created_at, errors)
    _validate_optional_iso("updated_at", obj.updated_at, errors)
    _validate_public_safe_payload(obj.metadata, errors, "metadata")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_knowledge_collection(collection: KnowledgeCollection) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", collection.id, errors, normalize_object_id)
    _validate_identifier("space_id", collection.space_id, errors, normalize_space_id)
    _validate_label("title", collection.title, errors)
    if not collection.member_ids:
        warnings.append("collection has no member_ids")
    for index, member_id in enumerate(collection.member_ids):
        _validate_identifier(f"member_ids[{index}]", member_id, errors, normalize_object_id)
    _validate_tags(collection.tags, errors)
    for index, source_ref in enumerate(collection.source_refs):
        _append_prefixed(validate_source_ref(source_ref), f"source_refs[{index}]", errors, warnings)
    _validate_public_safe_payload(collection.metadata, errors, "metadata")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_knowledge_link(link: KnowledgeLink) -> ValidationResult:
    return _validate_knowledge_link(link, allowed_reserved_extensions=set())


def _validate_knowledge_link(
    link: KnowledgeLink,
    *,
    allowed_reserved_extensions: set[str],
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", link.id, errors, normalize_graph_edge_id)
    _validate_identifier("space_id", link.space_id, errors, normalize_space_id)
    _validate_identifier("source_object_id", link.source_object_id, errors, normalize_object_id)
    _validate_identifier("target_object_id", link.target_object_id, errors, normalize_object_id)
    _validate_extension(
        "relation",
        link.relation,
        BUILTIN_GRAPH_RELATIONS,
        errors,
        allowed_reserved_extensions=allowed_reserved_extensions,
    )
    _validate_confidence("confidence", link.confidence, errors)
    _validate_decision_provenance(
        "knowledge_link",
        link.influences_decision,
        link.source_refs,
        link.missing_evidence,
        errors,
    )
    if link.evidence_strength is GraphEvidenceStrength.MISSING and not link.missing_evidence:
        errors.append("knowledge_link missing evidence_strength marker must set missing_evidence")
    for index, source_ref in enumerate(link.source_refs):
        _append_prefixed(validate_source_ref(source_ref), f"source_refs[{index}]", errors, warnings)
    _validate_public_safe_payload(link.metadata, errors, "metadata")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_knowledge_claim(claim: KnowledgeClaim) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", claim.id, errors, normalize_graph_edge_id)
    _validate_identifier("space_id", claim.space_id, errors, normalize_space_id)
    _validate_identifier("subject_object_id", claim.subject_object_id, errors, normalize_object_id)
    _validate_label("predicate", claim.predicate, errors)
    if (claim.object_object_id is None) == (claim.object_value is None):
        errors.append("exactly one of object_object_id or object_value is required")
    if claim.object_object_id is not None:
        _validate_identifier("object_object_id", claim.object_object_id, errors, normalize_object_id)
    if claim.object_value is not None:
        _validate_public_text("object_value", claim.object_value, errors)
    _validate_confidence("confidence", claim.confidence, errors)
    _validate_decision_provenance(
        "knowledge_claim",
        claim.influences_decision,
        claim.source_refs,
        claim.missing_evidence,
        errors,
    )
    if claim.evidence_strength is GraphEvidenceStrength.MISSING and not claim.missing_evidence:
        errors.append("knowledge_claim missing evidence_strength marker must set missing_evidence")
    for index, source_ref in enumerate(claim.source_refs):
        _append_prefixed(validate_source_ref(source_ref), f"source_refs[{index}]", errors, warnings)
    _validate_public_safe_payload(claim.metadata, errors, "metadata")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_graph_node(node: GraphNode) -> ValidationResult:
    return _validate_graph_node(node, allowed_reserved_extensions=set())


def _validate_graph_node(
    node: GraphNode,
    *,
    allowed_reserved_extensions: set[str],
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", node.id, errors, normalize_graph_node_id)
    _validate_identifier("space_id", node.space_id, errors, normalize_space_id)
    _validate_label("label", node.label, errors)
    _validate_extension(
        "kind",
        node.kind,
        BUILTIN_GRAPH_NODE_KINDS,
        errors,
        allowed_reserved_extensions=allowed_reserved_extensions,
    )
    if node.object_id is not None:
        _validate_identifier("object_id", node.object_id, errors, normalize_object_id)
    _validate_confidence("confidence", node.confidence, errors)
    for index, source_ref in enumerate(node.source_refs):
        _append_prefixed(validate_source_ref(source_ref), f"source_refs[{index}]", errors, warnings)
    _validate_public_safe_payload(node.metadata, errors, "metadata")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_graph_edge(edge: GraphEdge) -> ValidationResult:
    return _validate_graph_edge(edge, allowed_reserved_extensions=set())


def _validate_graph_edge(
    edge: GraphEdge,
    *,
    allowed_reserved_extensions: set[str],
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", edge.id, errors, normalize_graph_edge_id)
    _validate_identifier("space_id", edge.space_id, errors, normalize_space_id)
    _validate_identifier("source", edge.source, errors, normalize_graph_node_id)
    _validate_identifier("target", edge.target, errors, normalize_graph_node_id)
    _validate_extension(
        "relation",
        edge.relation,
        BUILTIN_GRAPH_RELATIONS,
        errors,
        allowed_reserved_extensions=allowed_reserved_extensions,
    )
    _validate_confidence("confidence", edge.confidence, errors)
    _validate_decision_provenance(
        "graph_edge",
        edge.influences_decision,
        edge.source_refs,
        edge.missing_evidence,
        errors,
    )
    if edge.evidence_strength is GraphEvidenceStrength.MISSING and not edge.missing_evidence:
        errors.append("graph_edge missing evidence_strength marker must set missing_evidence")
    for index, source_ref in enumerate(edge.source_refs):
        _append_prefixed(validate_source_ref(source_ref), f"source_refs[{index}]", errors, warnings)
    _validate_public_safe_payload(edge.metadata, errors, "metadata")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_graph_fragment(fragment: GraphFragment) -> ValidationResult:
    return _validate_graph_fragment(fragment, allowed_reserved_extensions=set())


def _validate_graph_fragment(
    fragment: GraphFragment,
    *,
    allowed_reserved_extensions: set[str],
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", fragment.id, errors, normalize_object_id)
    _validate_identifier("space_id", fragment.space_id, errors, normalize_space_id)
    _append_result(
        check_contract_version_compatibility(fragment.contract_version, GRAPH_CONTRACT_VERSION),
        errors,
        warnings,
    )
    seen_node_ids: set[str] = set()
    for index, node in enumerate(fragment.nodes):
        _append_prefixed(
            _validate_graph_node(node, allowed_reserved_extensions=allowed_reserved_extensions),
            f"nodes[{index}]",
            errors,
            warnings,
        )
        if node.space_id != fragment.space_id:
            errors.append(f"nodes[{index}] belongs to a different space")
        if node.id in seen_node_ids:
            errors.append(f"duplicate graph node id: {node.id}")
        seen_node_ids.add(node.id)

    seen_edge_ids: set[str] = set()
    for index, edge in enumerate(fragment.edges):
        _append_prefixed(
            _validate_graph_edge(edge, allowed_reserved_extensions=allowed_reserved_extensions),
            f"edges[{index}]",
            errors,
            warnings,
        )
        if edge.space_id != fragment.space_id:
            errors.append(f"edges[{index}] belongs to a different space")
        if edge.id in seen_edge_ids:
            errors.append(f"duplicate graph edge id: {edge.id}")
        seen_edge_ids.add(edge.id)
        if edge.source not in seen_node_ids or edge.target not in seen_node_ids:
            errors.append(f"edges[{index}] has dangling node references")

    for index, source_ref in enumerate(fragment.source_refs):
        _append_prefixed(validate_source_ref(source_ref), f"source_refs[{index}]", errors, warnings)
    _validate_public_safe_payload(fragment.metadata, errors, "metadata")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_knowledge_extension_manifest(
    manifest: KnowledgeExtensionManifest,
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", manifest.id, errors, normalize_object_id)
    _append_result(
        check_contract_version_compatibility(
            manifest.contract_version,
            EXTENSION_MANIFEST_CONTRACT_VERSION,
        ),
        errors,
        warnings,
    )
    _validate_identifier("namespace", manifest.namespace, errors, normalize_identifier)
    _validate_label("title", manifest.title, errors)
    _validate_label("version", manifest.version, errors)
    for index, value in enumerate(manifest.object_kinds):
        _validate_extension_value_for_manifest(
            f"object_kinds[{index}]",
            value,
            manifest,
            BUILTIN_OBJECT_KINDS,
            errors,
        )
    for index, value in enumerate(manifest.node_kinds):
        _validate_extension_value_for_manifest(
            f"node_kinds[{index}]",
            value,
            manifest,
            BUILTIN_GRAPH_NODE_KINDS,
            errors,
        )
    for index, value in enumerate(manifest.relations):
        _validate_extension_value_for_manifest(
            f"relations[{index}]",
            value,
            manifest,
            BUILTIN_GRAPH_RELATIONS,
            errors,
        )
    _validate_public_safe_payload(manifest.metadata, errors, "metadata")
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_agent_graph_context(context: AgentGraphContext) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", context.id, errors, normalize_object_id)
    _validate_identifier("space_id", context.space_id, errors, normalize_space_id)
    _validate_label("summary", context.summary, errors)
    _validate_decision_provenance(
        "agent_graph_context",
        True,
        context.source_refs,
        context.missing_evidence,
        errors,
    )
    _validate_public_safe_payload(context.confidence_summary, errors, "confidence_summary")
    _validate_public_safe_payload(context.review_summary, errors, "review_summary")
    _validate_public_safe_payload(context.sensitivity_summary, errors, "sensitivity_summary")
    _validate_public_safe_payload(context.metadata, errors, "metadata")
    for index, source_ref in enumerate(context.source_refs):
        _append_prefixed(validate_source_ref(source_ref), f"source_refs[{index}]", errors, warnings)
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_knowledge_space(space: KnowledgeSpace) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _validate_identifier("id", space.id, errors, normalize_space_id)
    _validate_label("title", space.title, errors)
    _validate_label("space_type", space.space_type, errors)
    _append_result(
        check_contract_version_compatibility(space.contract_version, KNOWLEDGE_CONTRACT_VERSION),
        errors,
        warnings,
    )
    if space.summary:
        _validate_public_text("summary", space.summary, errors)
    _validate_tags(space.tags, errors)
    _validate_public_safe_payload(space.metadata, errors, "metadata")

    allowed_reserved_extensions = _collect_allowed_reserved_extensions(space)

    source_ids: set[str] = set()
    for index, source in enumerate(space.sources):
        _append_prefixed(validate_knowledge_source(source), f"sources[{index}]", errors, warnings)
        if source.id in source_ids:
            errors.append(f"duplicate source id: {source.id}")
        source_ids.add(source.id)
        if source.space_id != space.id:
            errors.append(f"sources[{index}] belongs to a different space")

    object_ids: set[str] = set()
    for index, obj in enumerate(space.objects):
        _append_prefixed(
            _validate_knowledge_object(
                obj,
                allowed_reserved_extensions=allowed_reserved_extensions,
            ),
            f"objects[{index}]",
            errors,
            warnings,
        )
        if obj.id in object_ids:
            errors.append(f"duplicate object id: {obj.id}")
        object_ids.add(obj.id)
        if obj.space_id != space.id:
            errors.append(f"objects[{index}] belongs to a different space")
        for ref_index, ref in enumerate(obj.source_refs):
            if ref.source_id not in source_ids:
                errors.append(f"objects[{index}].source_refs[{ref_index}] references unknown source")

    collection_ids: set[str] = set()
    for index, collection in enumerate(space.collections):
        _append_prefixed(
            validate_knowledge_collection(collection),
            f"collections[{index}]",
            errors,
            warnings,
        )
        if collection.id in collection_ids:
            errors.append(f"duplicate collection id: {collection.id}")
        collection_ids.add(collection.id)
        if collection.space_id != space.id:
            errors.append(f"collections[{index}] belongs to a different space")
        for member_id in collection.member_ids:
            if member_id not in object_ids:
                errors.append(f"collections[{index}] references unknown member id: {member_id}")

    link_ids: set[str] = set()
    for index, link in enumerate(space.links):
        _append_prefixed(
            _validate_knowledge_link(
                link,
                allowed_reserved_extensions=allowed_reserved_extensions,
            ),
            f"links[{index}]",
            errors,
            warnings,
        )
        if link.id in link_ids:
            errors.append(f"duplicate link id: {link.id}")
        link_ids.add(link.id)
        if link.space_id != space.id:
            errors.append(f"links[{index}] belongs to a different space")
        if link.source_object_id not in object_ids or link.target_object_id not in object_ids:
            errors.append(f"links[{index}] references unknown object ids")
        for ref_index, ref in enumerate(link.source_refs):
            if ref.source_id not in source_ids:
                errors.append(f"links[{index}].source_refs[{ref_index}] references unknown source")

    claim_ids: set[str] = set()
    for index, claim in enumerate(space.claims):
        _append_prefixed(validate_knowledge_claim(claim), f"claims[{index}]", errors, warnings)
        if claim.id in claim_ids:
            errors.append(f"duplicate claim id: {claim.id}")
        claim_ids.add(claim.id)
        if claim.space_id != space.id:
            errors.append(f"claims[{index}] belongs to a different space")
        if claim.subject_object_id not in object_ids:
            errors.append(f"claims[{index}] references unknown subject object")
        if claim.object_object_id is not None and claim.object_object_id not in object_ids:
            errors.append(f"claims[{index}] references unknown object object")
        for ref_index, ref in enumerate(claim.source_refs):
            if ref.source_id not in source_ids:
                errors.append(f"claims[{index}].source_refs[{ref_index}] references unknown source")

    for index, fragment in enumerate(space.graph_fragments):
        _append_prefixed(
            _validate_graph_fragment(
                fragment,
                allowed_reserved_extensions=allowed_reserved_extensions,
            ),
            f"graph_fragments[{index}]",
            errors,
            warnings,
        )
        if fragment.space_id != space.id:
            errors.append(f"graph_fragments[{index}] belongs to a different space")
        for node_index, node in enumerate(fragment.nodes):
            if node.object_id is not None and node.object_id not in object_ids:
                errors.append(
                    f"graph_fragments[{index}].nodes[{node_index}] references unknown object"
                )
            for ref_index, ref in enumerate(node.source_refs):
                if ref.source_id not in source_ids:
                    errors.append(
                        f"graph_fragments[{index}].nodes[{node_index}].source_refs[{ref_index}] references unknown source"
                    )
        for edge_index, edge in enumerate(fragment.edges):
            for ref_index, ref in enumerate(edge.source_refs):
                if ref.source_id not in source_ids:
                    errors.append(
                        f"graph_fragments[{index}].edges[{edge_index}].source_refs[{ref_index}] references unknown source"
                    )

    for index, manifest in enumerate(space.extension_manifests):
        _append_prefixed(
            validate_knowledge_extension_manifest(manifest),
            f"extension_manifests[{index}]",
            errors,
            warnings,
        )

    for index, context in enumerate(space.agent_contexts):
        _append_prefixed(
            validate_agent_graph_context(context),
            f"agent_contexts[{index}]",
            errors,
            warnings,
        )
        if context.space_id != space.id:
            errors.append(f"agent_contexts[{index}] belongs to a different space")
        for object_id in context.object_ids:
            if object_id not in object_ids:
                errors.append(f"agent_contexts[{index}] references unknown object id: {object_id}")
        for link_id in context.link_ids:
            if link_id not in link_ids:
                errors.append(f"agent_contexts[{index}] references unknown link id: {link_id}")
        for claim_id in context.claim_ids:
            if claim_id not in claim_ids:
                errors.append(f"agent_contexts[{index}] references unknown claim id: {claim_id}")
        for ref_index, ref in enumerate(context.source_refs):
            if ref.source_id not in source_ids:
                errors.append(
                    f"agent_contexts[{index}].source_refs[{ref_index}] references unknown source"
                )

    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def _append_result(
    result: ValidationResult,
    errors: list[str],
    warnings: list[str],
) -> None:
    errors.extend(result.errors)
    warnings.extend(result.warnings)


def _append_prefixed(
    result: ValidationResult,
    prefix: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    errors.extend(f"{prefix}.{error}" for error in result.errors)
    warnings.extend(f"{prefix}.{warning}" for warning in result.warnings)


def _validate_identifier(
    field_name: str,
    value: str,
    errors: list[str],
    normalizer: Any = normalize_identifier,
) -> None:
    try:
        normalized = normalizer(value)
    except ValueError as exc:
        errors.append(f"{field_name} {exc}")
        return
    if normalized != value:
        errors.append(f"{field_name} must already be normalized")


def _validate_label(field_name: str, value: str, errors: list[str]) -> None:
    try:
        normalized = normalize_label(value)
    except ValueError as exc:
        errors.append(f"{field_name} {exc}")
        return
    if len(normalized) > MAX_LABEL_LENGTH:
        errors.append(f"{field_name} exceeds max length")


def _validate_public_text(field_name: str, value: str, errors: list[str]) -> None:
    _validate_label(field_name, value, errors)
    lowered = value.lower()
    if any(marker in lowered for marker in SCRIPT_LIKE_MARKERS):
        errors.append(f"{field_name} contains script-like content")


def _validate_tags(tags: list[str], errors: list[str]) -> None:
    try:
        normalized = normalize_tags(tags)
    except ValueError as exc:
        errors.append(f"tags {exc}")
        return
    if normalized != tags:
        errors.append("tags must already be normalized and deduplicated")


def _validate_source_path(field_name: str, value: str, errors: list[str]) -> None:
    try:
        normalized = normalize_source_path(value)
    except ValueError as exc:
        errors.append(f"{field_name} {exc}")
        return
    if normalized != value:
        errors.append(f"{field_name} must already be normalized")


def _validate_confidence(
    field_name: str,
    value: GraphConfidence | float | None,
    errors: list[str],
) -> None:
    if value is None:
        return
    if isinstance(value, (float, int)):
        if not 0 <= value <= 1:
            errors.append(f"{field_name} must be between 0.0 and 1.0")
        return
    if value not in GraphConfidence:
        errors.append(f"{field_name} must be a known confidence value or numeric score")


def _validate_decision_provenance(
    field_name: str,
    influences_decision: bool,
    source_refs: list[SourceRef],
    missing_evidence: bool,
    errors: list[str],
) -> None:
    if influences_decision and not source_refs and not missing_evidence:
        errors.append(
            f"{field_name} must include source_refs or set missing_evidence when it influences decisions"
        )


def _validate_extension(
    field_name: str,
    value: str,
    builtins: frozenset[str],
    errors: list[str],
    *,
    allowed_reserved_extensions: set[str],
) -> None:
    try:
        normalized = normalize_extension_value(value, builtins=builtins)
    except ValueError as exc:
        errors.append(f"{field_name} {exc}")
        return
    if normalized != value:
        errors.append(f"{field_name} must already be normalized")
    if value.startswith("omnivia:") and value not in allowed_reserved_extensions:
        errors.append(f"{field_name} must not use reserved omnivia namespace outside official manifests")


def _validate_extension_value_for_manifest(
    field_name: str,
    value: str,
    manifest: KnowledgeExtensionManifest,
    builtins: frozenset[str],
    errors: list[str],
) -> None:
    try:
        normalized = normalize_extension_value(value, builtins=builtins)
    except ValueError as exc:
        errors.append(f"{field_name} {exc}")
        return
    if normalized != value:
        errors.append(f"{field_name} must already be normalized")
    if value in builtins:
        errors.append(f"{field_name} must declare namespaced extension values, not built-ins")
        return
    namespace = value.split(":", maxsplit=1)[0]
    if namespace == "omnivia" and not manifest.official:
        errors.append(f"{field_name} collides with reserved omnivia namespace")
    elif namespace != manifest.namespace:
        errors.append(f"{field_name} must use the manifest namespace {manifest.namespace}")


def _validate_public_safe_payload(
    data: dict[str, Any] | list[Any] | Any,
    errors: list[str],
    prefix: str,
) -> None:
    scan_sensitive_fields(data, errors, prefix)
    _validate_public_safe_strings(data, errors, prefix)


def _validate_public_safe_strings(
    data: dict[str, Any] | list[Any] | Any,
    errors: list[str],
    prefix: str,
) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            key_prefix = f"{prefix}.{key}" if prefix else key
            _validate_public_safe_strings(value, errors, key_prefix)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            _validate_public_safe_strings(value, errors, f"{prefix}[{index}]")
    elif isinstance(data, str):
        if len(data) > MAX_QUOTE_PREVIEW_LENGTH and prefix.endswith(("label", "quote_preview", "summary")):
            errors.append(f"{prefix} exceeds max length")
        lowered = data.lower()
        if any(marker in lowered for marker in SCRIPT_LIKE_MARKERS):
            errors.append(f"{prefix} contains script-like content")


def _collect_allowed_reserved_extensions(space: KnowledgeSpace) -> set[str]:
    allowed: set[str] = set()
    for manifest in space.extension_manifests:
        if manifest.official and manifest.namespace == "omnivia":
            allowed.update(manifest.object_kinds)
            allowed.update(manifest.node_kinds)
            allowed.update(manifest.relations)
    return allowed
