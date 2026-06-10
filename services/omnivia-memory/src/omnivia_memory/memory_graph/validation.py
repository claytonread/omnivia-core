"""Validation helpers for memory graph contracts."""

from __future__ import annotations

from omnivia_memory._shared.validation import (
    ValidationResult,
    scan_sensitive_fields as _validate_no_sensitive_fields,
    validate_iso_timestamp as _validate_iso,
    validate_optional_iso_timestamp as _validate_optional_iso,
)
from omnivia_memory.memory_graph.models import (
    Confidence,
    EvidenceGraphResponse,
    GraphPreviewEdge,
    GraphPreviewNode,
    GraphPreviewResponse,
    MemoryEntity,
    MemoryFact,
    MemorySegment,
    MemorySource,
    SourceRef,
)

CONFIDENCE_BUCKETS = frozenset({"extracted", "inferred", "ambiguous"})


def validate_memory_source(source: MemorySource) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _require("id", source.id, errors)
    _require("workspace_id", source.workspace_id, errors)
    _require("uri", source.uri, errors)
    _require("title", source.title, errors)
    _validate_iso("created_at", source.created_at, errors)
    _validate_iso("updated_at", source.updated_at, errors)
    _validate_no_sensitive_fields(source.to_dict(), errors)
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_memory_segment(segment: MemorySegment) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _require("id", segment.id, errors)
    _require("source_id", segment.source_id, errors)
    _require("workspace_id", segment.workspace_id, errors)
    _require("label", segment.label, errors)
    _require("parser", segment.parser, errors)
    _require("parser_settings_ref", segment.parser_settings_ref, errors)
    _validate_iso("created_at", segment.created_at, errors)
    _validate_no_sensitive_fields(segment.to_dict(), errors)
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_memory_entity(entity: MemoryEntity) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _require("id", entity.id, errors)
    _require("workspace_id", entity.workspace_id, errors)
    _require("type", entity.type, errors)
    _require("canonical_name", entity.canonical_name, errors)
    _validate_confidence("confidence", entity.confidence, errors)
    for index, source_ref in enumerate(entity.source_refs):
        _validate_source_ref(source_ref, f"source_refs[{index}]", errors)
    if not entity.source_refs:
        warnings.append("entity has no source_refs")
    _validate_iso("created_at", entity.created_at, errors)
    _validate_iso("updated_at", entity.updated_at, errors)
    _validate_no_sensitive_fields(entity.to_dict(), errors)
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_memory_fact(fact: MemoryFact) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    _require("id", fact.id, errors)
    _require("workspace_id", fact.workspace_id, errors)
    _require("subject_id", fact.subject_id, errors)
    _require("predicate", fact.predicate, errors)
    if (fact.object_id is None) == (fact.object_value is None):
        errors.append("exactly one of object_id or object_value is required")
    _validate_confidence("confidence", fact.confidence, errors)
    for index, source_ref in enumerate(fact.source_refs):
        _validate_source_ref(source_ref, f"source_refs[{index}]", errors)
    if not fact.source_refs:
        warnings.append("fact has no source_refs; UI must show missing-evidence warning")
    _validate_optional_iso("valid_from", fact.valid_from, errors)
    _validate_optional_iso("valid_to", fact.valid_to, errors)
    _validate_iso("created_at", fact.created_at, errors)
    _validate_iso("updated_at", fact.updated_at, errors)
    _validate_no_sensitive_fields(fact.to_dict(), errors)
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_graph_preview_response(response: GraphPreviewResponse) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = list(response.warnings)
    _require("workspace_id", response.workspace_id, errors)
    _require("query", response.query, errors)
    _validate_iso("generated_at", response.generated_at, errors)
    for index, node in enumerate(response.nodes):
        _validate_preview_node(node, f"nodes[{index}]", errors, warnings)
    for index, edge in enumerate(response.edges):
        _validate_preview_edge(edge, f"edges[{index}]", errors, warnings)
    _validate_no_sensitive_fields(response.to_dict(), errors)
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_evidence_graph_response(response: EvidenceGraphResponse) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = list(response.warnings)
    _require("workspace_id", response.workspace_id, errors)
    _require("query", response.query, errors)
    _validate_iso("generated_at", response.generated_at, errors)
    for index, node in enumerate(response.nodes):
        _validate_preview_node(node, f"nodes[{index}]", errors, warnings)
    for index, edge in enumerate(response.edges):
        _validate_preview_edge(edge, f"edges[{index}]", errors, warnings)
    for index, citation in enumerate(response.citations):
        _validate_source_ref(citation, f"citations[{index}]", errors)
    if not response.citations:
        warnings.append("evidence graph has no citations")
    _validate_no_sensitive_fields(response.to_dict(), errors)
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def _require(field_name: str, value: object, errors: list[str]) -> None:
    if value is None or value == "":
        errors.append(f"{field_name} is required")


def _validate_confidence(field_name: str, value: Confidence | None, errors: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, float | int):
        if not 0 <= value <= 1:
            errors.append(f"{field_name} must be between 0.0 and 1.0")
        return
    if isinstance(value, str) and value in CONFIDENCE_BUCKETS:
        return
    errors.append(f"{field_name} must be a score or confidence bucket")


def _validate_source_ref(source_ref: SourceRef, prefix: str, errors: list[str]) -> None:
    _require(f"{prefix}.source_id", source_ref.source_id, errors)
    _validate_confidence(f"{prefix}.confidence", source_ref.confidence, errors)
    _validate_no_sensitive_fields(source_ref.to_dict(), errors, prefix)


def _validate_preview_node(
    node: GraphPreviewNode,
    prefix: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    _require(f"{prefix}.id", node.id, errors)
    _require(f"{prefix}.label", node.label, errors)
    _require(f"{prefix}.type", node.type, errors)
    _validate_confidence(f"{prefix}.confidence", node.confidence, errors)
    if not node.source_refs and node.state.value != "missing_evidence":
        warnings.append(f"{prefix} has no source_refs")


def _validate_preview_edge(
    edge: GraphPreviewEdge,
    prefix: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    _require(f"{prefix}.id", edge.id, errors)
    _require(f"{prefix}.source", edge.source, errors)
    _require(f"{prefix}.target", edge.target, errors)
    _require(f"{prefix}.label", edge.label, errors)
    _require(f"{prefix}.type", edge.type, errors)
    _validate_confidence(f"{prefix}.confidence", edge.confidence, errors)
    _validate_optional_iso(f"{prefix}.valid_from", edge.valid_from, errors)
    _validate_optional_iso(f"{prefix}.valid_to", edge.valid_to, errors)
    if not edge.source_refs and edge.state.value != "missing_evidence":
        warnings.append(f"{prefix} has no source_refs")
