"""Tests for the A2.3 provider-neutral application-contract slice: `evidence.search`,
`knowledge.search`, `knowledge.propose`, `candidate.approve`, `candidate.reject`,
`record.supersede`, `graph.traverse`, and `context_pack.build` (ADR-039).

Covers the eight operation input/result DTO pairs (schema and tolerant-decoder round
trips, additive-unknown-field tolerance vs. strict schema rejection), and the semantic
hardening in :mod:`omnivia_core.contracts.v1.semantics_evidence` and
:mod:`omnivia_core.contracts.v1.semantics_knowledge`: L0 evidence integrity and
tombstone/leakage rules, `knowledge.search` default-canonical-view leak prevention,
reciprocal governance-transition validation shared by `knowledge.propose` /
`candidate.approve` / `candidate.reject` / `record.supersede`, graph traversal
projection integrity, and Context Pack reproducibility/citation/budget invariants.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import inspect
import json
import re
import string
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts import v1
from omnivia_core.contracts.v1 import generated
from omnivia_core.contracts.v1 import semantics_evidence as sem_evidence
from omnivia_core.contracts.v1 import semantics_knowledge as sem_knowledge
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    CandidateApproveInput,
    CandidateApproveResult,
    CandidateExtractionMetadata,
    CandidateRejectInput,
    CandidateRejectResult,
    ContextPackAuthorizationContext,
    ContextPackAuthorizedCandidateSetManifest,
    ContextPackAuthorizedEvidenceCandidate,
    ContextPackAuthorizedRecordCandidate,
    ContextPackBudget,
    ContextPackBuildInput,
    ContextPackBuildResult,
    ContextPackConflict,
    ContextPackEvidenceCitation,
    ContextPackEvidenceReference,
    ContextPackNormalizedRequest,
    ContextPackRecordCitation,
    ContextPackReproducibility,
    ContextPackSection,
    ContextPackUncertainty,
    ContractDecodeError,
    EvidenceArtifact,
    EvidenceReference,
    EvidenceSearchInput,
    EvidenceSearchResult,
    GovernanceRationale,
    GrantedAuthority,
    GraphEdge,
    GraphNode,
    GraphTraversalInput,
    GraphTraversalResult,
    KnowledgeProposeInput,
    KnowledgeProposeResult,
    KnowledgeSearchInput,
    KnowledgeSearchResult,
    MemoryCreateInput,
    MutationPrecondition,
    PageMetadata,
    ProjectionFreshness,
    RecordSupersedeInput,
    RecordSupersedeResult,
    SourceSpan,
)
from omnivia_core.contracts.v1.semantics import MAX_PAGE_LIMIT

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
BASE_URI = "https://contracts.omnivia.dev/application/v1/"

_SCHEMA_NAMES = (
    "common",
    "compatibility",
    "errors",
    "envelopes",
    "service",
    "records",
    "jobs",
    "operations",
    "workspace",
    "memory",
    "evidence",
    "knowledge",
    "graph",
    "context-pack",
    "compatibility-matrix",
)


def _registry() -> Registry:
    entries: list[tuple[str, Resource[Any]]] = []
    for name in _SCHEMA_NAMES:
        document = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        resource = Resource.from_contents(document)
        resource_id = resource.id()
        assert resource_id is not None
        entries.append((resource_id, resource))
    return Registry().with_resources(entries)


REGISTRY = _registry()

_DEF_SOURCE: dict[str, str] = {
    "EvidenceArtifact": "evidence",
    "EvidenceSearchInput": "evidence",
    "EvidenceSearchResult": "evidence",
    "KnowledgeSearchInput": "knowledge",
    "KnowledgeSearchResult": "knowledge",
    "KnowledgeProposeInput": "knowledge",
    "KnowledgeProposeResult": "knowledge",
    "CandidateApproveInput": "knowledge",
    "CandidateApproveResult": "knowledge",
    "CandidateRejectInput": "knowledge",
    "CandidateRejectResult": "knowledge",
    "RecordSupersedeInput": "knowledge",
    "RecordSupersedeResult": "knowledge",
    "GraphTraversalInput": "graph",
    "GraphTraversalResult": "graph",
    "GraphNode": "graph",
    "GraphEdge": "graph",
    "ContextPackBuildInput": "context-pack",
    "ContextPackBuildResult": "context-pack",
    "ContextPackCitation": "context-pack",
    "ContextPackEvidenceCitation": "context-pack",
    "ContextPackEvidenceReference": "context-pack",
    "ContextPackRecordCitation": "context-pack",
    "ContextPackSection": "context-pack",
    "ContextPackConflict": "context-pack",
    "ContextPackUncertainty": "context-pack",
    "ContextPackBudget": "context-pack",
    "ContextPackAuthorizationContext": "context-pack",
    "ContextPackAuthorizedCandidate": "context-pack",
    "ContextPackAuthorizedCandidateSetManifest": "context-pack",
    "ContextPackAuthorizedEvidenceCandidate": "context-pack",
    "ContextPackAuthorizedRecordCandidate": "context-pack",
    "ContextPackNormalizedRequest": "context-pack",
    "ContextPackReproducibility": "context-pack",
}


def _strict_validator(def_name: str) -> Draft202012Validator:
    source = _DEF_SOURCE[def_name]
    return Draft202012Validator(
        {"$ref": f"{BASE_URI}{source}.schema.json#/$defs/{def_name}"},
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _is_schema_valid(def_name: str, document: Any) -> bool:
    return not list(_strict_validator(def_name).iter_errors(document))


# --------------------------------------------------------------------------
# Wire-document builders
# --------------------------------------------------------------------------

T0 = "2024-01-01T00:00:00Z"
T1 = "2024-01-02T00:00:00Z"
T2 = "2024-01-03T00:00:00Z"
"""Three ordered instants. Context Pack cases resolve at `T1`, so `T0` is safely in the
past and `T2` is the future a pack must never select from."""


def _source_wire(
    *,
    kind: str = "document",
    source_id: str = "doc-1",
    locator: str | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {"kind": kind, "source_id": source_id}
    if locator is not None:
        document["locator"] = locator
    if retrieved_at is not None:
        document["retrieved_at"] = retrieved_at
    return document


def _identity_wire(
    *,
    record_id: str = "rec-1",
    version: str = "v1",
    layer: str = "l2",
    governance_state: str = "accepted",
    currentness: str = "current",
    supersedes: dict[str, Any] | None = None,
    superseded_by: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "record_id": record_id,
        "version": version,
        "layer": layer,
        "governance_state": governance_state,
        "currentness": currentness,
    }
    if supersedes is not None:
        document["supersedes"] = supersedes
    if superseded_by is not None:
        document["superseded_by"] = superseded_by
    return document


def _temporal_wire(
    *,
    ingested_at: str = T0,
    recorded_at: str = T0,
    observed_at: str | None = None,
    superseded_at: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {"ingested_at": ingested_at, "recorded_at": recorded_at}
    if observed_at is not None:
        document["observed_at"] = observed_at
    if superseded_at is not None:
        document["superseded_at"] = superseded_at
    if valid_from is not None:
        document["valid_from"] = valid_from
    if valid_until is not None:
        document["valid_until"] = valid_until
    return document


def _history_entry_wire(
    *,
    actor_id: str = "actor-1",
    actor_kind: str = "user",
    action: str = "created",
    occurred_at: str = T0,
    evidence: list[dict[str, Any]] | None = None,
    reason_code: str | None = None,
    reason_comment: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "action": action,
        "occurred_at": occurred_at,
    }
    if evidence is not None:
        document["evidence"] = evidence
    if reason_code is not None:
        document["reason_code"] = reason_code
    if reason_comment is not None:
        document["reason_comment"] = reason_comment
    return document


def _evidence_reference_wire(
    *,
    source: dict[str, Any] | None = None,
    span: dict[str, Any] | None = None,
    excerpt: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {"source": source if source is not None else _source_wire()}
    if span is not None:
        document["span"] = span
    if excerpt is not None:
        document["excerpt"] = excerpt
    return document


def _assertion_wire(
    *,
    actor_id: str = "asserter-1",
    actor_kind: str = "user",
    actor_role: str = "author",
    asserted_at: str = T0,
    evidence: list[dict[str, Any]] | None = None,
    proposed_valid_from: str | None = None,
    proposed_valid_until: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "actor_role": actor_role,
        "asserted_at": asserted_at,
        "evidence": evidence if evidence is not None else [_evidence_reference_wire()],
    }
    if proposed_valid_from is not None:
        document["proposed_valid_from"] = proposed_valid_from
    if proposed_valid_until is not None:
        document["proposed_valid_until"] = proposed_valid_until
    return document


def _extraction_wire(
    *,
    extractor_id: str = "extractor-1",
    extracted_at: str = T0,
    extractor_version: str | None = None,
    confidence: float | None = None,
    reconciliation_state: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {"extractor_id": extractor_id, "extracted_at": extracted_at}
    if extractor_version is not None:
        document["extractor_version"] = extractor_version
    if confidence is not None:
        document["confidence"] = confidence
    if reconciliation_state is not None:
        document["reconciliation_state"] = reconciliation_state
    return document


def _provenance_wire(
    *,
    identity: dict[str, Any] | None = None,
    temporal: dict[str, Any] | None = None,
    evidence_disposition: str = "available",
    sources: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
    assertion: dict[str, Any] | None = None,
    extraction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "identity": identity if identity is not None else _identity_wire(),
        "temporal": temporal if temporal is not None else _temporal_wire(),
        "history": history if history is not None else [],
        "evidence_disposition": evidence_disposition,
        "sources": sources if sources is not None else [_source_wire()],
    }
    if assertion is not None:
        document["assertion"] = assertion
    if extraction is not None:
        document["extraction"] = extraction
    return document


def _governed_record_wire(
    *,
    workspace_id: str = "ws-1",
    record_type: str = "memory.fact",
    domain_scope: str = "personal.preferences",
    authority_level: str = "accepted",
    reviewer: str | None = "reviewer-1",
    provenance: dict[str, Any] | None = None,
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "workspace_id": workspace_id,
        "record_type": record_type,
        "domain_scope": domain_scope,
        "authority_level": authority_level,
        "provenance": provenance if provenance is not None else _provenance_wire(),
        "content": content if content is not None else {"fact": "hello"},
    }
    if reviewer is not None:
        document["reviewer"] = reviewer
    return document


def _accepted_record_wire(
    *, record_id: str = "rec-1", version: str = "v1", workspace_id: str = "ws-1"
) -> dict[str, Any]:
    identity = _identity_wire(record_id=record_id, version=version, layer="l2", governance_state="accepted", currentness="current")
    return _governed_record_wire(
        workspace_id=workspace_id, authority_level="accepted", reviewer="reviewer-1", provenance=_provenance_wire(identity=identity)
    )


def _canonical_knowledge_record_wire(
    *,
    record_id: str = "rec-1",
    version: str = "v1",
    workspace_id: str = "ws-1",
    record_type: str = "memory.fact",
    domain_scope: str = "personal.preferences",
    authority_level: str = "canonical",
    reviewer: str | None = "reviewer-1",
    valid_from: str | None = None,
    valid_until: str | None = None,
    history: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A record satisfying `knowledge.search`'s default/`current_canonical`-view exactness:
    `l2`/`accepted`/`current`, `authority_level` exactly `canonical`, a reviewer.

    `authority_level` is a parameter rather than a constant so a case can isolate the
    authority bar from the layer/state/currentness one; it defaults to the value the
    canonical view actually requires.
    """
    identity = _identity_wire(record_id=record_id, version=version, layer="l2", governance_state="accepted", currentness="current")
    temporal = _temporal_wire(valid_from=valid_from, valid_until=valid_until)
    return _governed_record_wire(
        workspace_id=workspace_id,
        record_type=record_type,
        domain_scope=domain_scope,
        authority_level=authority_level,
        reviewer=reviewer,
        provenance=_provenance_wire(
            identity=identity, temporal=temporal, history=history, sources=sources
        ),
    )


def _candidate_knowledge_record_wire(
    *, record_id: str = "rec-1", version: str = "v1", workspace_id: str = "ws-1"
) -> dict[str, Any]:
    """A record satisfying `knowledge.search`'s `candidates`-view exactness:
    `l1`/`candidate`/`current`, `authority_level` exactly `proposed`, no reviewer."""
    identity = _identity_wire(record_id=record_id, version=version, layer="l1", governance_state="candidate", currentness="current")
    return _governed_record_wire(
        workspace_id=workspace_id,
        authority_level="proposed",
        reviewer=None,
        provenance=_provenance_wire(identity=identity),
    )


def _history_knowledge_record_wire(
    *,
    record_id: str = "rec-1",
    version: str = "v1",
    workspace_id: str = "ws-1",
    record_type: str = "memory.fact",
    domain_scope: str = "personal.preferences",
    authority_level: str = "canonical",
    reviewer: str | None = "reviewer-1",
    superseded_at: str = T0,
    history: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A record satisfying the shared `history`-view exactness both `knowledge.search` and
    `graph.traverse` apply: `l2`/`accepted`/`superseded`, `canonical` authority, a reviewer,
    and superseded no later than the canonical-resolution time -- a governed version that
    *was* canonical knowledge and had already been replaced by then.

    `superseded_at` defaults to `T0`, the resolution instant every history case here reads
    at, rather than to a later one: a version replaced only *after* the read resolved was
    still the canonical answer at that instant, so it is not history yet, and defaulting to
    that would make every positive history case assert the opposite of what it means.
    """
    identity = _identity_wire(
        record_id=record_id,
        version=version,
        layer="l2",
        governance_state="accepted",
        currentness="superseded",
        superseded_by={"record_id": record_id, "version": "v-next"},
    )
    return _governed_record_wire(
        workspace_id=workspace_id,
        record_type=record_type,
        domain_scope=domain_scope,
        authority_level=authority_level,
        reviewer=reviewer,
        provenance=_provenance_wire(
            identity=identity,
            temporal=_temporal_wire(superseded_at=superseded_at),
            history=history,
            sources=sources,
        ),
    )


def _knowledge_search_input_wire(
    *,
    query: str = "hello",
    order: str | None = None,
    view: str | None = None,
    record_type: str | None = None,
    domain_scope: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {"query": query}
    if order is not None:
        document["order"] = order
    if view is not None:
        document["view"] = view
    if record_type is not None:
        document["record_type"] = record_type
    if domain_scope is not None:
        document["domain_scope"] = domain_scope
    if limit is not None:
        document["limit"] = limit
    return document


def _record_version_reference_wire(*, record_id: str = "rec-1", version: str = "v1") -> dict[str, Any]:
    return {"record_id": record_id, "version": version}


def _rationale_wire(*, reason_code: str = "meets_bar", comment: str | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {"reason_code": reason_code}
    if comment is not None:
        document["comment"] = comment
    return document


_DELETE = object()
"""Sentinel for :func:`_with`: remove the addressed key instead of setting it."""


def _with(document: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Deep-copy `document` and set (or, with :data:`_DELETE`, remove) a dotted `path`.

    Governance-transition adversarial cases are almost always "the canonical pair, with
    exactly one thing wrong", so mutating a deep copy keeps each case's intent visible as
    the single line that differs rather than as a re-spelled builder call.
    """
    clone = copy.deepcopy(document)
    target: Any = clone
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    if value is _DELETE:
        target.pop(parts[-1], None)
    else:
        target[parts[-1]] = value
    return clone


def _transition_event_wire(
    *,
    action: str,
    actor_id: str = "reviewer-1",
    actor_kind: str = "user",
    occurred_at: str = T1,
    reason_code: str | None = "meets_bar",
    reason_comment: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The single audit event a governance transition appends to the record's history."""
    document: dict[str, Any] = {
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "action": action,
        "occurred_at": occurred_at,
    }
    if reason_code is not None:
        document["reason_code"] = reason_code
    if reason_comment is not None:
        document["reason_comment"] = reason_comment
    if evidence is not None:
        document["evidence"] = evidence
    return document


def _replacement_wire(
    *,
    record_type: str = "memory.fact",
    domain_scope: str = "personal.preferences",
    content: dict[str, Any] | None = None,
    evidence_disposition: str = "available",
    sources: list[dict[str, Any]] | None = None,
    assertion: dict[str, Any] | None = None,
    extraction: dict[str, Any] | None = None,
    event_at: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """A `record.supersede` replacement claim: a whole `MemoryCreateInput`.

    Deliberately drawn from a *different* source (`doc-2`) than the canonical prior record's
    (`doc-1`), so the default positive case already proves a replacement is bound to its own
    sources rather than to a union with the superseded version's.
    """
    if sources is None:
        sources = [_source_wire(source_id="doc-2")]
    if assertion is None:
        assertion = _assertion_wire(
            evidence=[_evidence_reference_wire(source=_source_wire(source_id="doc-2"))]
        )
    document: dict[str, Any] = {
        "record_type": record_type,
        "domain_scope": domain_scope,
        "content": content if content is not None else {"fact": "superseded"},
        "evidence_disposition": evidence_disposition,
        "sources": sources,
        "assertion": assertion,
    }
    if extraction is not None:
        document["extraction"] = extraction
    if event_at is not None:
        document["event_at"] = event_at
    if observed_at is not None:
        document["observed_at"] = observed_at
    return document


REPLACEMENT = _replacement_wire()
"""The canonical replacement claim every `record.supersede` case starts from."""


def _transition_pair_wire(
    *,
    action: str,
    prev_layer: str,
    prev_state: str,
    prev_authority: str,
    upd_layer: str,
    upd_state: str,
    upd_authority: str,
    prev_reviewer: str | None = None,
    upd_reviewer: str | None = None,
    record_id: str = "rec-1",
    prev_version: str = "v1",
    upd_version: str = "v2",
    workspace_id: str = "ws-1",
    reason_code: str = "meets_bar",
    reason_comment: str | None = None,
    actor_id: str = "reviewer-1",
    actor_kind: str = "user",
    transition_at: str = T1,
    prev_recorded_at: str = T0,
    prev_history: list[dict[str, Any]] | None = None,
    upd_history: list[dict[str, Any]] | None = None,
    event_evidence: list[dict[str, Any]] | None = None,
    record_type: str = "memory.fact",
    upd_record_type: str | None = None,
    domain_scope: str = "personal.preferences",
    upd_domain_scope: str | None = None,
    content: dict[str, Any] | None = None,
    upd_content: dict[str, Any] | None = None,
    evidence_disposition: str = "available",
    upd_evidence_disposition: str | None = None,
    sources: list[dict[str, Any]] | None = None,
    upd_sources: list[dict[str, Any]] | None = None,
    assertion: dict[str, Any] | None = None,
    upd_assertion: dict[str, Any] | None = None,
    extraction: dict[str, Any] | None = None,
    upd_extraction: dict[str, Any] | None = None,
    prev_temporal: dict[str, Any] | None = None,
    upd_temporal: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A canonical previous/updated governed-record wire pair for one governance transition.

    Everything a passing transition needs holds by default: reciprocal supersession pointers
    carrying the rationale's exact reason code, one shared transition instant across
    `previous.superseded_at` / `updated.recorded_at` / the appended event's `occurred_at`,
    and `updated.history` equal to `previous.history` plus exactly one audit event naming
    `action`. Every `upd_*` override defaults to its `prev` counterpart, so the claim is
    preserved unless a case deliberately moves it.
    """
    if content is None:
        content = {"fact": "hello"}
    if sources is None:
        sources = [_source_wire()]
    if assertion is None:
        assertion = _assertion_wire()
    if prev_history is None:
        prev_history = []
    if upd_history is None:
        upd_history = [
            *prev_history,
            _transition_event_wire(
                action=action,
                actor_id=actor_id,
                actor_kind=actor_kind,
                occurred_at=transition_at,
                reason_code=reason_code,
                reason_comment=reason_comment,
                evidence=event_evidence,
            ),
        ]

    prev_identity = _identity_wire(
        record_id=record_id,
        version=prev_version,
        layer=prev_layer,
        governance_state=prev_state,
        currentness="superseded",
        superseded_by={"record_id": record_id, "version": upd_version, "reason": reason_code},
    )
    upd_identity = _identity_wire(
        record_id=record_id,
        version=upd_version,
        layer=upd_layer,
        governance_state=upd_state,
        currentness="current",
        supersedes={"record_id": record_id, "version": prev_version, "reason": reason_code},
    )
    prev_record = _governed_record_wire(
        workspace_id=workspace_id,
        record_type=record_type,
        domain_scope=domain_scope,
        authority_level=prev_authority,
        reviewer=prev_reviewer,
        content=content,
        provenance=_provenance_wire(
            identity=prev_identity,
            temporal=(
                prev_temporal
                if prev_temporal is not None
                else _temporal_wire(recorded_at=prev_recorded_at, superseded_at=transition_at)
            ),
            evidence_disposition=evidence_disposition,
            history=prev_history,
            sources=sources,
            assertion=assertion,
            extraction=extraction,
        ),
    )
    upd_record = _governed_record_wire(
        workspace_id=workspace_id,
        record_type=upd_record_type if upd_record_type is not None else record_type,
        domain_scope=upd_domain_scope if upd_domain_scope is not None else domain_scope,
        authority_level=upd_authority,
        reviewer=upd_reviewer,
        content=upd_content if upd_content is not None else content,
        provenance=_provenance_wire(
            identity=upd_identity,
            temporal=(
                upd_temporal
                if upd_temporal is not None
                else _temporal_wire(recorded_at=transition_at)
            ),
            evidence_disposition=(
                upd_evidence_disposition
                if upd_evidence_disposition is not None
                else evidence_disposition
            ),
            history=upd_history,
            sources=upd_sources if upd_sources is not None else sources,
            assertion=upd_assertion if upd_assertion is not None else assertion,
            extraction=upd_extraction if upd_extraction is not None else extraction,
        ),
    )
    return prev_record, upd_record


def _propose_pair_wire(**overrides: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    defaults: dict[str, Any] = {
        "action": "knowledge.propose",
        "actor_id": "actor-1",
        "prev_layer": "l1", "prev_state": "proposed", "prev_authority": "proposed",
        "upd_layer": "l1", "upd_state": "candidate", "upd_authority": "proposed",
    }
    defaults.update(overrides)
    return _transition_pair_wire(**defaults)


def _approve_pair_wire(**overrides: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    defaults: dict[str, Any] = {
        "action": "candidate.approve",
        "prev_layer": "l1", "prev_state": "candidate", "prev_authority": "proposed",
        "upd_layer": "l2", "upd_state": "accepted", "upd_authority": "canonical",
        "upd_reviewer": "reviewer-1",
    }
    defaults.update(overrides)
    return _transition_pair_wire(**defaults)


def _reject_pair_wire(**overrides: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    defaults: dict[str, Any] = {
        "action": "candidate.reject",
        "reason_code": "insufficient_evidence",
        "prev_layer": "l1", "prev_state": "candidate", "prev_authority": "proposed",
        "upd_layer": "l1", "upd_state": "rejected", "upd_authority": "rejected",
        "upd_reviewer": "reviewer-1",
    }
    defaults.update(overrides)
    return _transition_pair_wire(**defaults)


def _supersede_pair_wire(
    *, replacement: dict[str, Any] | None = None, **overrides: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A `record.supersede` pair whose updated record is bound to `replacement`.

    The prior record keeps its own claim (`doc-1`, `{"fact": "hello"}`); the updated one
    carries exactly the replacement's content, disposition, sources, assertion, extraction,
    event/observed times, and proposed validity bounds, and the appended audit event carries
    exactly the replacement's assertion evidence.
    """
    if replacement is None:
        replacement = REPLACEMENT
    assertion = replacement["assertion"]
    temporal: dict[str, Any] = {"ingested_at": T0, "recorded_at": overrides.get("transition_at", T1)}
    for input_key, temporal_key in (
        ("event_at", "event_at"),
        ("observed_at", "observed_at"),
    ):
        if input_key in replacement:
            temporal[temporal_key] = replacement[input_key]
    for assertion_key, temporal_key in (
        ("proposed_valid_from", "valid_from"),
        ("proposed_valid_until", "valid_until"),
    ):
        if assertion_key in assertion:
            temporal[temporal_key] = assertion[assertion_key]

    defaults: dict[str, Any] = {
        "action": "record.supersede",
        "reason_code": "new_evidence",
        "record_type": replacement["record_type"],
        "domain_scope": replacement["domain_scope"],
        "prev_layer": "l2", "prev_state": "accepted", "prev_authority": "canonical",
        "prev_reviewer": "reviewer-0",
        "upd_layer": "l2", "upd_state": "accepted", "upd_authority": "canonical",
        "upd_reviewer": "reviewer-1",
        "upd_content": replacement["content"],
        "upd_evidence_disposition": replacement["evidence_disposition"],
        "upd_sources": replacement["sources"],
        "upd_assertion": assertion,
        "upd_extraction": replacement.get("extraction"),
        "upd_temporal": temporal,
        "event_evidence": assertion["evidence"],
    }
    defaults.update(overrides)
    return _transition_pair_wire(**defaults)


def _precondition(record_version: str = "v1") -> MutationPrecondition:
    return MutationPrecondition.from_wire({"record_version": record_version})


def _propose_request(**overrides: Any) -> KnowledgeProposeInput:
    payload: dict[str, Any] = {"record_id": "rec-1", "rationale": _rationale_wire()}
    payload.update(overrides)
    return KnowledgeProposeInput.from_wire(payload)


def _approve_request(**overrides: Any) -> CandidateApproveInput:
    payload: dict[str, Any] = {"record_id": "rec-1", "rationale": _rationale_wire()}
    payload.update(overrides)
    return CandidateApproveInput.from_wire(payload)


def _reject_request(**overrides: Any) -> CandidateRejectInput:
    payload: dict[str, Any] = {
        "record_id": "rec-1",
        "rationale": _rationale_wire(reason_code="insufficient_evidence"),
    }
    payload.update(overrides)
    return CandidateRejectInput.from_wire(payload)


def _supersede_request(**overrides: Any) -> RecordSupersedeInput:
    payload: dict[str, Any] = {
        "record_id": "rec-1",
        "replacement": REPLACEMENT,
        "rationale": _rationale_wire(reason_code="new_evidence"),
    }
    payload.update(overrides)
    return RecordSupersedeInput.from_wire(payload)


def _evidence_artifact_wire(
    *,
    evidence_id: str = "ev-1",
    workspace_id: str = "ws-1",
    source: dict[str, Any] | None = None,
    temporal: dict[str, Any] | None = None,
    content_checksum: str = "sha256:deadbeef",
    media_type: str = "text/plain",
    metadata: dict[str, Any] | None = None,
    permission_labels: list[str] | None = None,
    sensitivity: str = "public",
    tombstoned: bool = False,
    parser_status: str = "parsed",
    ingestion_status: str = "ingested",
    provenance_history: list[dict[str, Any]] | None = None,
    import_run_id: str | None = None,
) -> dict[str, Any]:
    source = source if source is not None else _source_wire()
    if provenance_history is None:
        provenance_history = (
            [_history_entry_wire(action="tombstoned")] if tombstoned else [_history_entry_wire()]
        )
    document: dict[str, Any] = {
        "evidence_id": evidence_id,
        "workspace_id": workspace_id,
        "source": source,
        "temporal": temporal if temporal is not None else _temporal_wire(),
        "content_checksum": content_checksum,
        "media_type": media_type,
        "metadata": metadata if metadata is not None else {},
        "permission_labels": permission_labels if permission_labels is not None else [],
        "sensitivity": sensitivity,
        "tombstoned": tombstoned,
        "parser_status": parser_status,
        "ingestion_status": ingestion_status,
        "provenance_history": provenance_history,
    }
    if import_run_id is not None:
        document["import_run_id"] = import_run_id
    return document


def _evidence_search_input_wire(
    *,
    query: str = "hello",
    sensitivity: str | None = None,
    include_tombstoned: bool | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {"query": query}
    if sensitivity is not None:
        document["sensitivity"] = sensitivity
    if include_tombstoned is not None:
        document["include_tombstoned"] = include_tombstoned
    if limit is not None:
        document["limit"] = limit
    return document


def _freshness_wire(
    *,
    as_of: str = T0,
    projection_versions: dict[str, str] | None = None,
    projection_watermarks: dict[str, str] | None = None,
    stale: bool = False,
) -> dict[str, Any]:
    """A strict `ProjectionFreshness`: both maps non-empty and keyed identically.

    An explicitly passed empty mapping stays empty -- the point of most of the negative
    freshness cases below -- so neither map may be defaulted through `or {}`, which would
    silently swap a deliberate `{}` for the default and make those tests assert nothing.
    """
    return {
        "as_of": as_of,
        "projection_versions": (
            {"knowledge_index": "pv-1"} if projection_versions is None else projection_versions
        ),
        "projection_watermarks": (
            {"knowledge_index": "wm-1"} if projection_watermarks is None else projection_watermarks
        ),
        "stale": stale,
    }


def _graph_node_wire(
    *,
    reference: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    reference = reference if reference is not None else _record_version_reference_wire()
    return {
        "reference": reference,
        "record": record if record is not None else _canonical_knowledge_record_wire(
            record_id=reference["record_id"], version=reference["version"]
        ),
        "depth": depth,
    }


def _graph_edge_wire(
    *,
    relation_type: str = "relates_to",
    source: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
    include_source: bool = True,
    include_target: bool = True,
    record: dict[str, Any] | None = None,
    relation_reference: dict[str, Any] | None = None,
    boundary_reason: str | None = None,
) -> dict[str, Any]:
    """One edge. `relation_reference` defaults to `record`'s own identity, so an edge is
    identity-bound unless a test deliberately breaks that binding."""
    record = record if record is not None else _canonical_knowledge_record_wire(record_id="rel-1", version="v1")
    identity = record["provenance"]["identity"]
    document: dict[str, Any] = {
        "relation_type": relation_type,
        "record": record,
        "relation_reference": relation_reference if relation_reference is not None else _record_version_reference_wire(
            record_id=identity["record_id"], version=identity["version"]
        ),
    }
    if include_source:
        document["source"] = source if source is not None else _record_version_reference_wire()
    if include_target:
        document["target"] = target if target is not None else _record_version_reference_wire(record_id="rec-2")
    if boundary_reason is not None:
        document["boundary_reason"] = boundary_reason
    return document


def _graph_traversal_result_wire(
    *,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    applied_depth_limit: int = 3,
    applied_node_limit: int = 10,
    applied_edge_limit: int = 10,
    freshness: dict[str, Any] | None = None,
    ordering_basis: str = "depth_record_version_asc",
    page: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "nodes": nodes if nodes is not None else [_graph_node_wire()],
        "edges": edges if edges is not None else [],
        "applied_depth_limit": applied_depth_limit,
        "applied_node_limit": applied_node_limit,
        "applied_edge_limit": applied_edge_limit,
        "freshness": freshness if freshness is not None else _freshness_wire(),
        "ordering_basis": ordering_basis,
        # `page` is required on every paginated result in this contract, and `{}` is how an
        # exhausted read states itself. A test that wants "more remains" passes a token.
        "page": page if page is not None else {},
    }
    return document


# --------------------------------------------------------------------------
# Context Pack canonical fixture
#
# One pack that exercises every partition at once: L0 evidence, a current canonical L2
# record, a superseded historical L2 version, and a current canonical L3 context model,
# each cited exactly once and all four citations used by one section. A minimal fixture
# would leave whole partitions unexercised by every positive case, and would make each
# negative case prove less than it appears to.
# --------------------------------------------------------------------------

TOKEN_BUDGET = 1000
SECTION_TOKENS = 12

EV_CHECKSUM = "sha256:" + "9f" * 32
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64

EXPECTED_ROLES = ("analyst", "reader")
EXPECTED_CAPABILITIES = (
    ("memory.read", "1.0"),
    ("memory.write", "1.1"),
    ("workspace.read", "1.0"),
)
EXPECTED_SCOPES = ("memory:read", "workspace:read")
EXPECTED_POLICY_VERSIONS = {"acl": "pv-acl-1", "sensitivity": "pv-sens-1"}
"""Roles, capabilities, and scopes are written already in ascending order.

Capabilities previously carried the same id at two versions on purpose, to catch a
sort that compared ids alone and a set comparison that collapsed the pair. A granted
authority may no longer name one id twice at all -- the response envelope always
refused it, and the Context Pack rule now agrees -- so that shape is not a hazard
this fixture can still hold. Distinct ids in ascending order exercise the ordering
rule, and the differing versions keep version part of the recorded identity, which
the expected-set comparison still checks pairwise."""

ALL_CITATION_IDS = ("cit-1", "cit-2", "cit-3", "cit-4")

PACK_EVIDENCE_ID = "ev-1"
PACK_RECORD_ID = "rec-1"
PACK_HISTORY_ID = "rec-old"
PACK_CONTEXT_MODEL_ID = "ctx-1"


def _pack_evidence_wire(
    *, evidence_id: str = PACK_EVIDENCE_ID, content_checksum: str = EV_CHECKSUM, **overrides: Any
) -> dict[str, Any]:
    return _evidence_artifact_wire(
        evidence_id=evidence_id, content_checksum=content_checksum, **overrides
    )


def _pack_record_wire(
    *, record_id: str = PACK_RECORD_ID, version: str = "v1", **overrides: Any
) -> dict[str, Any]:
    return _canonical_knowledge_record_wire(record_id=record_id, version=version, **overrides)


def _pack_history_wire(
    *,
    record_id: str = PACK_HISTORY_ID,
    version: str = "v1",
    superseded_at: str = T1,
    **overrides: Any,
) -> dict[str, Any]:
    return _history_knowledge_record_wire(
        record_id=record_id, version=version, superseded_at=superseded_at, **overrides
    )


def _pack_context_model_wire(
    *,
    record_id: str = PACK_CONTEXT_MODEL_ID,
    version: str = "v1",
    layer: str = "l3",
    governance_state: str = "accepted",
    currentness: str = "current",
    authority_level: str = "canonical",
    reviewer: str | None = "reviewer-1",
    record_type: str = "memory.fact",
    domain_scope: str = "personal.preferences",
    temporal: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A current canonical L3 context model.

    Held to exactly the same accepted/canonical/reviewed/currently-valid bar an L2 record
    is; only the layer differs, which is why every axis is a parameter here.
    """
    identity = _identity_wire(
        record_id=record_id,
        version=version,
        layer=layer,
        governance_state=governance_state,
        currentness=currentness,
    )
    return _governed_record_wire(
        record_type=record_type,
        domain_scope=domain_scope,
        authority_level=authority_level,
        reviewer=reviewer,
        provenance=_provenance_wire(
            identity=identity, temporal=temporal, history=history, sources=sources
        ),
    )


def _context_pack_evidence_reference_wire(
    *, evidence_id: str = PACK_EVIDENCE_ID, content_checksum: str = EV_CHECKSUM
) -> dict[str, Any]:
    return {"evidence_id": evidence_id, "content_checksum": content_checksum}


# --- the authorized candidate frontier the canonical pack was built from ------
#
# Every positive Context Pack case is validated against a real manifest rather than a digest
# copied out of the artifact, because that copy is the one thing the checksum must never be
# checked by: a value read back out of the pack agrees with itself whatever the pack ranked
# over. The canonical frontier is exactly the four identities the canonical pack selects, so
# the selected-subset rule is satisfied by construction and every negative case that adds or
# changes a selection breaks it for a stated reason.

CANDIDATE_MANIFEST_FORMAT = sem_knowledge.CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT


def _evidence_candidate(
    *, evidence_id: str = PACK_EVIDENCE_ID, content_checksum: str = EV_CHECKSUM
) -> ContextPackAuthorizedEvidenceCandidate:
    return ContextPackAuthorizedEvidenceCandidate(
        partition="evidence", evidence_id=evidence_id, content_checksum=content_checksum
    )


def _record_candidate(
    partition: str, record_id: str, version: str = "v1"
) -> ContextPackAuthorizedRecordCandidate:
    return ContextPackAuthorizedRecordCandidate(
        partition=partition, record_id=record_id, version=version
    )


def _canonical_candidates() -> tuple[Any, ...]:
    """Exactly what the canonical pack selects, one candidate per partition."""
    return (
        _evidence_candidate(),
        _record_candidate("records", PACK_RECORD_ID),
        _record_candidate("history", PACK_HISTORY_ID),
        _record_candidate("context_models", PACK_CONTEXT_MODEL_ID),
    )


def _candidate_manifest(
    *,
    workspace_id: str = "ws-1",
    candidates: tuple[Any, ...] | None = None,
    format_: str = CANDIDATE_MANIFEST_FORMAT,
) -> ContextPackAuthorizedCandidateSetManifest:
    return ContextPackAuthorizedCandidateSetManifest(
        format=format_,
        workspace_id=workspace_id,
        candidates=_canonical_candidates() if candidates is None else candidates,
    )


CANDIDATE_SET_DIGEST = sem_knowledge.compute_authorized_candidate_set_checksum(
    _candidate_manifest()
)
"""The digest of the canonical frontier, computed rather than written down: a constant typed
out by hand would be a second, unverified statement of the same rule."""


def _evidence_citation_wire(
    *,
    citation_id: str = "cit-1",
    evidence_reference: dict[str, Any] | None = None,
    content_pointer: str | None = None,
    source_span: dict[str, Any] | None = None,
    excerpt: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "citation_id": citation_id,
        "evidence_reference": (
            evidence_reference
            if evidence_reference is not None
            else _context_pack_evidence_reference_wire()
        ),
    }
    if content_pointer is not None:
        document["content_pointer"] = content_pointer
    if source_span is not None:
        document["source_span"] = source_span
    if excerpt is not None:
        document["excerpt"] = excerpt
    return document


def _record_citation_wire(
    *,
    citation_id: str = "cit-2",
    record_reference: dict[str, Any] | None = None,
    content_pointer: str | None = None,
    source_span: dict[str, Any] | None = None,
    excerpt: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "citation_id": citation_id,
        "record_reference": (
            record_reference if record_reference is not None else _record_version_reference_wire()
        ),
    }
    if content_pointer is not None:
        document["content_pointer"] = content_pointer
    if source_span is not None:
        document["source_span"] = source_span
    if excerpt is not None:
        document["excerpt"] = excerpt
    return document


def _canonical_citations() -> list[dict[str, Any]]:
    """One citation per selected item, in ascending `citation_id` order."""
    return [
        _evidence_citation_wire(citation_id="cit-1"),
        _record_citation_wire(
            citation_id="cit-2",
            record_reference=_record_version_reference_wire(record_id=PACK_RECORD_ID, version="v1"),
        ),
        _record_citation_wire(
            citation_id="cit-3",
            record_reference=_record_version_reference_wire(
                record_id=PACK_HISTORY_ID, version="v1"
            ),
        ),
        _record_citation_wire(
            citation_id="cit-4",
            record_reference=_record_version_reference_wire(
                record_id=PACK_CONTEXT_MODEL_ID, version="v1"
            ),
        ),
    ]


def _section_wire(
    *,
    section_id: str = "sec-1",
    kind: str = "summary",
    title: str | None = None,
    content: str = "what the workspace currently knows",
    citation_ids: list[str] | None = None,
    token_count: int = SECTION_TOKENS,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "section_id": section_id,
        "kind": kind,
        "content": content,
        "citation_ids": citation_ids if citation_ids is not None else list(ALL_CITATION_IDS),
        "token_count": token_count,
    }
    if title is not None:
        document["title"] = title
    return document


def _granted_authority_wire(
    *,
    principal_id: str = "principal-1",
    roles: list[str] | None = None,
    capabilities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "principal_id": principal_id,
        "roles": list(EXPECTED_ROLES) if roles is None else roles,
        "capabilities": (
            [{"id": id_, "version": version} for id_, version in EXPECTED_CAPABILITIES]
            if capabilities is None
            else capabilities
        ),
    }


def _authorization_context_wire(
    *,
    workspace_id: str = "ws-1",
    authority: dict[str, Any] | None = None,
    scopes: list[str] | None = None,
    purpose: str = "assist.chat",
    policy_versions: dict[str, str] | None = None,
    pre_ranking_authorization_enforced: bool = True,
    authorized_candidate_set_checksum: str = CANDIDATE_SET_DIGEST,
) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id,
        "authority": _granted_authority_wire() if authority is None else authority,
        "scopes": list(EXPECTED_SCOPES) if scopes is None else scopes,
        "purpose": purpose,
        "policy_versions": (
            dict(EXPECTED_POLICY_VERSIONS) if policy_versions is None else policy_versions
        ),
        "pre_ranking_authorization_enforced": pre_ranking_authorization_enforced,
        "authorized_candidate_set_checksum": authorized_candidate_set_checksum,
    }


def _normalized_request_wire(
    *,
    normalized_query: str = "hello",
    mode: str = "deterministic_view",
    view: str = "current_canonical",
    token_budget: int = TOKEN_BUDGET,
    normalization_version: str = "norm-1",
    domain_scope: str | None = None,
    record_type: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "normalized_query": normalized_query,
        "mode": mode,
        "view": view,
        "token_budget": token_budget,
        "normalization_version": normalization_version,
    }
    if domain_scope is not None:
        document["domain_scope"] = domain_scope
    if record_type is not None:
        document["record_type"] = record_type
    return document


def _canonical_record_versions() -> list[dict[str, Any]]:
    """The sorted union of every governed identity the canonical pack selects."""
    return [
        _record_version_reference_wire(record_id=PACK_CONTEXT_MODEL_ID, version="v1"),
        _record_version_reference_wire(record_id=PACK_RECORD_ID, version="v1"),
        _record_version_reference_wire(record_id=PACK_HISTORY_ID, version="v1"),
    ]


def _reproducibility_wire(
    *,
    pack_format_version: str = "1.0",
    builder_version: str = "builder-1",
    normalized_request: dict[str, Any] | None = None,
    authorization_context: dict[str, Any] | None = None,
    evidence_versions: list[dict[str, Any]] | None = None,
    record_versions: list[dict[str, Any]] | None = None,
    freshness: dict[str, Any] | None = None,
    retrieval_version: str = "retrieval-1",
    ranking_version: str = "ranking-1",
    reranking_version: str = "reranking-1",
    selection_version: str = "selection-1",
    tokenizer_id: str = "tokenizer-1",
    tokenizer_version: str = "tokenizer-v1",
    summarizer_version: str = "disabled",
    model_versions: dict[str, str] | None = None,
    canonical_resolution_time: str = T1,
    generated_at: str = T1,
    artifact_canonicalization: str = "rfc8785",
    artifact_checksum: str = PLACEHOLDER_DIGEST,
) -> dict[str, Any]:
    return {
        "pack_format_version": pack_format_version,
        "builder_version": builder_version,
        "normalized_request": (
            _normalized_request_wire() if normalized_request is None else normalized_request
        ),
        "authorization_context": (
            _authorization_context_wire()
            if authorization_context is None
            else authorization_context
        ),
        "evidence_versions": (
            [_context_pack_evidence_reference_wire()]
            if evidence_versions is None
            else evidence_versions
        ),
        "record_versions": (
            _canonical_record_versions() if record_versions is None else record_versions
        ),
        "freshness": _freshness_wire(as_of=T1) if freshness is None else freshness,
        "retrieval_version": retrieval_version,
        "ranking_version": ranking_version,
        "reranking_version": reranking_version,
        "selection_version": selection_version,
        "tokenizer_id": tokenizer_id,
        "tokenizer_version": tokenizer_version,
        "summarizer_version": summarizer_version,
        "model_versions": {} if model_versions is None else model_versions,
        "canonical_resolution_time": canonical_resolution_time,
        "generated_at": generated_at,
        "artifact_canonicalization": artifact_canonicalization,
        "artifact_checksum": artifact_checksum,
    }


def _budget_wire(
    *, token_budget: int = TOKEN_BUDGET, tokens_used: int = SECTION_TOKENS
) -> dict[str, Any]:
    return {"token_budget": token_budget, "tokens_used": tokens_used}


def _context_pack_build_result_wire(
    *,
    pack_id: str = PLACEHOLDER_DIGEST,
    mode: str = "deterministic_view",
    query: str = "hello",
    sections: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    records: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
    context_models: list[dict[str, Any]] | None = None,
    citations: list[dict[str, Any]] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    uncertainties: list[dict[str, Any]] | None = None,
    omissions: list[dict[str, Any]] | None = None,
    budget: dict[str, Any] | None = None,
    reproducibility: dict[str, Any] | None = None,
    fresh_authorization_required: bool = True,
) -> dict[str, Any]:
    """The canonical Context Pack, unsigned.

    `pack_id` and `reproducibility.artifact_checksum` carry a placeholder: the digest is
    computed over the result with both members removed, so a caller mutates first and signs
    last (:func:`_signed`).
    """
    return {
        "pack_id": pack_id,
        "mode": mode,
        "query": query,
        "sections": [_section_wire()] if sections is None else sections,
        "evidence": [_pack_evidence_wire()] if evidence is None else evidence,
        "records": [_pack_record_wire()] if records is None else records,
        "history": [_pack_history_wire()] if history is None else history,
        "context_models": (
            [_pack_context_model_wire()] if context_models is None else context_models
        ),
        "citations": _canonical_citations() if citations is None else citations,
        "conflicts": [] if conflicts is None else conflicts,
        "uncertainties": [] if uncertainties is None else uncertainties,
        "omissions": [] if omissions is None else omissions,
        "budget": _budget_wire() if budget is None else budget,
        "reproducibility": _reproducibility_wire() if reproducibility is None else reproducibility,
        "fresh_authorization_required": fresh_authorization_required,
    }


def _signed(document: dict[str, Any]) -> dict[str, Any]:
    """Return `document` with `pack_id` and `artifact_checksum` set to its own digest.

    Every positive Context Pack case ends here, and so does every negative case whose point
    is *not* the checksum: a pack that breaks a content rule must fail on that rule, never
    incidentally on a stale digest, or the test would pass for the wrong reason.
    """
    signed = copy.deepcopy(document)
    signed["pack_id"] = PLACEHOLDER_DIGEST
    signed["reproducibility"]["artifact_checksum"] = PLACEHOLDER_DIGEST
    digest = sem_knowledge.compute_context_pack_artifact_digest(signed)
    signed["pack_id"] = digest
    signed["reproducibility"]["artifact_checksum"] = digest
    return signed


def _context_pack_request(**overrides: Any) -> ContextPackBuildInput:
    wire: dict[str, Any] = {
        "query": "hello",
        "mode": "deterministic_view",
        "token_budget": TOKEN_BUDGET,
    }
    wire.update(overrides)
    return ContextPackBuildInput.from_wire(wire)


def _pack(**overrides: Any) -> ContextPackBuildResult:
    """The canonical signed pack, decoded, with `overrides` applied before signing."""
    return ContextPackBuildResult.from_wire(_signed(_context_pack_build_result_wire(**overrides)))


def _validate_pack(result: ContextPackBuildResult, **overrides: Any) -> None:
    """Run the full result validator with the canonical caller context.

    The validator's arguments are keyword-only and mandatory by design, so every call site
    states the whole context; `overrides` exists so a case can vary exactly one part of it.
    """
    context: dict[str, Any] = {
        "request": _context_pack_request(),
        "expected_workspace_id": "ws-1",
        "expected_authority": GrantedAuthority.from_wire(_granted_authority_wire()),
        "expected_scopes": set(EXPECTED_SCOPES),
        "expected_purpose": "assist.chat",
        "expected_policy_versions": dict(EXPECTED_POLICY_VERSIONS),
        "expected_authorized_candidate_set": _candidate_manifest(),
        "canonical_resolution_time": T1,
        "response_freshness": ProjectionFreshness.from_wire(_freshness_wire(as_of=T1)),
    }
    context.update(overrides)
    sem_knowledge.validate_context_pack_build_result(result, **context)


def _validate_pack_document(document: dict[str, Any], **overrides: Any) -> ContextPackBuildResult:
    """The same canonical caller context, applied through the *raw-document* entry point.

    Both public entry points must reject the same content, so every rule whose point is
    "the stored artifact is out of bounds" is exercised from the bytes as well as from the
    trusted DTO -- otherwise a repair could close a gap on one path and leave it open on the
    one a trust boundary actually uses.
    """
    context: dict[str, Any] = {
        "request": _context_pack_request(),
        "expected_workspace_id": "ws-1",
        "expected_authority": GrantedAuthority.from_wire(_granted_authority_wire()),
        "expected_scopes": set(EXPECTED_SCOPES),
        "expected_purpose": "assist.chat",
        "expected_policy_versions": dict(EXPECTED_POLICY_VERSIONS),
        "expected_authorized_candidate_set": _candidate_manifest(),
        "canonical_resolution_time": T1,
        "response_freshness": ProjectionFreshness.from_wire(_freshness_wire(as_of=T1)),
    }
    context.update(overrides)
    return sem_knowledge.validate_context_pack_build_result_document(
        json.dumps(document), **context
    )


# --------------------------------------------------------------------------
# 1. Strict schema and tolerant DTO round trips for every payload
# --------------------------------------------------------------------------

_PROPOSE_PREV, _PROPOSE_UPD = _propose_pair_wire()
_APPROVE_PREV, _APPROVE_UPD = _approve_pair_wire()
_REJECT_PREV, _REJECT_UPD = _reject_pair_wire()
_SUPERSEDE_PREV, _SUPERSEDE_UPD = _supersede_pair_wire()

ROUND_TRIP_CASES: tuple[tuple[str, type, dict[str, Any]], ...] = (
    ("EvidenceSearchInput", EvidenceSearchInput, {"query": "hello"}),
    ("EvidenceSearchResult", EvidenceSearchResult, {"evidence": [_evidence_artifact_wire()], "page": {}}),
    ("KnowledgeSearchInput", KnowledgeSearchInput, {"query": "hello"}),
    ("KnowledgeSearchResult", KnowledgeSearchResult, {"records": [_accepted_record_wire()], "page": {}}),
    (
        "KnowledgeProposeInput",
        KnowledgeProposeInput,
        {"record_id": "rec-1", "rationale": _rationale_wire()},
    ),
    (
        "KnowledgeProposeResult",
        KnowledgeProposeResult,
        {"previous_record": _PROPOSE_PREV, "updated_record": _PROPOSE_UPD},
    ),
    (
        "CandidateApproveInput",
        CandidateApproveInput,
        {"record_id": "rec-1", "rationale": _rationale_wire()},
    ),
    (
        "CandidateApproveResult",
        CandidateApproveResult,
        {"previous_record": _APPROVE_PREV, "updated_record": _APPROVE_UPD},
    ),
    (
        "CandidateRejectInput",
        CandidateRejectInput,
        {"record_id": "rec-1", "rationale": _rationale_wire(reason_code="insufficient_evidence")},
    ),
    (
        "CandidateRejectResult",
        CandidateRejectResult,
        {"previous_record": _REJECT_PREV, "updated_record": _REJECT_UPD},
    ),
    (
        "RecordSupersedeInput",
        RecordSupersedeInput,
        {
            "record_id": "rec-1",
            "replacement": REPLACEMENT,
            "rationale": _rationale_wire(reason_code="new_evidence"),
        },
    ),
    (
        "RecordSupersedeResult",
        RecordSupersedeResult,
        {"previous_record": _SUPERSEDE_PREV, "updated_record": _SUPERSEDE_UPD},
    ),
    (
        "GraphTraversalInput",
        GraphTraversalInput,
        {"start": [_record_version_reference_wire()], "direction": "outbound"},
    ),
    ("GraphTraversalResult", GraphTraversalResult, _graph_traversal_result_wire()),
    ("GraphNode", GraphNode, _graph_node_wire()),
    ("GraphEdge", GraphEdge, _graph_edge_wire()),
    (
        "ContextPackBuildInput",
        ContextPackBuildInput,
        {"query": "hello", "mode": "deterministic_view", "token_budget": TOKEN_BUDGET},
    ),
    (
        "ContextPackBuildResult",
        ContextPackBuildResult,
        _signed(_context_pack_build_result_wire()),
    ),
    ("ContextPackEvidenceReference", ContextPackEvidenceReference, _context_pack_evidence_reference_wire()),
    ("ContextPackEvidenceCitation", ContextPackEvidenceCitation, _evidence_citation_wire()),
    ("ContextPackRecordCitation", ContextPackRecordCitation, _record_citation_wire()),
    ("ContextPackSection", ContextPackSection, _section_wire()),
    (
        "ContextPackConflict",
        ContextPackConflict,
        {"description": "conflict", "conflicting_citation_ids": ["cit-1", "cit-2"]},
    ),
    (
        "ContextPackUncertainty",
        ContextPackUncertainty,
        {"description": "uncertain", "related_citation_ids": ["cit-1"]},
    ),
    ("ContextPackBudget", ContextPackBudget, _budget_wire()),
    ("ContextPackAuthorizationContext", ContextPackAuthorizationContext, _authorization_context_wire()),
    ("ContextPackNormalizedRequest", ContextPackNormalizedRequest, _normalized_request_wire()),
    ("ContextPackReproducibility", ContextPackReproducibility, _reproducibility_wire()),
)


def test_every_operation_payload_is_covered() -> None:
    """A frozen count, so a payload added to the contract but not to this table fails.

    `ContextPackCitation` is absent by design: it is a `oneOf` union rather than an object,
    so it has no `from_wire` classmethod to parametrize over. Both of its branches appear
    here, and the union's own codec is covered by
    :func:`test_context_pack_citation_union_decodes_exactly_one_branch`.
    """
    assert len(ROUND_TRIP_CASES) == 28


@pytest.mark.parametrize("def_name,dataclass,wire", ROUND_TRIP_CASES)
def test_strict_schema_accepts_canonical_document(def_name: str, dataclass: type, wire: dict[str, Any]) -> None:
    assert _is_schema_valid(def_name, wire)


@pytest.mark.parametrize("def_name,dataclass,wire", ROUND_TRIP_CASES)
def test_tolerant_dto_round_trips_canonical_document(def_name: str, dataclass: type, wire: dict[str, Any]) -> None:
    value = dataclass.from_wire(wire)
    assert value.to_wire() == wire


@pytest.mark.parametrize("def_name,dataclass,wire", ROUND_TRIP_CASES)
def test_additive_unknown_field_tolerated_by_dto_rejected_by_schema(
    def_name: str, dataclass: type, wire: dict[str, Any]
) -> None:
    augmented = {**wire, "future_hint_field": "some-value"}
    assert not _is_schema_valid(def_name, augmented)
    baseline = dataclass.from_wire(wire)
    assert dataclass.from_wire(augmented) == baseline


@pytest.mark.parametrize("def_name,dataclass,wire", ROUND_TRIP_CASES)
def test_open_code_novel_value_round_trips(def_name: str, dataclass: type, wire: dict[str, Any]) -> None:
    """A well-formed but never-before-seen open-code value must be preserved verbatim."""
    novel = json.loads(json.dumps(wire).replace("relates_to", "some_future_relation"))
    value = dataclass.from_wire(novel)
    assert value.to_wire() == novel


# --------------------------------------------------------------------------
# 2. evidence.search adversarial cases
# --------------------------------------------------------------------------

EVIDENCE_REQUIRED_FIELDS = (
    "evidence_id",
    "workspace_id",
    "source",
    "temporal",
    "content_checksum",
    "media_type",
    "metadata",
    "permission_labels",
    "sensitivity",
    "tombstoned",
    "parser_status",
    "ingestion_status",
    "provenance_history",
)


@pytest.mark.parametrize("field_name", EVIDENCE_REQUIRED_FIELDS)
def test_evidence_artifact_missing_each_mandatory_field_rejected(field_name: str) -> None:
    wire = _evidence_artifact_wire()
    del wire[field_name]
    assert not _is_schema_valid("EvidenceSearchResult", {"evidence": [wire], "page": {}})
    with pytest.raises(ContractDecodeError):
        EvidenceArtifact.from_wire(wire)


def test_evidence_search_result_rejects_workspace_mismatch() -> None:
    result = EvidenceSearchResult.from_wire(
        {"evidence": [_evidence_artifact_wire(workspace_id="ws-other")], "page": {}}
    )
    request = sem_evidence.decode_evidence_search_input(_evidence_search_input_wire())
    with pytest.raises(ContractSemanticError, match="does not match"):
        sem_evidence.validate_evidence_search_result(result, request, "ws-1")


def test_evidence_artifact_rejects_malformed_source_kind() -> None:
    artifact = EvidenceArtifact.from_wire(_evidence_artifact_wire(source=_source_wire(kind="Not Valid!")))
    with pytest.raises(ContractSemanticError):
        sem_evidence.validate_evidence_artifact(artifact)


def test_evidence_artifact_rejects_malformed_provenance_history_entry() -> None:
    artifact = EvidenceArtifact.from_wire(
        _evidence_artifact_wire(
            tombstoned=True,
            provenance_history=[_history_entry_wire(actor_kind="Not Valid!")],
        )
    )
    with pytest.raises(ContractSemanticError):
        sem_evidence.validate_evidence_artifact(artifact)


def test_evidence_artifact_rejects_malformed_temporal_ordering() -> None:
    artifact = EvidenceArtifact.from_wire(
        _evidence_artifact_wire(temporal=_temporal_wire(ingested_at=T1, recorded_at=T0))
    )
    with pytest.raises(ContractSemanticError, match="ingested_at"):
        sem_evidence.validate_evidence_artifact(artifact)


def test_evidence_artifact_rejects_tombstone_with_no_provenance_history() -> None:
    artifact = EvidenceArtifact.from_wire(_evidence_artifact_wire(tombstoned=True, provenance_history=[]))
    with pytest.raises(ContractSemanticError, match="tombstoned"):
        sem_evidence.validate_evidence_artifact(artifact)


def test_evidence_search_result_rejects_tombstone_leakage_when_not_requested() -> None:
    result = EvidenceSearchResult.from_wire(
        {
            "evidence": [
                _evidence_artifact_wire(
                    tombstoned=True, provenance_history=[_history_entry_wire(action="tombstoned")]
                )
            ],
            "page": {},
        }
    )
    request = sem_evidence.decode_evidence_search_input(
        _evidence_search_input_wire(include_tombstoned=False)
    )
    with pytest.raises(ContractSemanticError, match="tombstoned"):
        sem_evidence.validate_evidence_search_result(result, request, "ws-1")


def test_evidence_search_result_allows_tombstone_when_requested() -> None:
    result = EvidenceSearchResult.from_wire(
        {
            "evidence": [
                _evidence_artifact_wire(
                    tombstoned=True, provenance_history=[_history_entry_wire(action="tombstoned")]
                )
            ],
            "page": {},
        }
    )
    request = sem_evidence.decode_evidence_search_input(
        _evidence_search_input_wire(include_tombstoned=True)
    )
    sem_evidence.validate_evidence_search_result(result, request, "ws-1")


def test_evidence_artifact_rejects_observed_after_ingested() -> None:
    artifact = EvidenceArtifact.from_wire(
        _evidence_artifact_wire(temporal=_temporal_wire(observed_at=T1, ingested_at=T0))
    )
    with pytest.raises(ContractSemanticError, match="observed_at"):
        sem_evidence.validate_evidence_artifact(artifact)


def test_evidence_artifact_rejects_duplicate_permission_labels() -> None:
    artifact = EvidenceArtifact.from_wire(
        _evidence_artifact_wire(permission_labels=["restricted", "restricted"])
    )
    with pytest.raises(ContractSemanticError, match="duplicates"):
        sem_evidence.validate_evidence_artifact(artifact)


def test_evidence_artifact_rejects_empty_provenance_history_when_not_tombstoned() -> None:
    artifact = EvidenceArtifact.from_wire(_evidence_artifact_wire(provenance_history=[]))
    with pytest.raises(ContractSemanticError, match="provenance_history must not be empty"):
        sem_evidence.validate_evidence_artifact(artifact)


def test_evidence_artifact_rejects_generic_history_action_on_tombstoned_artifact() -> None:
    artifact = EvidenceArtifact.from_wire(
        _evidence_artifact_wire(tombstoned=True, provenance_history=[_history_entry_wire(action="created")])
    )
    with pytest.raises(ContractSemanticError, match="tombstoned"):
        sem_evidence.validate_evidence_artifact(artifact)


def test_evidence_artifact_rejects_foreign_nested_provenance_evidence() -> None:
    artifact = EvidenceArtifact.from_wire(
        _evidence_artifact_wire(
            source=_source_wire(kind="document", source_id="doc-1"),
            provenance_history=[
                _history_entry_wire(
                    evidence=[_evidence_reference_wire(source=_source_wire(kind="document", source_id="doc-2"))]
                )
            ],
        )
    )
    with pytest.raises(ContractSemanticError, match="foreign, undeclared source"):
        sem_evidence.validate_evidence_artifact(artifact)


def test_evidence_artifact_accepts_nested_provenance_evidence_matching_own_source() -> None:
    artifact = EvidenceArtifact.from_wire(
        _evidence_artifact_wire(
            source=_source_wire(kind="document", source_id="doc-1"),
            provenance_history=[
                _history_entry_wire(
                    evidence=[
                        _evidence_reference_wire(
                            source=_source_wire(kind="document", source_id="doc-1"),
                            span={"pointer": "/body", "start_offset": 0, "end_offset": 10},
                            excerpt="hello",
                        )
                    ]
                )
            ],
        )
    )
    sem_evidence.validate_evidence_artifact(artifact)


# --- evidence schema/semantic parity -----------------------------------------
#
# One bounded comparison of `EvidenceArtifact`/`ProvenanceEntry` -- the structures a Context
# Pack reaches through its selected evidence partition -- against the JSON Schema, closing
# every divergence it found. Two kinds, and they fail on different paths:
#
#   * array cardinalities and string lengths, which the *tolerant decoder never applies*, so
#     an over-long wire document decodes cleanly and only strict JSON Schema would have
#     caught it;
#   * primitive types (`tombstoned` must be a boolean, span offsets must be integers), which
#     the tolerant decoder does enforce -- so the gap is on the trusted hand-built DTO path,
#     where a wrong type must still surface as `ContractSemanticError` rather than being
#     honoured by a truthiness test or leaking a raw `TypeError`.

EVIDENCE_PERMISSION_LABELS_MAX = 64
EVIDENCE_PROVENANCE_HISTORY_MAX = 1024
PROVENANCE_ENTRY_EVIDENCE_MAX = 256
PROVENANCE_REASON_COMMENT_MAX = 2048


def _labels(count: int) -> list[str]:
    return [f"label_{index:03d}" for index in range(count)]


def _self_evidence_references(count: int) -> list[dict[str, Any]]:
    """`count` references to this artifact's own declared source, which is the only source
    an artifact's provenance evidence may name."""
    return [
        _evidence_reference_wire(source=_source_wire(), excerpt=f"excerpt {index}")
        for index in range(count)
    ]


@pytest.mark.parametrize(
    "field_name,accepted,rejected,build",
    [
        (
            "permission_labels",
            EVIDENCE_PERMISSION_LABELS_MAX,
            EVIDENCE_PERMISSION_LABELS_MAX + 1,
            lambda count: {"permission_labels": _labels(count)},
        ),
        (
            "provenance_history",
            EVIDENCE_PROVENANCE_HISTORY_MAX,
            EVIDENCE_PROVENANCE_HISTORY_MAX + 1,
            lambda count: {"provenance_history": [_history_entry_wire()] * count},
        ),
        (
            "evidence",
            PROVENANCE_ENTRY_EVIDENCE_MAX,
            PROVENANCE_ENTRY_EVIDENCE_MAX + 1,
            lambda count: {
                "provenance_history": [
                    _history_entry_wire(evidence=_self_evidence_references(count))
                ]
            },
        ),
    ],
)
def test_evidence_artifact_bounded_arrays_match_the_schema(
    field_name: str, accepted: int, rejected: int, build: Any
) -> None:
    at_max = _evidence_artifact_wire(**build(accepted))
    assert _is_schema_valid("EvidenceArtifact", at_max)
    sem_evidence.validate_evidence_artifact(EvidenceArtifact.from_wire(at_max))

    over_max = _evidence_artifact_wire(**build(rejected))
    assert not _is_schema_valid("EvidenceArtifact", over_max)
    # The tolerant decoder applies no cardinality at all, which is why the bound has to be
    # restated semantically: without it this document passes every check but the schema's.
    with pytest.raises(
        ContractSemanticError, match=f"{field_name} has {rejected} entries"
    ):
        sem_evidence.validate_evidence_artifact(EvidenceArtifact.from_wire(over_max))


def test_evidence_artifact_rejects_a_non_boolean_tombstone_on_a_hand_built_dto() -> None:
    """The tolerant decoder refuses a wire `1` here, so the reachable gap is the trusted DTO.

    `tombstoned` decides whether the artifact must carry an audited tombstone entry at all,
    and an integer `1` would answer that question by truthiness rather than by the boolean
    the schema declares.
    """
    artifact = EvidenceArtifact.from_wire(_evidence_artifact_wire())
    assert not _is_schema_valid("EvidenceArtifact", _evidence_artifact_wire(tombstoned=1))
    with pytest.raises(ContractDecodeError, match="expected a boolean"):
        EvidenceArtifact.from_wire(_evidence_artifact_wire(tombstoned=1))
    for value in (1, 0, "true", None):
        with pytest.raises(ContractSemanticError, match="tombstoned: expected a boolean"):
            sem_evidence.validate_evidence_artifact(
                dataclasses.replace(artifact, tombstoned=value)
            )


@pytest.mark.parametrize("offset_field", ["start_offset", "end_offset"])
@pytest.mark.parametrize("value", [True, False, "0", 1.5])
def test_evidence_provenance_span_offsets_must_be_real_integers(
    offset_field: str, value: Any
) -> None:
    """A `bool` passes every `>= 0` test as an offset of one, and a `str` would raise a raw
    `TypeError` out of the contract layer. Both must be `ContractSemanticError`."""
    artifact = EvidenceArtifact.from_wire(
        _evidence_artifact_wire(
            provenance_history=[
                _history_entry_wire(
                    evidence=[
                        _evidence_reference_wire(
                            source=_source_wire(), span={"pointer": "/body", "start_offset": 0}
                        )
                    ]
                )
            ]
        )
    )
    entry = artifact.provenance_history[0]
    assert entry.evidence is not None
    reference = entry.evidence[0]
    assert reference.span is not None
    broken_span = dataclasses.replace(reference.span, **{offset_field: value})
    broken = dataclasses.replace(
        artifact,
        provenance_history=(
            dataclasses.replace(
                entry,
                evidence=(dataclasses.replace(reference, span=broken_span),),
            ),
        ),
    )
    with pytest.raises(ContractSemanticError, match=f"{offset_field}: expected an integer"):
        sem_evidence.validate_evidence_artifact(broken)


def test_evidence_provenance_entry_validates_an_optional_reason_code() -> None:
    """The same `ProvenanceEntry` shape a governed record's history uses, reached from the
    evidence side: it must not be held to a weaker bar here than there."""
    valid = _evidence_artifact_wire(
        provenance_history=[_history_entry_wire(reason_code="new_evidence")]
    )
    assert _is_schema_valid("EvidenceArtifact", valid)
    sem_evidence.validate_evidence_artifact(EvidenceArtifact.from_wire(valid))

    for bad in ("Not-An-OpenCode", "1leading_digit", "trailing.", "a" * 129):
        broken = _evidence_artifact_wire(
            provenance_history=[_history_entry_wire(reason_code=bad)]
        )
        assert not _is_schema_valid("EvidenceArtifact", broken)
        with pytest.raises(ContractSemanticError, match="is not a valid OpenCode"):
            sem_evidence.validate_evidence_artifact(EvidenceArtifact.from_wire(broken))


def test_evidence_provenance_entry_bounds_an_optional_reason_comment() -> None:
    at_max = _evidence_artifact_wire(
        provenance_history=[
            _history_entry_wire(reason_comment="c" * PROVENANCE_REASON_COMMENT_MAX)
        ]
    )
    assert _is_schema_valid("EvidenceArtifact", at_max)
    sem_evidence.validate_evidence_artifact(EvidenceArtifact.from_wire(at_max))

    over_max = _evidence_artifact_wire(
        provenance_history=[
            _history_entry_wire(reason_comment="c" * (PROVENANCE_REASON_COMMENT_MAX + 1))
        ]
    )
    assert not _is_schema_valid("EvidenceArtifact", over_max)
    with pytest.raises(ContractSemanticError, match="reason_comment exceeds the maximum"):
        sem_evidence.validate_evidence_artifact(EvidenceArtifact.from_wire(over_max))


def test_evidence_search_result_rejects_sensitivity_mismatch() -> None:
    result = EvidenceSearchResult.from_wire(
        {"evidence": [_evidence_artifact_wire(sensitivity="restricted")], "page": {}}
    )
    request = sem_evidence.decode_evidence_search_input(
        _evidence_search_input_wire(sensitivity="public")
    )
    with pytest.raises(ContractSemanticError, match="sensitivity"):
        sem_evidence.validate_evidence_search_result(result, request, "ws-1")


def test_evidence_search_result_rejects_evidence_count_over_request_limit() -> None:
    result = EvidenceSearchResult.from_wire(
        {
            "evidence": [
                _evidence_artifact_wire(evidence_id="ev-1"),
                _evidence_artifact_wire(evidence_id="ev-2"),
            ],
            "page": {},
        }
    )
    request = sem_evidence.decode_evidence_search_input(_evidence_search_input_wire(limit=1))
    with pytest.raises(ContractSemanticError, match="exceeding the applicable limit"):
        sem_evidence.validate_evidence_search_result(result, request, "ws-1")


@pytest.mark.parametrize(
    "call",
    [
        lambda: sem_evidence.validate_evidence_artifact({"not": "an artifact"}),
        lambda: sem_evidence.validate_evidence_artifact(None),
        lambda: sem_evidence.validate_evidence_search_input("not an input"),
        lambda: sem_evidence.validate_evidence_search_result("not a result", "not a request", "ws-1"),
        lambda: sem_evidence.validate_evidence_search_result(
            EvidenceSearchResult.from_wire({"evidence": [], "page": {}}), object(), "ws-1"
        ),
        lambda: sem_evidence.validate_evidence_search_result(
            EvidenceSearchResult.from_wire({"evidence": [], "page": {}}),
            sem_evidence.decode_evidence_search_input(_evidence_search_input_wire()),
            123,
        ),
    ],
)
def test_evidence_direct_helpers_reject_malformed_types_as_contract_semantic_error(
    call: Any,
) -> None:
    with pytest.raises(ContractSemanticError):
        call()


def test_evidence_reference_tolerant_decode_round_trips_and_ignores_nested_unknown_fields() -> None:
    wire = _evidence_reference_wire(
        source=_source_wire(kind="document", source_id="doc-1"),
        span={"pointer": "/body", "start_offset": 0, "end_offset": 10},
        excerpt="hello",
    )
    baseline = EvidenceReference.from_wire(wire)
    assert baseline.to_wire() == wire

    augmented = {**wire, "future_hint_field": "some-value"}
    augmented["source"] = {**augmented["source"], "future_hint_field": "some-value"}
    assert EvidenceReference.from_wire(augmented) == baseline


# --------------------------------------------------------------------------
# 3. knowledge.search adversarial cases: trust closure
# --------------------------------------------------------------------------


def _knowledge_search_request(**overrides: Any) -> KnowledgeSearchInput:
    return sem_knowledge.decode_knowledge_search_input(_knowledge_search_input_wire(**overrides))


def _knowledge_search_result(records: list[dict[str, Any]]) -> KnowledgeSearchResult:
    return KnowledgeSearchResult.from_wire({"records": records, "page": {}})


LEAKING_IDENTITIES = (
    {"layer": "l2", "governance_state": "candidate", "currentness": "current"},
    {"layer": "l1", "governance_state": "accepted", "currentness": "current"},
    {"layer": "l3", "governance_state": "accepted", "currentness": "current"},
    {"layer": "l4", "governance_state": "accepted", "currentness": "current"},
    {"layer": "l2", "governance_state": "accepted", "currentness": "superseded"},
    {"layer": "some_future_layer", "governance_state": "accepted", "currentness": "current"},
)


@pytest.mark.parametrize("overrides", LEAKING_IDENTITIES)
def test_knowledge_search_default_view_rejects_leaking_record(overrides: dict[str, str]) -> None:
    identity_kwargs = dict(overrides)
    reviewer = "reviewer-1"
    superseded_by = None
    if identity_kwargs.get("currentness") == "superseded":
        superseded_by = {"record_id": "rec-1", "version": "v2"}
    record = _governed_record_wire(
        authority_level="canonical",
        reviewer=reviewer,
        provenance=_provenance_wire(
            identity=_identity_wire(**identity_kwargs, superseded_by=superseded_by),
            temporal=_temporal_wire(superseded_at=T1 if superseded_by else None),
        ),
    )
    result = _knowledge_search_result([record])
    with pytest.raises(ContractSemanticError):
        sem_knowledge.validate_knowledge_search_result(result, _knowledge_search_request(), "ws-1", T0, set())


def test_knowledge_search_default_view_accepts_canonical_record() -> None:
    result = _knowledge_search_result([_canonical_knowledge_record_wire()])
    sem_knowledge.validate_knowledge_search_result(result, _knowledge_search_request(), "ws-1", T0, set())


def test_knowledge_search_default_view_rejects_canonical_unknown_authority() -> None:
    """A default-view record that is exactly l2/accepted/current but does not carry
    authority_level=='canonical' has not reached the citable-knowledge bar, regardless
    of otherwise being a validly governed accepted record."""
    record = _accepted_record_wire()  # authority_level="accepted", not "canonical"
    result = _knowledge_search_result([record])
    with pytest.raises(ContractSemanticError, match="authority_level"):
        sem_knowledge.validate_knowledge_search_result(result, _knowledge_search_request(), "ws-1", T0, set())


def test_knowledge_search_explicit_candidates_view_allows_candidate_record_with_capability() -> None:
    result = _knowledge_search_result([_candidate_knowledge_record_wire()])
    request = _knowledge_search_request(view="candidates")
    sem_knowledge.validate_knowledge_search_result(result, request, "ws-1", T0, {"candidates"})


def test_knowledge_search_candidate_view_without_capability_rejected() -> None:
    result = _knowledge_search_result([_candidate_knowledge_record_wire()])
    request = _knowledge_search_request(view="candidates")
    with pytest.raises(ContractSemanticError, match="authorized_views"):
        sem_knowledge.validate_knowledge_search_result(result, request, "ws-1", T0, set())


def test_knowledge_search_history_view_with_capability_allows_historical_record() -> None:
    result = _knowledge_search_result([_history_knowledge_record_wire()])
    request = _knowledge_search_request(view="history")
    sem_knowledge.validate_knowledge_search_result(result, request, "ws-1", T0, {"history"})


def test_knowledge_search_history_view_without_capability_rejected() -> None:
    result = _knowledge_search_result([_history_knowledge_record_wire()])
    request = _knowledge_search_request(view="history")
    with pytest.raises(ContractSemanticError, match="authorized_views"):
        sem_knowledge.validate_knowledge_search_result(result, request, "ws-1", T0, {"candidates"})


def test_knowledge_search_candidates_view_rejects_record_carrying_reviewer() -> None:
    record = _governed_record_wire(
        authority_level="proposed",
        reviewer="reviewer-1",
        provenance=_provenance_wire(identity=_identity_wire(layer="l1", governance_state="candidate", currentness="current")),
    )
    result = _knowledge_search_result([record])
    request = _knowledge_search_request(view="candidates")
    with pytest.raises(ContractSemanticError, match="reviewer"):
        sem_knowledge.validate_knowledge_search_result(result, request, "ws-1", T0, {"candidates"})


def test_knowledge_search_history_view_rejects_still_current_record() -> None:
    result = _knowledge_search_result([_accepted_record_wire()])
    request = _knowledge_search_request(view="history")
    with pytest.raises(ContractSemanticError, match="history"):
        sem_knowledge.validate_knowledge_search_result(result, request, "ws-1", T0, {"history"})


def test_knowledge_search_input_rejects_unknown_view_value() -> None:
    input_ = KnowledgeSearchInput.from_wire(_knowledge_search_input_wire(view="some_future_view"))
    with pytest.raises(ContractSemanticError, match="GovernedRecordView"):
        sem_knowledge.validate_knowledge_search_input(input_)


def test_knowledge_search_input_rejects_unknown_order_value() -> None:
    input_ = KnowledgeSearchInput.from_wire(_knowledge_search_input_wire(order="some_future_order"))
    with pytest.raises(ContractSemanticError, match="MemorySearchOrder"):
        sem_knowledge.validate_knowledge_search_input(input_)


def test_knowledge_search_input_accepts_known_order() -> None:
    input_ = KnowledgeSearchInput.from_wire(_knowledge_search_input_wire(order="recency"))
    sem_knowledge.validate_knowledge_search_input(input_)


def test_knowledge_search_rejects_workspace_mismatch() -> None:
    result = _knowledge_search_result([_canonical_knowledge_record_wire(workspace_id="ws-other")])
    with pytest.raises(ContractSemanticError, match="does not match"):
        sem_knowledge.validate_knowledge_search_result(result, _knowledge_search_request(), "ws-1", T0, set())


def test_knowledge_search_rejects_accepted_record_missing_reviewer() -> None:
    record = _governed_record_wire(authority_level="canonical", reviewer=None)
    result = _knowledge_search_result([record])
    with pytest.raises(ContractSemanticError, match="reviewer"):
        sem_knowledge.validate_knowledge_search_result(result, _knowledge_search_request(), "ws-1", T0, set())


def test_knowledge_search_rejects_record_type_filter_mismatch() -> None:
    """`knowledge.search` binds the request's filters: a record naming a different
    record_type than what was actually requested is a request-filter mismatch."""
    record = _canonical_knowledge_record_wire(record_type="memory.other")
    result = _knowledge_search_result([record])
    request = _knowledge_search_request(record_type="memory.fact")
    with pytest.raises(ContractSemanticError, match="record_type"):
        sem_knowledge.validate_knowledge_search_result(result, request, "ws-1", T0, set())


def test_knowledge_search_rejects_domain_scope_filter_mismatch() -> None:
    record = _canonical_knowledge_record_wire(domain_scope="personal.other")
    result = _knowledge_search_result([record])
    request = _knowledge_search_request(domain_scope="personal.preferences")
    with pytest.raises(ContractSemanticError, match="domain_scope"):
        sem_knowledge.validate_knowledge_search_result(result, request, "ws-1", T0, set())


def test_knowledge_search_rejects_malformed_canonical_resolution_time() -> None:
    result = _knowledge_search_result([_canonical_knowledge_record_wire()])
    with pytest.raises(ContractSemanticError):
        sem_knowledge.validate_knowledge_search_result(
            result, _knowledge_search_request(), "ws-1", "not-a-timestamp", set()
        )


def test_knowledge_search_rejects_record_not_yet_valid_at_resolution_time() -> None:
    record = _canonical_knowledge_record_wire(valid_from=T1)
    result = _knowledge_search_result([record])
    with pytest.raises(ContractSemanticError, match="not yet valid"):
        sem_knowledge.validate_knowledge_search_result(result, _knowledge_search_request(), "ws-1", T0, set())


def test_knowledge_search_rejects_record_no_longer_valid_at_resolution_time() -> None:
    record = _canonical_knowledge_record_wire(valid_until=T0)
    result = _knowledge_search_result([record])
    with pytest.raises(ContractSemanticError, match="no longer valid"):
        sem_knowledge.validate_knowledge_search_result(result, _knowledge_search_request(), "ws-1", T1, set())


def test_knowledge_search_accepts_record_within_validity_window() -> None:
    record = _canonical_knowledge_record_wire(valid_from=T0, valid_until=T1)
    result = _knowledge_search_result([record])
    sem_knowledge.validate_knowledge_search_result(result, _knowledge_search_request(), "ws-1", T0, set())


def test_knowledge_search_rejects_result_count_over_request_limit() -> None:
    records = [
        _canonical_knowledge_record_wire(record_id="rec-1"),
        _canonical_knowledge_record_wire(record_id="rec-2"),
    ]
    result = _knowledge_search_result(records)
    request = _knowledge_search_request(limit=1)
    with pytest.raises(ContractSemanticError, match="exceeding the applicable limit"):
        sem_knowledge.validate_knowledge_search_result(result, request, "ws-1", T0, set())


def test_decode_knowledge_search_input_tolerates_unknown_field_and_round_trips() -> None:
    wire = _knowledge_search_input_wire(query="hello", order="relevance")
    baseline = sem_knowledge.decode_knowledge_search_input(wire)
    augmented = {**wire, "future_hint_field": "some-value"}
    assert sem_knowledge.decode_knowledge_search_input(augmented) == baseline


@pytest.mark.parametrize(
    "call",
    [
        lambda: sem_knowledge.validate_knowledge_search_input("not an input"),
        lambda: sem_knowledge.validate_knowledge_search_input(None),
        lambda: sem_knowledge.validate_knowledge_search_result(
            "not a result", _knowledge_search_request(), "ws-1", T0, set()
        ),
        lambda: sem_knowledge.validate_knowledge_search_result(
            _knowledge_search_result([]), "not a request", "ws-1", T0, set()
        ),
        lambda: sem_knowledge.validate_knowledge_search_result(
            _knowledge_search_result([]), _knowledge_search_request(), 123, T0, set()
        ),
        lambda: sem_knowledge.validate_knowledge_search_result(
            _knowledge_search_result([]), _knowledge_search_request(), "ws-1", 12345, set()
        ),
        lambda: sem_knowledge.validate_knowledge_search_result(
            _knowledge_search_result([]), _knowledge_search_request(), "ws-1", T0, "candidates"
        ),
        lambda: sem_knowledge.validate_knowledge_propose_input({"record_id": "rec-1"}),
        lambda: sem_knowledge.validate_candidate_approve_input(None),
        lambda: sem_knowledge.validate_candidate_reject_input(123),
        lambda: sem_knowledge.validate_record_supersede_input("not an input"),
    ],
)
def test_knowledge_direct_helpers_reject_malformed_types_as_contract_semantic_error(call: Any) -> None:
    with pytest.raises(ContractSemanticError):
        call()


# --------------------------------------------------------------------------
# 4. Governance transition adversarial cases, shared across the four operations
# --------------------------------------------------------------------------

_UNSET = object()


class _Op(NamedTuple):
    """One governance transition, with everything a shared case needs to drive it."""

    name: str
    action: str
    build_pair: Callable[..., tuple[dict[str, Any], dict[str, Any]]]
    result_cls: Any
    validate: Callable[..., None]
    build_request: Callable[..., Any]
    trusted_actor: str
    reason_code: str


OPS: tuple[_Op, ...] = (
    _Op(
        "propose",
        "knowledge.propose",
        _propose_pair_wire,
        KnowledgeProposeResult,
        sem_knowledge.validate_knowledge_propose_result,
        _propose_request,
        "actor-1",
        "meets_bar",
    ),
    _Op(
        "approve",
        "candidate.approve",
        _approve_pair_wire,
        CandidateApproveResult,
        sem_knowledge.validate_candidate_approve_result,
        _approve_request,
        "reviewer-1",
        "meets_bar",
    ),
    _Op(
        "reject",
        "candidate.reject",
        _reject_pair_wire,
        CandidateRejectResult,
        sem_knowledge.validate_candidate_reject_result,
        _reject_request,
        "reviewer-1",
        "insufficient_evidence",
    ),
    _Op(
        "supersede",
        "record.supersede",
        _supersede_pair_wire,
        RecordSupersedeResult,
        sem_knowledge.validate_record_supersede_result,
        _supersede_request,
        "reviewer-1",
        "new_evidence",
    ),
)

OP_IDS = [op.name for op in OPS]

PRESERVING_OPS = OPS[:3]
"""`knowledge.propose` / `candidate.approve` / `candidate.reject`: the three transitions
that decide *about* a claim and may never edit it. `record.supersede` is the one that
replaces it, so every preservation case below deliberately excludes it."""

PRESERVING_OP_IDS = [op.name for op in PRESERVING_OPS]


def _check(
    op: _Op,
    prev: dict[str, Any],
    upd: dict[str, Any],
    *,
    request: Any = _UNSET,
    precondition: Any = _UNSET,
    workspace_id: Any = "ws-1",
    actor: Any = _UNSET,
    actor_kind: Any = "user",
) -> None:
    """Decode `prev`/`upd` into `op`'s result DTO and run its public result validator."""
    result = op.result_cls.from_wire({"previous_record": prev, "updated_record": upd})
    op.validate(
        result,
        op.build_request() if request is _UNSET else request,
        _precondition() if precondition is _UNSET else precondition,
        workspace_id,
        op.trusted_actor if actor is _UNSET else actor,
        actor_kind,
    )


# --- the canonical positive case for each transition -------------------------


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_accepts_canonical_pair(op: _Op) -> None:
    prev, upd = op.build_pair()
    _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_accepts_rationale_comment_carried_into_the_audit_event(op: _Op) -> None:
    """A rationale comment is not optional decoration on the event: when the request carries
    one, the appended event must carry exactly it."""
    prev, upd = op.build_pair(reason_comment="approved at the Tuesday review")
    request = op.build_request(
        rationale=_rationale_wire(
            reason_code=op.reason_code, comment="approved at the Tuesday review"
        )
    )
    _check(op, prev, upd, request=request)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_accepts_appended_event_after_existing_history(op: _Op) -> None:
    prev, upd = op.build_pair(prev_history=[_history_entry_wire(), _history_entry_wire(action="modified")])
    _check(op, prev, upd)


# --- request / precondition / workspace binding ------------------------------


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_request_naming_a_different_record(op: _Op) -> None:
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError, match="record_id"):
        _check(op, prev, upd, request=op.build_request(record_id="rec-other"))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_result_record_id_not_bound_to_the_request(op: _Op) -> None:
    prev, upd = op.build_pair(record_id="rec-elsewhere")
    with pytest.raises(ContractSemanticError, match="record_id"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_stale_precondition_version(op: _Op) -> None:
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError, match="precondition.record_version"):
        _check(op, prev, upd, precondition=_precondition("v0"))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_precondition_naming_the_updated_version(op: _Op) -> None:
    """The precondition targets the version being replaced, never the one produced."""
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError, match="precondition.record_version"):
        _check(op, prev, upd, precondition=_precondition("v2"))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
@pytest.mark.parametrize(
    "precondition", [None, "v1", {"record_version": "v1"}, 123, object()], ids=range(5)
)
def test_transition_rejects_malformed_precondition(op: _Op, precondition: Any) -> None:
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError, match="precondition"):
        _check(op, prev, upd, precondition=precondition)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_malformed_precondition_record_version(op: _Op) -> None:
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError, match="record_version"):
        _check(op, prev, upd, precondition=MutationPrecondition(record_version="not a token"))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_workspace_mismatch(op: _Op) -> None:
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError, match="workspace"):
        _check(op, prev, upd, workspace_id="ws-other")


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_updated_record_in_a_foreign_workspace(op: _Op) -> None:
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError, match="workspace"):
        _check(op, prev, _with(upd, "workspace_id", "ws-other"))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
@pytest.mark.parametrize("workspace_id", [None, "", "not a workspace id", 123], ids=range(4))
def test_transition_rejects_malformed_expected_workspace_id(op: _Op, workspace_id: Any) -> None:
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError, match="expected_workspace_id"):
        _check(op, prev, upd, workspace_id=workspace_id)


# --- version / reciprocity ---------------------------------------------------


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_identical_versions(op: _Op) -> None:
    prev, upd = op.build_pair(prev_version="v1", upd_version="v1")
    with pytest.raises(ContractSemanticError):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_missing_forward_pointer(op: _Op) -> None:
    prev, upd = op.build_pair()
    prev = _with(prev, "provenance.identity.superseded_by", _DELETE)
    with pytest.raises(ContractSemanticError, match="superseded"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_forward_pointer_to_a_foreign_record(op: _Op) -> None:
    prev, upd = op.build_pair()
    prev = _with(
        prev,
        "provenance.identity.superseded_by",
        {"record_id": "rec-other", "version": "v2", "reason": op.reason_code},
    )
    with pytest.raises(ContractSemanticError, match="superseded_by"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_forward_pointer_to_the_wrong_version(op: _Op) -> None:
    prev, upd = op.build_pair()
    prev = _with(
        prev,
        "provenance.identity.superseded_by",
        {"record_id": "rec-1", "version": "v-wrong", "reason": op.reason_code},
    )
    with pytest.raises(ContractSemanticError, match="superseded_by"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_forward_pointer_with_no_reason(op: _Op) -> None:
    prev, upd = op.build_pair()
    prev = _with(
        prev, "provenance.identity.superseded_by", {"record_id": "rec-1", "version": "v2"}
    )
    with pytest.raises(ContractSemanticError, match="superseded_by.reason"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_forward_pointer_reason_disagreeing_with_the_rationale(op: _Op) -> None:
    prev, upd = op.build_pair()
    prev = _with(
        prev,
        "provenance.identity.superseded_by",
        {"record_id": "rec-1", "version": "v2", "reason": "some_other_reason"},
    )
    with pytest.raises(ContractSemanticError, match="superseded_by.reason"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_missing_backward_pointer(op: _Op) -> None:
    prev, upd = op.build_pair()
    upd = _with(upd, "provenance.identity.supersedes", _DELETE)
    with pytest.raises(ContractSemanticError, match="supersedes"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_backward_pointer_to_the_wrong_version(op: _Op) -> None:
    prev, upd = op.build_pair()
    upd = _with(
        upd,
        "provenance.identity.supersedes",
        {"record_id": "rec-1", "version": "v-wrong", "reason": op.reason_code},
    )
    with pytest.raises(ContractSemanticError, match="supersedes"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_backward_pointer_reason_disagreeing_with_the_rationale(op: _Op) -> None:
    prev, upd = op.build_pair()
    upd = _with(
        upd,
        "provenance.identity.supersedes",
        {"record_id": "rec-1", "version": "v1", "reason": "some_other_reason"},
    )
    with pytest.raises(ContractSemanticError, match="supersedes.reason"):
        _check(op, prev, upd)


# --- exact state / authority / reviewer matrices -----------------------------

WRONG_PRIOR_STATES: tuple[dict[str, Any], ...] = (
    {"prev_layer": "l0"},
    {"prev_layer": "l3"},
    {"prev_state": "some_future_state"},
    {"prev_state": "retracted"},
)
"""Prior states no transition may start from, whichever operation it is: the raw-evidence
and context-model layers, and a governance state this build has never seen."""


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
@pytest.mark.parametrize("overrides", WRONG_PRIOR_STATES, ids=range(len(WRONG_PRIOR_STATES)))
def test_transition_rejects_universally_wrong_prior_state(
    op: _Op, overrides: dict[str, Any]
) -> None:
    prev, upd = op.build_pair(**overrides)
    with pytest.raises(ContractSemanticError):
        _check(op, prev, upd)


WRONG_PRIOR_STATES_BY_OP: tuple[tuple[str, dict[str, Any]], ...] = (
    ("propose", {"prev_layer": "l2"}),
    ("propose", {"prev_state": "candidate"}),
    ("propose", {"prev_state": "accepted"}),
    ("approve", {"prev_layer": "l2"}),
    ("approve", {"prev_state": "proposed"}),
    ("approve", {"prev_state": "rejected"}),
    ("reject", {"prev_layer": "l2"}),
    ("reject", {"prev_state": "proposed"}),
    ("reject", {"prev_state": "accepted"}),
    ("supersede", {"prev_layer": "l1"}),
    ("supersede", {"prev_state": "candidate"}),
    ("supersede", {"prev_state": "rejected"}),
)
"""Each operation's own wrong starting points -- including the *other* operations' correct
ones, so no transition can be driven from a state that belongs to a different step."""


@pytest.mark.parametrize(
    "op_name,overrides",
    WRONG_PRIOR_STATES_BY_OP,
    ids=[f"{name}-{index}" for index, (name, _) in enumerate(WRONG_PRIOR_STATES_BY_OP)],
)
def test_transition_rejects_operation_specific_wrong_prior_state(
    op_name: str, overrides: dict[str, Any]
) -> None:
    op = next(candidate for candidate in OPS if candidate.name == op_name)
    prev, upd = op.build_pair(**overrides)
    with pytest.raises(ContractSemanticError):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_prior_record_still_current(op: _Op) -> None:
    prev, upd = op.build_pair()
    prev = _with(prev, "provenance.identity.currentness", "current")
    with pytest.raises(ContractSemanticError):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_updated_record_not_current(op: _Op) -> None:
    prev, upd = op.build_pair()
    upd = _with(upd, "provenance.identity.currentness", "superseded")
    with pytest.raises(ContractSemanticError):
        _check(op, prev, upd)


WRONG_UPDATED_AUTHORITY_LEVELS: tuple[tuple[str, str], ...] = (
    ("propose", "some_future_authority"),
    ("approve", "accepted"),
    ("reject", "declined"),
    ("supersede", "accepted"),
)
"""Authority levels that are perfectly coherent with the rest of the record -- so generic
governed-record validation lets them through -- but are not the *exact* level the operation
must produce. `accepted` authority on an approved record is the sharpest case: it is
decision-bearing and reviewer-backed, and still not `canonical`."""


@pytest.mark.parametrize(
    "op_name,authority_level", WRONG_UPDATED_AUTHORITY_LEVELS, ids=[name for name, _ in WRONG_UPDATED_AUTHORITY_LEVELS]
)
def test_transition_rejects_wrong_updated_authority_level(op_name: str, authority_level: str) -> None:
    op = next(candidate for candidate in OPS if candidate.name == op_name)
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError, match="authority_level"):
        _check(op, prev, _with(upd, "authority_level", authority_level))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_wrong_prior_authority_level(op: _Op) -> None:
    prev, upd = op.build_pair()
    prev = _with(prev, "authority_level", "some_future_authority")
    with pytest.raises(ContractSemanticError, match="authority_level"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS[1:], ids=OP_IDS[1:])
def test_transition_rejects_updated_reviewer_other_than_the_trusted_one(op: _Op) -> None:
    """approve/reject/supersede attribute the decision to the caller's trusted reviewer, so a
    result naming anyone else is rejected even though that reviewer is itself well formed."""
    prev, upd = op.build_pair(upd_reviewer="reviewer-impostor", actor_id="reviewer-impostor")
    with pytest.raises(ContractSemanticError, match="reviewer"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS[1:], ids=OP_IDS[1:])
def test_transition_rejects_updated_record_with_no_reviewer(op: _Op) -> None:
    prev, upd = op.build_pair(upd_reviewer=None)
    with pytest.raises(ContractSemanticError, match="reviewer"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS[:3], ids=OP_IDS[:3])
def test_transition_rejects_prior_record_already_carrying_a_reviewer(op: _Op) -> None:
    """propose/approve/reject all act on a record no reviewer has decided on yet."""
    prev, upd = op.build_pair(prev_reviewer="reviewer-9")
    with pytest.raises(ContractSemanticError, match="reviewer"):
        _check(op, prev, upd)


def test_propose_rejects_updated_record_carrying_a_reviewer() -> None:
    """Putting a record forward for a decision is not itself a decision."""
    op = OPS[0]
    prev, upd = op.build_pair(upd_reviewer="reviewer-1")
    with pytest.raises(ContractSemanticError, match="reviewer"):
        _check(op, prev, upd)


def test_supersede_rejects_prior_record_with_no_reviewer() -> None:
    """The record being superseded was accepted, so somebody accepted it."""
    op = OPS[3]
    prev, upd = op.build_pair(prev_reviewer=None)
    with pytest.raises(ContractSemanticError, match="reviewer"):
        _check(op, prev, upd)


def test_supersede_accepts_prior_reviewer_other_than_the_superseding_one() -> None:
    """The prior version keeps whoever originally approved it; only the new version's
    reviewer must be the caller's trusted identity."""
    op = OPS[3]
    prev, upd = op.build_pair(prev_reviewer="someone-else-entirely")
    _check(op, prev, upd)


EXACT_UPDATED_STATES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("propose", {"upd_layer": "l2"}),
    ("propose", {"upd_state": "accepted", "upd_reviewer": "actor-1"}),
    ("approve", {"upd_layer": "l1"}),
    ("approve", {"upd_state": "candidate"}),
    ("reject", {"upd_layer": "l2"}),
    ("reject", {"upd_state": "accepted"}),
    ("supersede", {"upd_layer": "l1"}),
    ("supersede", {"upd_state": "candidate"}),
    # Coherent with the rest of the record, and still not the exact expected position:
    # generic governed-record validation passes these, only the frozen matrix rejects them.
    ("approve", {"upd_state": "reviewed"}),
    ("supersede", {"upd_state": "some_future_state"}),
)
"""Each operation's wrong destinations. The first entries are contradictions generic
governed-record validation would catch on its own; the last two are not -- they are fully
coherent records sitting at the wrong point in the workflow, and only the frozen per-operation
matrix rejects them."""


@pytest.mark.parametrize(
    "op_name,overrides", EXACT_UPDATED_STATES, ids=[f"{name}-{i}" for i, (name, _) in enumerate(EXACT_UPDATED_STATES)]
)
def test_transition_rejects_wrong_updated_state(op_name: str, overrides: dict[str, Any]) -> None:
    op = next(candidate for candidate in OPS if candidate.name == op_name)
    prev, upd = op.build_pair(**overrides)
    with pytest.raises(ContractSemanticError):
        _check(op, prev, upd)


# --- one transition instant --------------------------------------------------


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_updated_recorded_at_disagreeing_with_superseded_at(op: _Op) -> None:
    prev, upd = op.build_pair()
    upd = _with(upd, "provenance.temporal.recorded_at", "2024-01-03T00:00:00Z")
    with pytest.raises(ContractSemanticError, match="recorded_at"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_event_occurred_at_disagreeing_with_the_instant(op: _Op) -> None:
    prev, upd = op.build_pair()
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1]["occurred_at"] = "2024-01-03T00:00:00Z"
    with pytest.raises(ContractSemanticError, match="occurred_at"):
        _check(op, prev, _with(upd, "provenance.history", history))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_prior_recorded_after_the_transition_instant(op: _Op) -> None:
    prev, upd = op.build_pair(prev_recorded_at="2024-01-05T00:00:00Z", transition_at="2024-01-04T00:00:00Z")
    with pytest.raises(ContractSemanticError):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_accepts_prior_recorded_exactly_at_the_transition_instant(op: _Op) -> None:
    """A record superseded the same instant it was written is unusual, not impossible."""
    prev, upd = op.build_pair(prev_recorded_at=T1, transition_at=T1)
    _check(op, prev, upd)


# --- exactly one appended audit event ----------------------------------------


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_history_with_no_appended_event(op: _Op) -> None:
    prev, upd = op.build_pair(prev_history=[_history_entry_wire()], upd_history=[_history_entry_wire()])
    with pytest.raises(ContractSemanticError, match="exactly one"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_completely_empty_updated_history(op: _Op) -> None:
    prev, upd = op.build_pair(upd_history=[])
    with pytest.raises(ContractSemanticError, match="exactly one"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_two_appended_events(op: _Op) -> None:
    prev, upd = op.build_pair()
    history = [*upd["provenance"]["history"], copy.deepcopy(upd["provenance"]["history"][-1])]
    with pytest.raises(ContractSemanticError, match="exactly one"):
        _check(op, prev, _with(upd, "provenance.history", history))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_erased_history(op: _Op) -> None:
    prev, upd = op.build_pair(prev_history=[_history_entry_wire(), _history_entry_wire(action="modified")])
    with pytest.raises(ContractSemanticError):
        _check(op, prev, _with(upd, "provenance.history", upd["provenance"]["history"][1:]))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_modified_history_prefix(op: _Op) -> None:
    prev, upd = op.build_pair(prev_history=[_history_entry_wire()])
    history = copy.deepcopy(upd["provenance"]["history"])
    history[0]["actor_id"] = "rewritten-actor"
    with pytest.raises(ContractSemanticError, match="prefix"):
        _check(op, prev, _with(upd, "provenance.history", history))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_reordered_history_prefix(op: _Op) -> None:
    prev, upd = op.build_pair(
        prev_history=[_history_entry_wire(), _history_entry_wire(action="modified", occurred_at=T1)]
    )
    history = copy.deepcopy(upd["provenance"]["history"])
    history[0], history[1] = history[1], history[0]
    with pytest.raises(ContractSemanticError, match="prefix"):
        _check(op, prev, _with(upd, "provenance.history", history))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_event_naming_another_operations_action(op: _Op) -> None:
    other = next(candidate for candidate in OPS if candidate.name != op.name)
    prev, upd = op.build_pair()
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1]["action"] = other.action
    with pytest.raises(ContractSemanticError, match="action"):
        _check(op, prev, _with(upd, "provenance.history", history))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_event_naming_a_vague_action(op: _Op) -> None:
    prev, upd = op.build_pair()
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1]["action"] = "modified"
    with pytest.raises(ContractSemanticError, match="action"):
        _check(op, prev, _with(upd, "provenance.history", history))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_event_actor_other_than_the_trusted_one(op: _Op) -> None:
    prev, upd = op.build_pair()
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1]["actor_id"] = "someone-else"
    with pytest.raises(ContractSemanticError, match="actor_id"):
        _check(op, prev, _with(upd, "provenance.history", history))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_event_actor_kind_other_than_the_trusted_one(op: _Op) -> None:
    prev, upd = op.build_pair()
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1]["actor_kind"] = "agent"
    with pytest.raises(ContractSemanticError, match="actor_kind"):
        _check(op, prev, _with(upd, "provenance.history", history))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_event_missing_the_reason_code(op: _Op) -> None:
    prev, upd = op.build_pair()
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1].pop("reason_code")
    with pytest.raises(ContractSemanticError, match="reason_code"):
        _check(op, prev, _with(upd, "provenance.history", history))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_event_reason_code_disagreeing_with_the_rationale(op: _Op) -> None:
    prev, upd = op.build_pair()
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1]["reason_code"] = "some_other_reason"
    with pytest.raises(ContractSemanticError, match="reason_code"):
        _check(op, prev, _with(upd, "provenance.history", history))


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_event_inventing_a_reason_comment(op: _Op) -> None:
    """An absent comment must stay absent: a server may not fill one in on the caller's
    behalf, because the audit trail would then attribute words to them they never wrote."""
    prev, upd = op.build_pair(reason_comment="invented by the server")
    with pytest.raises(ContractSemanticError, match="reason_comment"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_event_dropping_the_requested_reason_comment(op: _Op) -> None:
    prev, upd = op.build_pair()
    request = op.build_request(
        rationale=_rationale_wire(reason_code=op.reason_code, comment="the reason it happened")
    )
    with pytest.raises(ContractSemanticError, match="reason_comment"):
        _check(op, prev, upd, request=request)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_rejects_event_altering_the_requested_reason_comment(op: _Op) -> None:
    prev, upd = op.build_pair(reason_comment="a subtly different comment")
    request = op.build_request(
        rationale=_rationale_wire(reason_code=op.reason_code, comment="the reason it happened")
    )
    with pytest.raises(ContractSemanticError, match="reason_comment"):
        _check(op, prev, upd, request=request)


# --- transition-event evidence -----------------------------------------------


@pytest.mark.parametrize("op", PRESERVING_OPS, ids=PRESERVING_OP_IDS)
def test_decision_transition_rejects_event_asserting_new_evidence(op: _Op) -> None:
    """Deciding on a candidate weighs the evidence already there; it never adds any."""
    prev, upd = op.build_pair(event_evidence=[_evidence_reference_wire()])
    with pytest.raises(ContractSemanticError, match="evidence"):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", PRESERVING_OPS, ids=PRESERVING_OP_IDS)
def test_decision_transition_accepts_explicitly_empty_event_evidence(op: _Op) -> None:
    prev, upd = op.build_pair(event_evidence=[])
    _check(op, prev, upd)


def test_supersede_accepts_event_evidence_matching_the_replacement() -> None:
    op = OPS[3]
    replacement = _replacement_wire(
        assertion=_assertion_wire(
            evidence=[
                _evidence_reference_wire(
                    source=_source_wire(source_id="doc-2"),
                    span={"pointer": "/body", "start_offset": 10, "end_offset": 40},
                    excerpt="the sentence that changed things",
                ),
                _evidence_reference_wire(
                    source=_source_wire(source_id="doc-2"), span={"pointer": "/footnote"}
                ),
            ]
        )
    )
    prev, upd = op.build_pair(replacement=replacement)
    _check(op, prev, upd, request=_supersede_request(replacement=replacement))


def test_supersede_rejects_event_with_no_evidence() -> None:
    op = OPS[3]
    prev, upd = op.build_pair()
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1].pop("evidence")
    with pytest.raises(ContractSemanticError, match="evidence"):
        _check(op, prev, _with(upd, "provenance.history", history))


def test_supersede_rejects_event_evidence_from_a_different_source() -> None:
    op = OPS[3]
    prev, upd = op.build_pair()
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1]["evidence"] = [_evidence_reference_wire(source=_source_wire(source_id="doc-2"), excerpt="x")]
    with pytest.raises(ContractSemanticError, match="evidence"):
        _check(op, prev, _with(upd, "provenance.history", history))


SUPERSEDE_EVIDENCE_MUTATIONS: tuple[dict[str, Any], ...] = (
    {"span": {"pointer": "/body", "start_offset": 11, "end_offset": 40}},
    {"span": {"pointer": "/other", "start_offset": 10, "end_offset": 40}},
    {"excerpt": "a quietly reworded excerpt"},
    {"source": _source_wire(source_id="doc-2", locator="/elsewhere")},
)


@pytest.mark.parametrize(
    "mutation", SUPERSEDE_EVIDENCE_MUTATIONS, ids=range(len(SUPERSEDE_EVIDENCE_MUTATIONS))
)
def test_supersede_rejects_event_evidence_with_any_field_mutated(mutation: dict[str, Any]) -> None:
    """Equality is over the complete `EvidenceReference`, so a moved span, a reworded
    excerpt, or a re-pointed source locator is caught as loudly as a missing item."""
    op = OPS[3]
    evidence = _evidence_reference_wire(
        source=_source_wire(source_id="doc-2", locator="/body"),
        span={"pointer": "/body", "start_offset": 10, "end_offset": 40},
        excerpt="the sentence that changed things",
    )
    replacement = _replacement_wire(
        sources=[_source_wire(source_id="doc-2", locator="/body")],
        assertion=_assertion_wire(evidence=[evidence]),
    )
    prev, upd = op.build_pair(replacement=replacement)
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1]["evidence"] = [{**evidence, **mutation}]
    with pytest.raises(ContractSemanticError):
        _check(
            op,
            prev,
            _with(upd, "provenance.history", history),
            request=_supersede_request(replacement=replacement),
        )


def test_supersede_rejects_reordered_event_evidence() -> None:
    op = OPS[3]
    first = _evidence_reference_wire(source=_source_wire(source_id="doc-2"), excerpt="first")
    second = _evidence_reference_wire(source=_source_wire(source_id="doc-2"), excerpt="second")
    replacement = _replacement_wire(assertion=_assertion_wire(evidence=[first, second]))
    prev, upd = op.build_pair(replacement=replacement)
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1]["evidence"] = [second, first]
    with pytest.raises(ContractSemanticError, match="evidence"):
        _check(
            op,
            prev,
            _with(upd, "provenance.history", history),
            request=_supersede_request(replacement=replacement),
        )


# --- assertion lineage is never dropped --------------------------------------


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
@pytest.mark.parametrize("side", ["previous_record", "updated_record"])
def test_transition_rejects_missing_assertion_lineage(op: _Op, side: str) -> None:
    prev, upd = op.build_pair()
    if side == "previous_record":
        prev = _with(prev, "provenance.assertion", _DELETE)
    else:
        upd = _with(upd, "provenance.assertion", _DELETE)
    with pytest.raises(ContractSemanticError, match="assertion"):
        _check(op, prev, upd)


# --- preserved lineage must be valid, not merely identical on both sides ------
#
# A transition validator can only observe that `previous` and `updated` preserved the claim
# *unchanged*; it cannot tell valid lineage from lineage that is equally malformed on both
# sides. Every case below is exactly that: the same broken assertion/extraction on both
# records, preserved perfectly. They pass every preservation, reciprocity, and event rule,
# and must still be rejected -- by `validate_record_provenance`, which now runs the same
# lineage rules `memory.create` runs on the input the lineage came from.

MALFORMED_PRESERVED_LINEAGE: tuple[tuple[str, dict[str, Any]], ...] = (
    ("assertion_actor_id", {"assertion": _assertion_wire(actor_id="not an identifier!")}),
    ("assertion_actor_kind", {"assertion": _assertion_wire(actor_kind="Not A Kind")}),
    ("assertion_actor_role", {"assertion": _assertion_wire(actor_role="Not A Role")}),
    ("assertion_asserted_at", {"assertion": _assertion_wire(asserted_at="2024-01-01")}),
    (
        "assertion_proposed_window_reversed",
        {"assertion": _assertion_wire(proposed_valid_from=T1, proposed_valid_until=T0)},
    ),
    (
        "assertion_proposed_valid_from_malformed",
        {"assertion": _assertion_wire(proposed_valid_from="yesterday")},
    ),
    (
        "assertion_evidence_source_not_declared",
        {
            "sources": [_source_wire(source_id="doc-1")],
            "assertion": _assertion_wire(
                evidence=[_evidence_reference_wire(source=_source_wire(source_id="doc-99"))]
            ),
        },
    ),
    (
        "assertion_evidence_locator_conflicts",
        {
            "sources": [_source_wire(locator="/body")],
            "assertion": _assertion_wire(
                evidence=[_evidence_reference_wire(source=_source_wire(locator="/elsewhere"))]
            ),
        },
    ),
    (
        "assertion_evidence_malformed_source_id",
        {
            "assertion": _assertion_wire(
                evidence=[
                    _evidence_reference_wire(source=_source_wire(source_id="not an id!"))
                ]
            )
        },
    ),
    (
        "assertion_evidence_reversed_span",
        {
            "assertion": _assertion_wire(
                evidence=[
                    _evidence_reference_wire(
                        span={"pointer": "p", "start_offset": 9, "end_offset": 2}
                    )
                ]
            )
        },
    ),
    (
        "assertion_evidence_negative_offset",
        {
            "assertion": _assertion_wire(
                evidence=[_evidence_reference_wire(span={"pointer": "p", "start_offset": -1})]
            )
        },
    ),
    (
        "assertion_duplicate_evidence",
        {
            "assertion": _assertion_wire(
                evidence=[
                    _evidence_reference_wire(excerpt="same"),
                    _evidence_reference_wire(excerpt="same"),
                ]
            )
        },
    ),
    (
        "assertion_evidence_missing_under_available_disposition",
        {"assertion": _assertion_wire(evidence=[])},
    ),
    ("extraction_extractor_id", {"extraction": _extraction_wire(extractor_id="not an id!")}),
    (
        "extraction_extractor_version",
        {"extraction": _extraction_wire(extractor_version="not an id!")},
    ),
    ("extraction_extracted_at", {"extraction": _extraction_wire(extracted_at="yesterday")}),
    (
        "extraction_reconciliation_state",
        {"extraction": _extraction_wire(reconciliation_state="Not A State")},
    ),
    ("extraction_confidence_above_one", {"extraction": _extraction_wire(confidence=1.5)}),
    ("extraction_confidence_below_zero", {"extraction": _extraction_wire(confidence=-0.1)}),
)


@pytest.mark.parametrize("op", PRESERVING_OPS, ids=PRESERVING_OP_IDS)
@pytest.mark.parametrize(
    "label,overrides",
    MALFORMED_PRESERVED_LINEAGE,
    ids=[label for label, _ in MALFORMED_PRESERVED_LINEAGE],
)
def test_decision_transition_rejects_identically_malformed_preserved_lineage(
    op: _Op, label: str, overrides: dict[str, Any]
) -> None:
    prev, upd = op.build_pair(**overrides)
    with pytest.raises(ContractSemanticError):
        _check(op, prev, upd)


PRIOR_ONLY_MALFORMED_LINEAGE = tuple(
    (label, overrides)
    for label, overrides in MALFORMED_PRESERVED_LINEAGE
    if "extraction" not in overrides
)
"""The subset of :data:`MALFORMED_PRESERVED_LINEAGE` that lands on the *prior* record alone
under `record.supersede`.

`_supersede_pair_wire` pins the updated record's assertion and sources to the replacement's,
so those overrides touch only the prior side. An `extraction` override does not: the default
replacement carries none, so the pair builder mirrors it onto both sides and the rejection
would come from the replacement-binding mismatch instead of from the prior record's lineage.
Those cases are excluded here so this test asserts exactly what it claims to.
"""


@pytest.mark.parametrize(
    "label,overrides",
    PRIOR_ONLY_MALFORMED_LINEAGE,
    ids=[label for label, _ in PRIOR_ONLY_MALFORMED_LINEAGE],
)
def test_supersede_rejects_malformed_lineage_carried_by_the_superseded_record(
    label: str, overrides: dict[str, Any]
) -> None:
    """The prior record's own lineage is validated too. `record.supersede` replaces the
    claim, so the malformed lineage here sits on the record being replaced -- which the
    replacement-binding rules never look at, and which must still be rejected."""
    op = OPS[3]
    prev, upd = op.build_pair(**overrides)
    with pytest.raises(ContractSemanticError):
        _check(op, prev, upd)


def test_decision_transition_accepts_valid_preserved_lineage_with_full_extraction() -> None:
    """The control for the cases above: identical, *valid* lineage on both sides passes."""
    for op in PRESERVING_OPS:
        prev, upd = op.build_pair(
            assertion=_assertion_wire(
                evidence=[_evidence_reference_wire(span={"pointer": "p", "start_offset": 0})],
                proposed_valid_from=T0,
                proposed_valid_until="2030-01-01T00:00:00Z",
            ),
            extraction=_extraction_wire(
                extractor_version="v3", confidence=0.75, reconciliation_state="some_future.state"
            ),
        )
        _check(op, prev, upd)


# --- history may cite a source only an older version declared -----------------


def test_supersede_accepts_old_source_history_evidence_across_the_supersession() -> None:
    """The reproduction of the blocker, as a positive.

    The record's existing history cites `doc-1` -- the source the version being replaced
    drew on. The replacement draws on `doc-7` and nothing else, so the new current version
    declares exactly `doc-7`. Requiring every historical event's evidence to appear in the
    *current* version's `sources` would make this legitimate supersession impossible: the
    only way to satisfy it would be to keep declaring a source the new claim does not use,
    or to rewrite append-only history.
    """
    op = OPS[3]
    old_source = _source_wire(source_id="doc-1")
    new_source = _source_wire(source_id="doc-7")
    prior_history = [
        _history_entry_wire(
            action="created",
            evidence=[_evidence_reference_wire(source=old_source, excerpt="cited back then")],
        ),
        _history_entry_wire(action="candidate.approve", evidence=[]),
    ]
    replacement = _replacement_wire(
        sources=[new_source],
        assertion=_assertion_wire(
            evidence=[_evidence_reference_wire(source=new_source, excerpt="fresh")]
        ),
    )
    prev, upd = op.build_pair(
        replacement=replacement,
        sources=[old_source],
        assertion=_assertion_wire(evidence=[_evidence_reference_wire(source=old_source)]),
        prev_history=prior_history,
    )

    assert upd["provenance"]["sources"] == [new_source]
    assert upd["provenance"]["history"][0]["evidence"][0]["source"] == old_source
    assert _is_schema_valid("RecordSupersedeResult", {"previous_record": prev, "updated_record": upd})
    _check(op, prev, upd, request=_supersede_request(replacement=replacement))


def test_supersede_still_rejects_a_new_event_citing_the_old_source() -> None:
    """Relaxing the rule for *preserved* history never relaxes it for the event this
    transition appends: that one is pinned by exact equality to the replacement's own
    assertion evidence."""
    op = OPS[3]
    old_source = _source_wire(source_id="doc-1")
    new_source = _source_wire(source_id="doc-7")
    replacement = _replacement_wire(
        sources=[new_source],
        assertion=_assertion_wire(evidence=[_evidence_reference_wire(source=new_source)]),
    )
    prev, upd = op.build_pair(replacement=replacement, sources=[old_source])
    history = copy.deepcopy(upd["provenance"]["history"])
    history[-1]["evidence"] = [_evidence_reference_wire(source=old_source)]
    with pytest.raises(ContractSemanticError, match="evidence"):
        _check(
            op,
            prev,
            _with(upd, "provenance.history", history),
            request=_supersede_request(replacement=replacement),
        )


def test_supersede_still_rejects_history_evidence_that_is_intrinsically_invalid() -> None:
    """Old-source history is exempt from *current-source agreement*, never from being a
    well-formed `EvidenceReference`."""
    op = OPS[3]
    prev, upd = op.build_pair(
        prev_history=[
            _history_entry_wire(
                evidence=[
                    _evidence_reference_wire(
                        source=_source_wire(source_id="doc-1"),
                        span={"pointer": "p", "start_offset": 10, "end_offset": 1},
                    )
                ]
            )
        ]
    )
    with pytest.raises(ContractSemanticError, match="start_offset"):
        _check(op, prev, upd)


# --- assertion and event evidence share one cardinality bound -----------------

EVIDENCE_MAX_ITEMS = 256
"""The `maxItems` both `CandidateAssertion.evidence` and `ProvenanceEntry.evidence` declare."""


def _bulk_evidence(count: int) -> list[dict[str, Any]]:
    """`count` distinct references into one declared source, distinguished by excerpt."""
    source = _source_wire(source_id="doc-2")
    return [_evidence_reference_wire(source=source, excerpt=f"excerpt-{i}") for i in range(count)]


def _bulk_supersede(count: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """A `record.supersede` request/result triple whose replacement carries `count` pieces of
    evidence -- and whose appended event therefore echoes all `count` of them."""
    replacement = _replacement_wire(assertion=_assertion_wire(evidence=_bulk_evidence(count)))
    prev, upd = _supersede_pair_wire(replacement=replacement)
    return replacement, prev, upd


@pytest.mark.parametrize("count", [1, 64, 65, EVIDENCE_MAX_ITEMS])
def test_supersede_accepts_evidence_cardinality_on_every_path(count: int) -> None:
    """64 was `ProvenanceEntry.evidence`'s old bound while `CandidateAssertion.evidence`
    allowed 256, so a replacement carrying 65+ pieces of evidence produced an event the
    strict result schema rejected. All three paths must now agree at each boundary."""
    replacement, prev, upd = _bulk_supersede(count)
    request_wire = {
        "record_id": "rec-1",
        "replacement": replacement,
        "rationale": _rationale_wire(reason_code="new_evidence"),
    }
    result_wire = {"previous_record": prev, "updated_record": upd}

    assert _is_schema_valid("RecordSupersedeInput", request_wire)
    assert _is_schema_valid("RecordSupersedeResult", result_wire)
    assert len(upd["provenance"]["history"][-1]["evidence"]) == count
    _check(OPS[3], prev, upd, request=_supersede_request(replacement=replacement))


def test_supersede_rejects_evidence_cardinality_over_the_bound_on_every_path() -> None:
    count = EVIDENCE_MAX_ITEMS + 1
    replacement, prev, upd = _bulk_supersede(count)
    request_wire = {
        "record_id": "rec-1",
        "replacement": replacement,
        "rationale": _rationale_wire(reason_code="new_evidence"),
    }
    result_wire = {"previous_record": prev, "updated_record": upd}

    assert not _is_schema_valid("RecordSupersedeInput", request_wire)
    assert not _is_schema_valid("RecordSupersedeResult", result_wire)
    with pytest.raises(ContractSemanticError, match="exceeding the maximum"):
        sem_knowledge.validate_record_supersede_input(
            RecordSupersedeInput.from_wire(request_wire)
        )
    with pytest.raises(ContractSemanticError, match="exceeding the maximum"):
        _check(OPS[3], prev, upd, request=_supersede_request(replacement=replacement))


# --- append-only history has no finite inline cap ------------------------------


@pytest.mark.parametrize("prior_entries", [1023, 1024])
def test_transition_accepts_history_growing_past_the_removed_cap(prior_entries: int) -> None:
    """`RecordProvenance.history` used to declare `maxItems: 1024`, which -- against a frozen
    "complete history, exactly one new event per transition" contract -- made a record with
    1024 entries impossible to transition at all. Both the strict schema and the semantic
    validator must accept 1023 -> 1024 and 1024 -> 1025."""
    op = OPS[0]
    prev, upd = op.build_pair(prev_history=[_history_entry_wire()] * prior_entries)

    assert len(prev["provenance"]["history"]) == prior_entries
    assert len(upd["provenance"]["history"]) == prior_entries + 1
    assert _is_schema_valid("KnowledgeProposeResult", {"previous_record": prev, "updated_record": upd})
    _check(op, prev, upd)


def test_record_provenance_history_declares_no_max_items() -> None:
    records = json.loads((SCHEMA_DIR / "records.schema.json").read_text(encoding="utf-8"))
    history = records["$defs"]["RecordProvenance"]["properties"]["history"]
    assert "maxItems" not in history
    assert "history" in records["$defs"]["RecordProvenance"]["required"]


# --- claim preservation for propose / approve / reject ------------------------

PRESERVED_MUTATIONS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("content", {"upd_content": {"fact": "quietly rewritten"}}),
    ("record_type", {"upd_record_type": "memory.entity"}),
    ("domain_scope", {"upd_domain_scope": "project.roadmap"}),
    ("evidence_disposition", {"upd_evidence_disposition": "unavailable"}),
    ("source_locator", {"upd_sources": [_source_wire(locator="/moved")]}),
    ("source_retrieved_at", {"upd_sources": [{**_source_wire(), "retrieved_at": T1}]}),
    (
        "source_order",
        {
            "sources": [_source_wire(), _source_wire(kind="conversation", source_id="conv-1")],
            "upd_sources": [_source_wire(kind="conversation", source_id="conv-1"), _source_wire()],
        },
    ),
    (
        "source_dropped",
        {
            "sources": [_source_wire(), _source_wire(kind="conversation", source_id="conv-1")],
            "upd_sources": [_source_wire()],
        },
    ),
    ("source_added", {"upd_sources": [_source_wire(), _source_wire(source_id="doc-9")]}),
    ("assertion_actor", {"upd_assertion": _assertion_wire(actor_id="asserter-2")}),
    ("assertion_role", {"upd_assertion": _assertion_wire(actor_role="editor")}),
    ("assertion_time", {"upd_assertion": _assertion_wire(asserted_at=T1)}),
    (
        "assertion_evidence",
        {"upd_assertion": _assertion_wire(evidence=[_evidence_reference_wire(excerpt="new")])},
    ),
    ("extraction_added", {"upd_extraction": _extraction_wire()}),
    (
        "extraction_changed",
        {"extraction": _extraction_wire(), "upd_extraction": _extraction_wire(extractor_id="extractor-2")},
    ),
    ("extraction_dropped", {"extraction": _extraction_wire(), "upd_extraction": _DELETE}),
)


@pytest.mark.parametrize("op", PRESERVING_OPS, ids=PRESERVING_OP_IDS)
@pytest.mark.parametrize(
    "label,overrides", PRESERVED_MUTATIONS, ids=[label for label, _ in PRESERVED_MUTATIONS]
)
def test_decision_transition_rejects_any_edit_to_the_claim(
    op: _Op, label: str, overrides: dict[str, Any]
) -> None:
    resolved = {key: (None if value is _DELETE else value) for key, value in overrides.items()}
    prev, upd = op.build_pair(**resolved)
    if overrides.get("upd_extraction") is _DELETE:
        upd = _with(upd, "provenance.extraction", _DELETE)
    with pytest.raises(ContractSemanticError):
        _check(op, prev, upd)


PRESERVED_TEMPORAL_FIELDS = ("event_at", "observed_at", "ingested_at", "valid_from", "valid_until")


@pytest.mark.parametrize("op", PRESERVING_OPS, ids=PRESERVING_OP_IDS)
@pytest.mark.parametrize("field_name", PRESERVED_TEMPORAL_FIELDS)
def test_decision_transition_rejects_moved_temporal_field(op: _Op, field_name: str) -> None:
    base_temporal = {
        "ingested_at": T0,
        "event_at": T0,
        "observed_at": T0,
        "valid_from": T0,
        "valid_until": "2030-01-01T00:00:00Z",
    }
    prev_temporal = {**base_temporal, "recorded_at": T0, "superseded_at": T1}
    upd_temporal = {**base_temporal, "recorded_at": T1}
    moved = "2024-06-01T00:00:00Z" if field_name != "ingested_at" else T1
    prev, upd = op.build_pair(
        prev_temporal=prev_temporal, upd_temporal={**upd_temporal, field_name: moved}
    )
    with pytest.raises(ContractSemanticError, match=field_name):
        _check(op, prev, upd)


@pytest.mark.parametrize("op", PRESERVING_OPS, ids=PRESERVING_OP_IDS)
def test_decision_transition_accepts_fully_preserved_temporal_envelope(op: _Op) -> None:
    base_temporal = {
        "ingested_at": T0,
        "event_at": T0,
        "observed_at": T0,
        "valid_from": T0,
        "valid_until": "2030-01-01T00:00:00Z",
    }
    prev, upd = op.build_pair(
        prev_temporal={**base_temporal, "recorded_at": T0, "superseded_at": T1},
        upd_temporal={**base_temporal, "recorded_at": T1},
    )
    _check(op, prev, upd)


@pytest.mark.parametrize("op", PRESERVING_OPS, ids=PRESERVING_OP_IDS)
def test_decision_transition_accepts_a_preserved_extraction(op: _Op) -> None:
    extraction = _extraction_wire(
        extractor_version="v3", confidence=0.75, reconciliation_state="novel"
    )
    prev, upd = op.build_pair(extraction=extraction)
    _check(op, prev, upd)


# --- record.supersede binds the new claim to the replacement -----------------


def test_supersede_accepts_replacement_drawing_on_wholly_different_sources() -> None:
    """The canonical prior record cites `doc-1`; this replacement cites only `doc-7` and
    `conv-7`. The result must declare exactly those, with nothing carried over."""
    op = OPS[3]
    sources = [
        _source_wire(source_id="doc-7", locator="/section/2"),
        _source_wire(kind="conversation", source_id="conv-7"),
    ]
    replacement = _replacement_wire(
        sources=sources,
        assertion=_assertion_wire(
            evidence=[
                _evidence_reference_wire(source=sources[0], excerpt="fresh"),
                _evidence_reference_wire(source=sources[1]),
            ],
            proposed_valid_from=T1,
            proposed_valid_until="2030-01-01T00:00:00Z",
        ),
        extraction=_extraction_wire(confidence=0.9, reconciliation_state="merged"),
        event_at=T0,
        observed_at=T1,
    )
    prev, upd = op.build_pair(replacement=replacement)
    _check(op, prev, upd, request=_supersede_request(replacement=replacement))
    assert upd["provenance"]["sources"] == sources


SUPERSEDE_UNBOUND_OUTPUTS: tuple[tuple[str, str, Any], ...] = (
    ("content", "content", {"fact": "not what was asked for"}),
    ("disposition", "provenance.evidence_disposition", "unavailable"),
    ("sources", "provenance.sources", [_source_wire(source_id="doc-2"), _source_wire()]),
    ("assertion", "provenance.assertion", _assertion_wire(actor_id="someone-else")),
    ("extraction", "provenance.extraction", _extraction_wire()),
    ("event_at", "provenance.temporal.event_at", T0),
    ("observed_at", "provenance.temporal.observed_at", T0),
    ("valid_from", "provenance.temporal.valid_from", T0),
    ("valid_until", "provenance.temporal.valid_until", T1),
)


@pytest.mark.parametrize(
    "label,path,value", SUPERSEDE_UNBOUND_OUTPUTS, ids=[label for label, _, _ in SUPERSEDE_UNBOUND_OUTPUTS]
)
def test_supersede_rejects_output_not_bound_to_the_replacement(
    label: str, path: str, value: Any
) -> None:
    op = OPS[3]
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError):
        _check(op, prev, _with(upd, path, value))


def test_supersede_rejects_unioning_the_prior_sources_into_the_new_claim() -> None:
    """The most tempting wrong implementation: keep the old sources "just in case"."""
    op = OPS[3]
    prev, upd = op.build_pair()
    unioned = [*upd["provenance"]["sources"], *prev["provenance"]["sources"]]
    with pytest.raises(ContractSemanticError, match="sources"):
        _check(op, prev, _with(upd, "provenance.sources", unioned))


@pytest.mark.parametrize(
    "overrides",
    [{"record_type": "memory.entity"}, {"domain_scope": "project.roadmap"}],
    ids=["record_type", "domain_scope"],
)
def test_supersede_rejects_replacement_reclassifying_the_record(overrides: dict[str, Any]) -> None:
    """Supersession replaces a claim; it must never be a back door to refiling a record
    under a different type or domain."""
    op = OPS[3]
    replacement = _replacement_wire(**overrides)
    prev, upd = op.build_pair(replacement=replacement)
    prev = _with(_with(prev, "record_type", "memory.fact"), "domain_scope", "personal.preferences")
    with pytest.raises(ContractSemanticError, match="record_type|domain_scope"):
        _check(op, prev, upd, request=_supersede_request(replacement=replacement))


# --- record.supersede validates its nested replacement like memory.create ----

INVALID_REPLACEMENTS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "duplicate_sources",
        {"sources": [_source_wire(source_id="doc-2"), _source_wire(source_id="doc-2")]},
    ),
    (
        "available_with_no_evidence",
        {"evidence_disposition": "available", "assertion": _assertion_wire(evidence=[])},
    ),
    (
        "available_with_no_sources",
        {"evidence_disposition": "available", "sources": [], "assertion": _assertion_wire(evidence=[])},
    ),
    (
        "evidence_source_not_declared",
        {
            "sources": [_source_wire(source_id="doc-2")],
            "assertion": _assertion_wire(
                evidence=[_evidence_reference_wire(source=_source_wire(source_id="doc-99"))]
            ),
        },
    ),
    (
        "evidence_source_locator_disagrees",
        {
            "sources": [_source_wire(source_id="doc-2", locator="/body")],
            "assertion": _assertion_wire(
                evidence=[
                    _evidence_reference_wire(
                        source=_source_wire(source_id="doc-2", locator="/somewhere-else")
                    )
                ]
            ),
        },
    ),
    (
        "duplicate_evidence",
        {
            "assertion": _assertion_wire(
                evidence=[
                    _evidence_reference_wire(source=_source_wire(source_id="doc-2"), excerpt="same"),
                    _evidence_reference_wire(source=_source_wire(source_id="doc-2"), excerpt="same"),
                ]
            )
        },
    ),
    ("malformed_domain_scope", {"domain_scope": "Not A Scope"}),
    ("malformed_record_type", {"record_type": "Memory.Fact"}),
    ("malformed_disposition", {"evidence_disposition": "NOT VALID"}),
    (
        "malformed_assertion_actor",
        {"assertion": _assertion_wire(actor_id="not a valid identifier!")},
    ),
    ("malformed_asserted_at", {"assertion": _assertion_wire(asserted_at="2024-01-01")}),
    (
        "reversed_proposed_validity_window",
        {
            "assertion": _assertion_wire(
                proposed_valid_from="2030-01-01T00:00:00Z", proposed_valid_until=T0
            )
        },
    ),
    ("event_after_observed", {"event_at": T1, "observed_at": T0}),
    ("extraction_confidence_out_of_range", {"extraction": _extraction_wire(confidence=1.5)}),
    ("malformed_extractor_id", {"extraction": _extraction_wire(extractor_id="not valid!")}),
    ("malformed_extracted_at", {"extraction": _extraction_wire(extracted_at="yesterday")}),
)


@pytest.mark.parametrize(
    "label,overrides", INVALID_REPLACEMENTS, ids=[label for label, _ in INVALID_REPLACEMENTS]
)
def test_supersede_input_rejects_incoherent_replacement(label: str, overrides: dict[str, Any]) -> None:
    request = _supersede_request(replacement=_replacement_wire(**overrides))
    with pytest.raises(ContractSemanticError):
        sem_knowledge.validate_record_supersede_input(request)


def test_supersede_input_accepts_an_evidence_free_unavailable_replacement() -> None:
    """`unavailable` is the one disposition that excuses both empty sources and no
    evidence -- and it must still be the caller's explicit statement, never a default."""
    request = _supersede_request(
        replacement=_replacement_wire(
            evidence_disposition="unavailable", sources=[], assertion=_assertion_wire(evidence=[])
        )
    )
    sem_knowledge.validate_record_supersede_input(request)


def test_supersede_input_rejects_missing_assertion_at_decode_time() -> None:
    payload = {
        "record_id": "rec-1",
        "replacement": {
            key: value for key, value in REPLACEMENT.items() if key != "assertion"
        },
        "rationale": _rationale_wire(reason_code="new_evidence"),
    }
    with pytest.raises(ContractDecodeError):
        sem_knowledge.decode_record_supersede_input(payload)
    assert not _is_schema_valid("RecordSupersedeInput", payload)


def test_supersede_input_rejects_hand_built_replacement_with_wrongly_typed_content() -> None:
    """A caller reaching the validator without a `from_wire` decode in front must still get a
    `ContractSemanticError`, never a raw `TypeError` from whatever we do with `content`."""
    request = RecordSupersedeInput(
        record_id="rec-1",
        replacement=MemoryCreateInput.from_wire(REPLACEMENT),
        rationale=GovernanceRationale(reason_code="new_evidence"),
    )
    broken = RecordSupersedeInput(
        record_id=request.record_id,
        replacement=dataclasses.replace(request.replacement, content="not a mapping"),
        rationale=request.rationale,
    )
    with pytest.raises(ContractSemanticError, match="content"):
        sem_knowledge.validate_record_supersede_input(broken)


# --- direct entry points never leak a raw TypeError/AttributeError -------------
#
# The governance validators reach into a `MemoryCreateInput`'s and a `GovernedRecord`'s
# nested lineage, and a frozen dataclass enforces nothing about the types of the fields it
# was handed. Every case below is a hand-built DTO that never went through `from_wire`.


def _hand_built_replacement(**overrides: Any) -> MemoryCreateInput:
    return dataclasses.replace(MemoryCreateInput.from_wire(REPLACEMENT), **overrides)


def _hand_built_assertion(**overrides: Any) -> Any:
    decoded = MemoryCreateInput.from_wire(REPLACEMENT)
    return dataclasses.replace(decoded.assertion, **overrides)


_GOOD_SOURCE = MemoryCreateInput.from_wire(REPLACEMENT).sources[0]


WRONGLY_TYPED_REPLACEMENTS: tuple[tuple[str, MemoryCreateInput], ...] = (
    ("content", _hand_built_replacement(content="not a mapping")),
    ("record_type", _hand_built_replacement(record_type=7)),
    ("domain_scope", _hand_built_replacement(domain_scope=7)),
    ("evidence_disposition", _hand_built_replacement(evidence_disposition=7)),
    ("sources", _hand_built_replacement(sources="not a sequence")),
    # A non-sized value, not just a wrong-but-iterable one: iterating a string happens to
    # fail on its members, so only an `int` actually exercises the sequence guard itself.
    ("sources_not_sized", _hand_built_replacement(sources=7)),
    ("sources_member", _hand_built_replacement(sources=("not a source",))),
    (
        "source_locator",
        _hand_built_replacement(
            sources=(dataclasses.replace(_GOOD_SOURCE, locator=7),),
        ),
    ),
    (
        "source_retrieved_at",
        _hand_built_replacement(
            sources=(dataclasses.replace(_GOOD_SOURCE, retrieved_at=7),),
        ),
    ),
    ("event_at", _hand_built_replacement(event_at=7)),
    ("observed_at", _hand_built_replacement(observed_at=7)),
    ("assertion", _hand_built_replacement(assertion="not an assertion")),
    ("assertion_actor_id", _hand_built_replacement(assertion=_hand_built_assertion(actor_id=7))),
    (
        "assertion_asserted_at",
        _hand_built_replacement(assertion=_hand_built_assertion(asserted_at=7)),
    ),
    (
        "assertion_proposed_valid_from",
        _hand_built_replacement(assertion=_hand_built_assertion(proposed_valid_from=7)),
    ),
    (
        "assertion_proposed_valid_until",
        _hand_built_replacement(assertion=_hand_built_assertion(proposed_valid_until=7)),
    ),
    (
        "assertion_evidence",
        _hand_built_replacement(assertion=_hand_built_assertion(evidence="not a sequence")),
    ),
    (
        "assertion_evidence_not_sized",
        _hand_built_replacement(assertion=_hand_built_assertion(evidence=7)),
    ),
    (
        "assertion_evidence_member",
        _hand_built_replacement(assertion=_hand_built_assertion(evidence=("not evidence",))),
    ),
    (
        "assertion_evidence_span",
        _hand_built_replacement(
            assertion=_hand_built_assertion(
                evidence=(EvidenceReference(source=_GOOD_SOURCE, span={"pointer": "p"}),)
            )
        ),
    ),
    (
        "assertion_evidence_span_offsets",
        _hand_built_replacement(
            assertion=_hand_built_assertion(
                evidence=(
                    EvidenceReference(
                        source=_GOOD_SOURCE,
                        span=SourceSpan(pointer="p", start_offset="0", end_offset="9"),
                    ),
                )
            )
        ),
    ),
    (
        "assertion_evidence_excerpt",
        _hand_built_replacement(
            assertion=_hand_built_assertion(
                evidence=(EvidenceReference(source=_GOOD_SOURCE, excerpt=7),)
            )
        ),
    ),
    ("extraction", _hand_built_replacement(extraction="not an extraction")),
    (
        "extraction_extracted_at",
        _hand_built_replacement(
            extraction=CandidateExtractionMetadata(extractor_id="extractor-1", extracted_at=7)
        ),
    ),
    (
        "extraction_reconciliation_state",
        _hand_built_replacement(
            extraction=CandidateExtractionMetadata(
                extractor_id="extractor-1", extracted_at=T0, reconciliation_state=7
            )
        ),
    ),
    (
        "extraction_confidence",
        _hand_built_replacement(
            extraction=CandidateExtractionMetadata(
                extractor_id="extractor-1", extracted_at=T0, confidence="0.5"
            )
        ),
    ),
)


@pytest.mark.parametrize(
    "label,replacement",
    WRONGLY_TYPED_REPLACEMENTS,
    ids=[label for label, _ in WRONGLY_TYPED_REPLACEMENTS],
)
def test_supersede_input_rejects_every_wrongly_typed_nested_replacement_field(
    label: str, replacement: MemoryCreateInput
) -> None:
    request = RecordSupersedeInput(
        record_id="rec-1",
        replacement=replacement,
        rationale=GovernanceRationale(reason_code="new_evidence"),
    )
    with pytest.raises(ContractSemanticError):
        sem_knowledge.validate_record_supersede_input(request)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
@pytest.mark.parametrize(
    "field,value",
    [
        ("assertion", "not an assertion"),
        ("extraction", "not an extraction"),
        ("sources", "not a sequence"),
        ("sources", 7),
        ("history", "not a sequence"),
        ("history", 7),
        ("evidence_disposition", 7),
    ],
    ids=[
        "assertion",
        "extraction",
        "sources",
        "sources_not_sized",
        "history",
        "history_not_sized",
        "evidence_disposition",
    ],
)
def test_transition_rejects_wrongly_typed_provenance_on_a_hand_built_result(
    op: _Op, field: str, value: Any
) -> None:
    """The result validators reach into both records' provenance, so a hand-built result
    carrying a wrongly typed nested value must raise `ContractSemanticError` too."""
    prev, upd = op.build_pair()
    result = op.result_cls.from_wire({"previous_record": prev, "updated_record": upd})
    broken_provenance = dataclasses.replace(
        result.updated_record.provenance, **{field: value}
    )
    broken = dataclasses.replace(
        result,
        updated_record=dataclasses.replace(
            result.updated_record, provenance=broken_provenance
        ),
    )
    with pytest.raises(ContractSemanticError):
        op.validate(
            broken,
            op.build_request(),
            _precondition(),
            "ws-1",
            op.trusted_actor,
            "user",
        )


def test_supersede_replacement_round_trips_with_extraction_and_unknown_additive_fields() -> None:
    """The nested replacement is decoded by the same tolerant production path as a top-level
    `memory.create` input: unknown additive fields are ignored at every depth, everything
    known -- including the optional extraction lineage -- survives a round trip verbatim, and
    strict schema conformance still rejects the additive spellings."""
    replacement = _replacement_wire(
        extraction=_extraction_wire(
            extractor_version="v3", confidence=0.5, reconciliation_state="some_future_state"
        ),
        assertion=_assertion_wire(
            evidence=[
                _evidence_reference_wire(
                    source=_source_wire(source_id="doc-2", locator="/body"),
                    span={"pointer": "/body", "start_offset": 1, "end_offset": 2},
                    excerpt="excerpt",
                )
            ],
            proposed_valid_from=T0,
            proposed_valid_until=T1,
        ),
        sources=[_source_wire(source_id="doc-2", locator="/body")],
        event_at=T0,
        observed_at=T1,
    )
    payload = {
        "record_id": "rec-1",
        "replacement": replacement,
        "rationale": _rationale_wire(reason_code="new_evidence"),
    }
    decoded = sem_knowledge.decode_record_supersede_input(payload)
    assert decoded.to_wire() == payload
    assert _is_schema_valid("RecordSupersedeInput", payload)

    augmented = copy.deepcopy(payload)
    augmented["future_top_level"] = "ignored"
    augmented["replacement"]["future_replacement_field"] = "ignored"
    augmented["replacement"]["assertion"]["future_assertion_field"] = "ignored"
    augmented["replacement"]["extraction"]["future_extraction_field"] = "ignored"
    augmented["replacement"]["assertion"]["evidence"][0]["future_evidence_field"] = "ignored"
    assert sem_knowledge.decode_record_supersede_input(augmented) == decoded
    assert not _is_schema_valid("RecordSupersedeInput", augmented)


# --- knowledge.propose now requires a rationale ------------------------------


def test_propose_input_without_rationale_is_rejected_by_schema_and_decoder() -> None:
    payload = {"record_id": "rec-1"}
    assert not _is_schema_valid("KnowledgeProposeInput", payload)
    with pytest.raises(ContractDecodeError):
        sem_knowledge.decode_knowledge_propose_input(payload)


def test_propose_input_with_rationale_is_accepted() -> None:
    payload = {"record_id": "rec-1", "rationale": _rationale_wire()}
    decoded = sem_knowledge.decode_knowledge_propose_input(payload)
    assert decoded.to_wire() == payload


@pytest.mark.parametrize(
    "rationale",
    [
        {"reason_code": "Not A Code"},
        {"reason_code": "meets_bar", "comment": "x" * 2049},
        {"comment": "no code at all"},
    ],
    ids=["malformed_code", "overlong_comment", "missing_code"],
)
def test_governance_input_rejects_malformed_rationale(rationale: dict[str, Any]) -> None:
    payload = {"record_id": "rec-1", "rationale": rationale}
    with pytest.raises((ContractSemanticError, ContractDecodeError)):
        sem_knowledge.decode_candidate_approve_input(payload)


# --- reserved-field smuggling guards on the four mutation inputs -------------

DECODE_INPUT_CASES = (
    (
        sem_knowledge.decode_knowledge_propose_input,
        {"record_id": "rec-1", "rationale": _rationale_wire()},
    ),
    (
        sem_knowledge.decode_candidate_approve_input,
        {"record_id": "rec-1", "rationale": _rationale_wire()},
    ),
    (
        sem_knowledge.decode_candidate_reject_input,
        {"record_id": "rec-1", "rationale": _rationale_wire()},
    ),
    (
        sem_knowledge.decode_record_supersede_input,
        {
            "record_id": "rec-1",
            "replacement": REPLACEMENT,
            "rationale": _rationale_wire(reason_code="new_evidence"),
        },
    ),
)


@pytest.mark.parametrize("decode_fn,base_payload", DECODE_INPUT_CASES)
@pytest.mark.parametrize("reserved_field", sorted(sem_knowledge.GOVERNANCE_TRANSITION_RESERVED_INPUT_FIELDS))
def test_decode_governance_input_rejects_each_reserved_field(decode_fn, base_payload, reserved_field: str) -> None:
    payload = {**base_payload, reserved_field: "smuggled-value"}
    with pytest.raises(ContractSemanticError, match="reserved"):
        decode_fn(payload)


@pytest.mark.parametrize("decode_fn,base_payload", DECODE_INPUT_CASES)
def test_decode_governance_input_accepts_clean_payload(decode_fn, base_payload) -> None:
    decode_fn(base_payload)


def test_decode_candidate_approve_input_rejects_reserved_field_nested_in_rationale() -> None:
    """A reserved server-owned field smuggled inside `rationale`, not at the top level, must
    be caught just as loudly: the tolerant decoder otherwise ignores it silently."""
    payload = {
        "record_id": "rec-1",
        "rationale": {"reason_code": "meets_bar", "reviewer": "smuggled-value"},
    }
    with pytest.raises(ContractSemanticError, match="reserved"):
        sem_knowledge.decode_candidate_approve_input(payload)


RESERVED_REPLACEMENT_PATHS: tuple[tuple[str, ...], ...] = (
    ("replacement",),
    ("replacement", "assertion"),
)


@pytest.mark.parametrize("path", RESERVED_REPLACEMENT_PATHS, ids=["replacement", "assertion"])
@pytest.mark.parametrize("reserved_field", sorted(sem_knowledge.GOVERNANCE_TRANSITION_RESERVED_INPUT_FIELDS))
def test_decode_record_supersede_input_rejects_reserved_field_nested_in_the_replacement(
    path: tuple[str, ...], reserved_field: str
) -> None:
    """The replacement is a whole nested `MemoryCreateInput`, so the recursive reserved-field
    scan must reach into it and into its own nested shapes, not stop at the top level."""
    payload = copy.deepcopy(
        {
            "record_id": "rec-1",
            "replacement": REPLACEMENT,
            "rationale": _rationale_wire(reason_code="new_evidence"),
        }
    )
    target: Any = payload
    for part in path:
        target = target[part]
    target[reserved_field] = "smuggled-value"
    with pytest.raises(ContractSemanticError, match="reserved"):
        sem_knowledge.decode_record_supersede_input(payload)


def test_decode_record_supersede_input_rejects_reserved_field_in_a_replacement_source() -> None:
    payload = {
        "record_id": "rec-1",
        "replacement": _replacement_wire(
            sources=[{**_source_wire(source_id="doc-2"), "superseded_at": T1}]
        ),
        "rationale": _rationale_wire(reason_code="new_evidence"),
    }
    with pytest.raises(ContractSemanticError, match="reserved"):
        sem_knowledge.decode_record_supersede_input(payload)


def test_decode_record_supersede_input_ignores_reserved_looking_key_inside_opaque_content() -> None:
    """`content` is opaque application content: a key that happens to spell a reserved
    governance field name inside it is not a smuggling attempt against this contract's own
    governance fields, and must not be rejected -- at any nesting depth."""
    payload = {
        "record_id": "rec-1",
        "replacement": _replacement_wire(
            content={
                "fact": "hello",
                "reviewer": "just some content field, not a smuggle",
                "nested": {"provenance": {"history": ["still just content"]}},
            }
        ),
        "rationale": _rationale_wire(reason_code="new_evidence"),
    }
    sem_knowledge.decode_record_supersede_input(payload)


# --- every direct helper argument is type-guarded ----------------------------


def _valid_result(op: _Op) -> Any:
    prev, upd = op.build_pair()
    return op.result_cls.from_wire({"previous_record": prev, "updated_record": upd})


BAD_DIRECT_ARGUMENTS: tuple[tuple[str, int, Any], ...] = (
    ("result", 0, None),
    ("result", 0, "not a result"),
    ("result", 0, {"previous_record": {}, "updated_record": {}}),
    ("request", 1, None),
    ("request", 1, "not a request"),
    ("request", 1, 123),
    ("precondition", 2, None),
    ("precondition", 2, "v1"),
    ("workspace", 3, None),
    ("workspace", 3, 123),
    ("actor", 4, None),
    ("actor", 4, 123),
    ("actor", 4, "not a valid identifier!"),
    ("actor_kind", 5, None),
    ("actor_kind", 5, 123),
    ("actor_kind", 5, "NOT A CODE"),
)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
@pytest.mark.parametrize(
    "label,position,value",
    BAD_DIRECT_ARGUMENTS,
    ids=[f"{label}-{index}" for index, (label, _, _) in enumerate(BAD_DIRECT_ARGUMENTS)],
)
def test_transition_helper_rejects_every_wrong_argument_type(
    op: _Op, label: str, position: int, value: Any
) -> None:
    arguments: list[Any] = [
        _valid_result(op),
        op.build_request(),
        _precondition(),
        "ws-1",
        op.trusted_actor,
        "user",
    ]
    arguments[position] = value
    with pytest.raises(ContractSemanticError):
        op.validate(*arguments)


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_helper_rejects_a_result_carrying_a_non_record(op: _Op) -> None:
    result = _valid_result(op)
    broken = dataclasses.replace(result, updated_record="not a governed record")
    with pytest.raises(ContractSemanticError, match="updated_record"):
        op.validate(broken, op.build_request(), _precondition(), "ws-1", op.trusted_actor, "user")


@pytest.mark.parametrize("op", OPS, ids=OP_IDS)
def test_transition_helper_revalidates_a_hand_built_request(op: _Op) -> None:
    """`request` is revalidated here rather than trusted, so a caller cannot bypass the input
    validator's checks by hand-building a request that never went through `from_wire`."""
    request = dataclasses.replace(op.build_request(), record_id="not a valid record id!")
    prev, upd = op.build_pair()
    with pytest.raises(ContractSemanticError, match="record_id"):
        _check(op, prev, upd, request=request)


def test_frozen_governance_actions_are_exactly_the_four_operations() -> None:
    assert sem_knowledge.GOVERNANCE_ACTIONS == frozenset(
        {"knowledge.propose", "candidate.approve", "candidate.reject", "record.supersede"}
    )
    assert {op.action for op in OPS} == sem_knowledge.GOVERNANCE_ACTIONS


# --------------------------------------------------------------------------
# 5. graph.traverse adversarial cases
# --------------------------------------------------------------------------


def _graph_request(**overrides: Any) -> GraphTraversalInput:
    """The complete original request `validate_graph_traversal_result` now requires."""
    payload: dict[str, Any] = {"start": [_record_version_reference_wire()]}
    payload.update(overrides)
    return GraphTraversalInput.from_wire(payload)


def _graph_result(**overrides: Any) -> GraphTraversalResult:
    return GraphTraversalResult.from_wire(_graph_traversal_result_wire(**overrides))


def _both_endpoint_nodes_wire(
    record_builder: Any = _canonical_knowledge_record_wire,
) -> list[dict[str, Any]]:
    """Both nodes `_graph_edge_wire()`'s default edge names, in deterministic `(depth,
    record_id, version)` order.

    A default edge names `rec-1` and `rec-2`, and both endpoints present states a *fully
    materialized* edge, so any result carrying that edge for a reason other than endpoint
    closure has to return both -- otherwise the edge rule fires first and hides whatever the
    test was actually about.

    `rec-1` is the seed `_graph_request()` starts from and so sits at depth 0; `rec-2` is not
    a requested seed and so sits at depth 1, since depth 0 is exactly the requested seeds. A
    test that wants both at depth 0 is a test about multi-seed first-page closure and states
    its own nodes.
    """
    return [
        _graph_node_wire(
            reference=_record_version_reference_wire(record_id="rec-1"),
            record=record_builder(record_id="rec-1"),
        ),
        _graph_node_wire(
            reference=_record_version_reference_wire(record_id="rec-2"),
            record=record_builder(record_id="rec-2"),
            depth=1,
        ),
    ]


def _validate_graph_result(
    result: GraphTraversalResult,
    *,
    request: GraphTraversalInput | None = None,
    workspace_id: str = "ws-1",
    canonical_resolution_time: str = T0,
    authorized_views: Any = frozenset(),
) -> None:
    sem_knowledge.validate_graph_traversal_result(
        result,
        request if request is not None else _graph_request(),
        workspace_id,
        canonical_resolution_time,
        authorized_views,
    )


# --- input: direction, view, seeds -------------------------------------------


def test_graph_traversal_input_rejects_unknown_direction() -> None:
    input_ = GraphTraversalInput.from_wire({"start": [_record_version_reference_wire()], "direction": "sideways"})
    with pytest.raises(ContractSemanticError, match="GraphDirection"):
        sem_knowledge.validate_graph_traversal_input(input_)


def test_graph_traversal_input_rejects_unknown_view() -> None:
    input_ = GraphTraversalInput.from_wire(
        {"start": [_record_version_reference_wire()], "direction": "outbound", "view": "some_future_view"}
    )
    with pytest.raises(ContractSemanticError, match="GovernedRecordView"):
        sem_knowledge.validate_graph_traversal_input(input_)


def test_graph_traversal_input_rejects_duplicate_start_reference() -> None:
    ref = _record_version_reference_wire()
    input_ = GraphTraversalInput.from_wire({"start": [ref, dict(ref)], "direction": "outbound"})
    with pytest.raises(ContractSemanticError, match="duplicates"):
        sem_knowledge.validate_graph_traversal_input(input_)


def test_graph_traversal_input_rejects_empty_start() -> None:
    input_ = GraphTraversalInput.from_wire({"start": []})
    with pytest.raises(ContractSemanticError, match="bounded range"):
        sem_knowledge.validate_graph_traversal_input(input_)


def test_graph_traversal_input_rejects_over_limit_depth() -> None:
    input_ = GraphTraversalInput.from_wire(
        {"start": [_record_version_reference_wire()], "direction": "outbound", "depth_limit": 9}
    )
    with pytest.raises(ContractSemanticError, match="depth_limit"):
        sem_knowledge.validate_graph_traversal_input(input_)


def test_graph_traversal_input_accepts_zero_depth_limit() -> None:
    input_ = GraphTraversalInput.from_wire(
        {"start": [_record_version_reference_wire()], "direction": "outbound", "depth_limit": 0}
    )
    sem_knowledge.validate_graph_traversal_input(input_)


def test_graph_traversal_input_accepts_absent_direction() -> None:
    input_ = GraphTraversalInput.from_wire({"start": [_record_version_reference_wire()]})
    sem_knowledge.validate_graph_traversal_input(input_)
    assert input_.direction is None


def test_resolve_graph_direction_defaults_absent_to_outbound() -> None:
    assert sem_knowledge.resolve_graph_direction(None) == sem_knowledge.GRAPH_DIRECTION_DEFAULT
    assert sem_knowledge.resolve_graph_direction(None) == "outbound"


def test_resolve_graph_direction_rejects_unknown_value() -> None:
    with pytest.raises(ContractSemanticError, match="GraphDirection"):
        sem_knowledge.resolve_graph_direction("sideways")


@pytest.mark.parametrize(
    "direction",
    [
        pytest.param(0, id="int-zero"),
        pytest.param(123, id="int"),
        pytest.param(True, id="bool"),
        pytest.param(("outbound",), id="tuple"),
        pytest.param({"direction": "outbound"}, id="mapping"),
        pytest.param(b"outbound", id="bytes"),
        pytest.param(object(), id="object"),
    ],
)
def test_resolve_graph_direction_rejects_a_non_string_present_value(direction: Any) -> None:
    """`resolve_graph_direction` is exported, so it is a direct entry point in its own right:
    a caller reaching it without a tolerant decode in front must see a `ContractSemanticError`,
    never the raw `TypeError` the length/regex check would raise on a non-string. `None` is
    the one non-string it accepts, and it means "absent", not "unvalidated"."""
    with pytest.raises(ContractSemanticError, match="expected a string"):
        sem_knowledge.resolve_graph_direction(direction)


def test_resolve_graph_direction_type_guard_precedes_the_pattern_check() -> None:
    """The guard has to come *before* the regex, not alongside it: an integer that would
    satisfy no pattern must still fail as a contract error rather than blowing up in `len()`."""
    with pytest.raises(ContractSemanticError) as raised:
        sem_knowledge.resolve_graph_direction(1)
    assert not isinstance(raised.value, (TypeError, AttributeError))
    assert "int" in str(raised.value)


def test_graph_traversal_input_accepts_sixty_four_seeds() -> None:
    seeds = [_record_version_reference_wire(record_id=f"rec-{i}") for i in range(64)]
    input_ = GraphTraversalInput.from_wire({"start": seeds})
    assert _is_schema_valid("GraphTraversalInput", {"start": seeds})
    sem_knowledge.validate_graph_traversal_input(input_)


def test_graph_traversal_input_rejects_sixty_five_seeds_at_schema_level() -> None:
    seeds = [_record_version_reference_wire(record_id=f"rec-{i}") for i in range(65)]
    assert not _is_schema_valid("GraphTraversalInput", {"start": seeds})


def test_graph_traversal_input_rejects_sixty_five_seeds_through_the_tolerant_decoder() -> None:
    """The schema ceiling is not the only gate: tolerant decoding never applies `maxItems`,
    so a 65-seed input that `from_wire` happily decodes must still fail semantically."""
    seeds = [_record_version_reference_wire(record_id=f"rec-{i}") for i in range(65)]
    input_ = GraphTraversalInput.from_wire({"start": seeds})
    assert len(input_.start) == 65
    with pytest.raises(ContractSemanticError, match="bounded range"):
        sem_knowledge.validate_graph_traversal_input(input_)


# --- input: the node budget the seeds need ------------------------------------
#
# `node_limit` and `start` are not independent bounds. A first page owes every seed at depth
# 0 and may return no more nodes than the limit, so `node_limit < len(start)` describes a
# result no traversal could return: the two rules that would have to hold cannot both hold.
# The request is refused rather than left to fail as an impossible result later.


def test_graph_traversal_input_rejects_a_node_limit_below_the_seed_count() -> None:
    document = {
        "start": [
            _record_version_reference_wire(record_id="rec-1"),
            _record_version_reference_wire(record_id="rec-2"),
        ],
        "node_limit": 1,
    }
    assert _is_schema_valid("GraphTraversalInput", document)
    with pytest.raises(ContractSemanticError, match="below the 2 requested start seed"):
        sem_knowledge.validate_graph_traversal_input(GraphTraversalInput.from_wire(document))


def test_graph_traversal_input_accepts_a_node_limit_equal_to_the_seed_count() -> None:
    """Equality is the boundary and is accepted: a first page of nothing but its own seeds is
    a real, answerable request."""
    document = {
        "start": [
            _record_version_reference_wire(record_id="rec-1"),
            _record_version_reference_wire(record_id="rec-2"),
        ],
        "node_limit": 2,
    }
    sem_knowledge.validate_graph_traversal_input(GraphTraversalInput.from_wire(document))


def test_graph_traversal_input_still_enforces_the_page_limit_maximum() -> None:
    """The seed-count floor is added beneath the existing `PageLimit` ceiling, never in place
    of it: a limit above the maximum stays rejected however many seeds are requested."""
    document = {"start": [_record_version_reference_wire()], "node_limit": MAX_PAGE_LIMIT + 1}
    with pytest.raises(ContractSemanticError, match="bounded range"):
        sem_knowledge.validate_graph_traversal_input(GraphTraversalInput.from_wire(document))


# --- input: continuation position ---------------------------------------------


def test_graph_traversal_input_rejects_page_metadata_naming_no_continuation_position() -> None:
    """The conservative, direct-safe reading of an input `page: {}`: a request to continue
    that names nowhere to continue *from* is not a request to start at the beginning, and it
    is not a request to continue either -- it is meaningless, and the only way to accept it
    would be to guess which of the two the caller meant. `page` is schema-optional precisely
    so "start at the beginning" can be stated by omitting it."""
    document = {"start": [_record_version_reference_wire()], "page": {}}
    assert _is_schema_valid("GraphTraversalInput", document)
    with pytest.raises(ContractSemanticError, match="names no continuation_token"):
        sem_knowledge.validate_graph_traversal_input(GraphTraversalInput.from_wire(document))


def test_graph_traversal_input_accepts_a_real_continuation_position() -> None:
    document = {"start": [_record_version_reference_wire()], "page": {"continuation_token": "cont-1"}}
    assert _is_schema_valid("GraphTraversalInput", document)
    sem_knowledge.validate_graph_traversal_input(GraphTraversalInput.from_wire(document))


def test_graph_traversal_input_accepts_an_absent_page() -> None:
    input_ = GraphTraversalInput.from_wire({"start": [_record_version_reference_wire()]})
    sem_knowledge.validate_graph_traversal_input(input_)
    assert input_.page is None


def test_graph_traversal_input_rejects_a_malformed_continuation_token() -> None:
    input_ = GraphTraversalInput.from_wire(
        {"start": [_record_version_reference_wire()], "page": {"continuation_token": "cont 1"}}
    )
    with pytest.raises(ContractSemanticError, match="OpaqueToken"):
        sem_knowledge.validate_graph_traversal_input(input_)


def test_graph_traversal_result_rejects_a_request_carrying_an_empty_page() -> None:
    """The result validator re-validates its request, so a meaningless continuation request
    cannot be laundered into a checkable one by arriving alongside a result."""
    request = _graph_request(page={})
    with pytest.raises(ContractSemanticError, match="names no continuation_token"):
        _validate_graph_result(_graph_result(), request=request)


# --- input: relation-type filter ---------------------------------------------


def test_graph_traversal_input_accepts_sixty_four_relation_types() -> None:
    relation_types = [f"relates_to_{index}" for index in range(64)]
    document = {"start": [_record_version_reference_wire()], "relation_types": relation_types}
    assert _is_schema_valid("GraphTraversalInput", document)
    sem_knowledge.validate_graph_traversal_input(GraphTraversalInput.from_wire(document))


@pytest.mark.parametrize(
    "relation_types,expected",
    [
        pytest.param([], "bounded range", id="empty"),
        pytest.param(["relates_to", "relates_to"], "duplicates", id="duplicate"),
        pytest.param([f"relates_to_{index}" for index in range(65)], "bounded range", id="sixty-five"),
    ],
)
def test_graph_traversal_input_rejects_malformed_relation_type_filter(
    relation_types: list[str], expected: str
) -> None:
    document = {"start": [_record_version_reference_wire()], "relation_types": relation_types}
    assert not _is_schema_valid("GraphTraversalInput", document)
    with pytest.raises(ContractSemanticError, match=expected):
        sem_knowledge.validate_graph_traversal_input(GraphTraversalInput.from_wire(document))


# --- result: the canonical positive case --------------------------------------


def test_graph_traversal_result_accepts_canonical_traversal() -> None:
    result = _graph_result(
        nodes=[
            _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1")),
            _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-2"), depth=1),
        ],
        edges=[_graph_edge_wire()],
    )
    _validate_graph_result(result)


# --- result: workspace, identity, limits, ordering -----------------------------


def test_graph_traversal_result_rejects_workspace_mismatch() -> None:
    node = _graph_node_wire(record=_canonical_knowledge_record_wire(workspace_id="ws-other"))
    result = _graph_result(nodes=[node])
    with pytest.raises(ContractSemanticError, match="workspace"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_relation_record_in_a_foreign_workspace() -> None:
    edge = _graph_edge_wire(
        record=_canonical_knowledge_record_wire(record_id="rel-1", workspace_id="ws-other")
    )
    result = _graph_result(edges=[edge])
    with pytest.raises(ContractSemanticError, match="workspace"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_duplicate_node_identity() -> None:
    node = _graph_node_wire()
    result = _graph_result(nodes=[node, copy.deepcopy(node)])
    with pytest.raises(ContractSemanticError, match="duplicates node identity"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_node_reference_not_matching_its_record() -> None:
    node = _graph_node_wire(
        reference=_record_version_reference_wire(record_id="rec-1"),
        record=_canonical_knowledge_record_wire(record_id="rec-9"),
    )
    result = _graph_result(nodes=[node])
    with pytest.raises(ContractSemanticError, match="identity does not match"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_node_count_over_the_applied_node_limit() -> None:
    nodes = [
        _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1")),
        _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-2"), depth=1),
    ]
    result = _graph_result(nodes=nodes, applied_node_limit=1)
    with pytest.raises(ContractSemanticError, match="applied_node_limit"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_edge_count_over_the_applied_edge_limit() -> None:
    edges = [
        _graph_edge_wire(record=_canonical_knowledge_record_wire(record_id="rel-1")),
        _graph_edge_wire(record=_canonical_knowledge_record_wire(record_id="rel-2")),
    ]
    result = _graph_result(edges=edges, applied_edge_limit=1)
    with pytest.raises(ContractSemanticError, match="applied_edge_limit"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_applied_limit_looser_than_requested() -> None:
    request = _graph_request(depth_limit=2)
    result = _graph_result(applied_depth_limit=3)
    with pytest.raises(ContractSemanticError, match="exceeds the requested"):
        _validate_graph_result(result, request=request)


def test_graph_traversal_result_rejects_unknown_ordering_basis() -> None:
    result = _graph_result(ordering_basis="record_id_asc")
    with pytest.raises(ContractSemanticError, match="GraphOrderingBasis"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_node_depth_exceeding_applied_limit() -> None:
    node = _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-2"), depth=5)
    result = _graph_result(nodes=[node], applied_depth_limit=1)
    with pytest.raises(ContractSemanticError, match="applied_depth_limit"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_seed_node_with_nonzero_depth() -> None:
    node = _graph_node_wire(depth=1)
    result = _graph_result(nodes=[node])
    with pytest.raises(ContractSemanticError, match="not 0"):
        _validate_graph_result(result)


# --- result: requested-seed closure --------------------------------------------
#
# Depth is measured from the requested seeds, and four independent rules say so. Two compose
# into "every requested seed is returned exactly once at depth 0 on the first page": the
# per-node rule above, which forbids a returned seed sitting at a nonzero depth, and the
# first-page rule below, which forbids a requested seed being absent from `nodes` at all. A
# third forbids the converse -- a node that is *not* a requested seed sitting at depth 0, on
# any page -- and a fourth forbids a continuation page returning a requested seed again at
# any depth. None subsumes another, so each is exercised on its own, and the node budget
# rules that keep the first-page obligation satisfiable are exercised beside them.


def _multi_seed_request(**overrides: Any) -> GraphTraversalInput:
    """A two-seed request, so "some seeds returned" stays distinguishable from "all of them"."""
    return _graph_request(
        start=[
            _record_version_reference_wire(record_id="rec-1"),
            _record_version_reference_wire(record_id="rec-2"),
        ],
        **overrides,
    )


def test_graph_traversal_result_rejects_a_first_page_omitting_one_requested_seed() -> None:
    """A first page owes *every* seed, not merely some: one missing seed is a result quietly
    answering a narrower question than the one that was asked."""
    result = _graph_result(
        nodes=[_graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1"))]
    )
    with pytest.raises(ContractSemanticError, match="requested start seed") as error:
        _validate_graph_result(result, request=_multi_seed_request())
    assert "('rec-2', 'v1')" in str(error.value)
    assert "rec-1" not in str(error.value)


def test_graph_traversal_result_rejects_a_first_page_omitting_every_requested_seed() -> None:
    """The hole this rule closes: a result may not drop every seed it was asked to start
    from and hand back unrelated nodes in their place. The unrelated node sits at depth 1,
    so it is the *missing seeds* rule that fires and not the depth-0 one."""
    result = _graph_result(
        nodes=[
            _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-9"), depth=1)
        ]
    )
    with pytest.raises(ContractSemanticError, match="requested start seed") as error:
        _validate_graph_result(result, request=_multi_seed_request())
    assert "('rec-1', 'v1')" in str(error.value)
    assert "('rec-2', 'v1')" in str(error.value)


def test_graph_traversal_result_accepts_a_first_page_returning_every_seed_at_depth_zero() -> None:
    """The positive half: all seeds present and all at depth 0 is exactly what a first page
    owes, and nothing beyond that is demanded of it."""
    nodes = [
        _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1")),
        _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-2")),
    ]
    _validate_graph_result(_graph_result(nodes=nodes), request=_multi_seed_request())


def test_graph_traversal_result_rejects_an_empty_first_page() -> None:
    """`start` carries at least one seed, so no first page can justify returning no nodes at
    all. The schema cannot see this: `nodes` has no `minItems` and could not have one, since
    a continuation page legitimately may run out."""
    wire = _graph_traversal_result_wire(nodes=[])
    assert _is_schema_valid("GraphTraversalResult", wire)
    with pytest.raises(ContractSemanticError, match="requested start seed"):
        _validate_graph_result(GraphTraversalResult.from_wire(wire))


def test_graph_traversal_result_rejects_an_empty_first_page_offering_a_next_page() -> None:
    """Page metadata on the *result* offers a next page; it says nothing about what this one
    already owed, so it never excuses a first page that returned none of its seeds."""
    result = _graph_result(nodes=[], applied_node_limit=1, page={"continuation_token": "cont-1"})
    with pytest.raises(ContractSemanticError, match="requested start seed"):
        _validate_graph_result(result)


def test_graph_traversal_result_accepts_a_continuation_page_that_omits_the_seeds() -> None:
    """A continuation page states that its seeds were already returned by an earlier page --
    that is what carrying `request.page` means -- so it does not repeat them."""
    result = _graph_result(
        nodes=[
            _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-9"), depth=1)
        ]
    )
    _validate_graph_result(
        result, request=_multi_seed_request(page={"continuation_token": "cont-1"})
    )


def test_graph_traversal_result_accepts_an_ordinary_continuation_page_node() -> None:
    """The positive half of the continuation rules: a node that is not a requested seed, at
    depth 1, is exactly what a continuation page is for."""
    result = _graph_result(
        nodes=[
            _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-9"), depth=1)
        ]
    )
    _validate_graph_result(result, request=_graph_request(page={"continuation_token": "cont-1"}))


def test_graph_traversal_result_rejects_a_repeated_seed_at_depth_zero_on_a_continuation_page() -> None:
    """A continuation page is excused from *returning* its seeds because an earlier page
    already did; returning one again re-answers a question that was already answered and
    double-counts it against this page's node limit."""
    result = _graph_result(nodes=[_graph_node_wire()])
    with pytest.raises(ContractSemanticError, match="repeats requested start seed"):
        _validate_graph_result(
            result, request=_graph_request(page={"continuation_token": "cont-1"})
        )


def test_graph_traversal_result_rejects_a_repeated_seed_at_nonzero_depth_on_a_continuation_page() -> None:
    """Moving the repeat off depth 0 does not make it a different node: the rule is about the
    seed being returned again *at all*, not about the depth it is returned at."""
    result = _graph_result(nodes=[_graph_node_wire(depth=1)])
    with pytest.raises(ContractSemanticError, match="repeats requested start seed"):
        _validate_graph_result(
            result, request=_graph_request(page={"continuation_token": "cont-1"})
        )


def test_graph_traversal_result_rejects_a_non_seed_at_depth_zero_on_a_first_page() -> None:
    """The converse of first-page closure, and the half no seed-presence check can cover:
    this result returns every seed it owes *and* smuggles an unrelated record in beside them
    at depth 0, which claims the traversal also started from somewhere it was never asked to."""
    result = _graph_result(
        nodes=[
            _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1")),
            _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-9")),
        ]
    )
    with pytest.raises(ContractSemanticError, match="is not a requested start seed") as error:
        _validate_graph_result(result)
    assert "('rec-9', 'v1')" in str(error.value)


def test_graph_traversal_result_rejects_a_non_seed_at_depth_zero_on_a_continuation_page() -> None:
    """The depth-0 rule holds on every page, and a continuation page is where it has to be
    stated separately: that page repeats no seeds, so nothing it returns may claim depth 0."""
    result = _graph_result(
        nodes=[_graph_node_wire(reference=_record_version_reference_wire(record_id="rec-9"))]
    )
    with pytest.raises(ContractSemanticError, match="is not a requested start seed"):
        _validate_graph_result(
            result, request=_graph_request(page={"continuation_token": "cont-1"})
        )


# --- result: the node budget a first page's obligation needs ---------------------
#
# First-page closure and the node limit are not independent bounds: a first page owes every
# seed at depth 0 *and* may return no more nodes than the limit it states, so a limit below
# the seed count describes a page that can satisfy neither rule. The request-side rule
# (`node_limit >= len(start)`) lives with the input tests below; this is the result-side one,
# which also binds a server that chose the limit itself when the request named none.


def test_graph_traversal_result_rejects_an_applied_node_limit_below_the_seed_count() -> None:
    """The request named no `node_limit` at all, so nothing on the input path could have
    caught this: the server chose an applied limit its own first page cannot satisfy."""
    result = _graph_result(
        nodes=[_graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1"))],
        applied_node_limit=1,
    )
    assert _multi_seed_request().node_limit is None
    with pytest.raises(ContractSemanticError, match="below the 2 requested start seed"):
        _validate_graph_result(result, request=_multi_seed_request())


def test_graph_traversal_result_accepts_an_applied_node_limit_equal_to_the_seed_count() -> None:
    """Equality is the boundary, and it is answerable: a first page of nothing but its seeds."""
    nodes = [
        _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1")),
        _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-2")),
    ]
    _validate_graph_result(
        _graph_result(nodes=nodes, applied_node_limit=2), request=_multi_seed_request()
    )


def test_graph_traversal_result_exempts_a_continuation_page_from_the_seed_count_relation() -> None:
    """A continuation page repeats no seeds, so it owes them no room: an applied node limit
    below the seed count is coherent there, and only there."""
    result = _graph_result(
        nodes=[
            _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-9"), depth=1)
        ],
        applied_node_limit=1,
    )
    _validate_graph_result(
        result, request=_multi_seed_request(page={"continuation_token": "cont-1"})
    )


@pytest.mark.parametrize(
    "page",
    [
        pytest.param("cont-1", id="bare-string"),
        pytest.param({"continuation_token": "cont-1"}, id="raw-mapping"),
        pytest.param(PageMetadata(continuation_token=None), id="names-no-continuation"),
        pytest.param(PageMetadata(continuation_token="cont 1"), id="malformed-token"),
        pytest.param(PageMetadata(continuation_token=7), id="non-string-token"),
    ],
)
def test_graph_traversal_result_rejects_a_hand_built_malformed_request_page(page: Any) -> None:
    """A malformed `request.page` must never read as "this is a continuation page" and so
    switch the first-page seed rule off. Every case here pairs the bad page with a result
    returning none of its seeds, so a validator that shrugged the page value off would have
    to let the result through -- and each still raises `ContractSemanticError`, never a raw
    `TypeError`/`AttributeError`."""
    result = _graph_result(
        nodes=[
            _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-9"), depth=1)
        ]
    )
    with pytest.raises(ContractSemanticError):
        _validate_graph_result(result, request=dataclasses.replace(_graph_request(), page=page))


def test_graph_traversal_result_rejects_out_of_order_nodes() -> None:
    """`rec-2` is the requested seed, so the depth-0 node it names is legitimate and the
    ordering rule is the only thing left to fire."""
    node_depth_1 = _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1"), depth=1)
    node_depth_0 = _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-2"), depth=0)
    result = _graph_result(nodes=[node_depth_1, node_depth_0])
    with pytest.raises(ContractSemanticError, match="deterministic"):
        _validate_graph_result(result, request=_graph_request(start=[_record_version_reference_wire(record_id="rec-2")]))


def test_graph_traversal_result_rejects_out_of_order_edges() -> None:
    node_a = _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-a"))
    node_b = _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-b"), depth=1)
    edge_b = _graph_edge_wire(
        source=_record_version_reference_wire(record_id="rec-b"),
        target=_record_version_reference_wire(record_id="rec-a"),
        record=_canonical_knowledge_record_wire(record_id="rel-1"),
    )
    edge_a = _graph_edge_wire(
        source=_record_version_reference_wire(record_id="rec-a"),
        target=_record_version_reference_wire(record_id="rec-b"),
        record=_canonical_knowledge_record_wire(record_id="rel-2"),
    )
    result = _graph_result(nodes=[node_a, node_b], edges=[edge_b, edge_a])
    with pytest.raises(ContractSemanticError, match="deterministic"):
        _validate_graph_result(result, request=_graph_request(start=[_record_version_reference_wire(record_id="rec-a")]))


def test_graph_traversal_result_rejects_same_endpoints_with_relations_out_of_order() -> None:
    """Ordering on endpoints alone is not deterministic: two edges between the same pair
    still have to sort by `relation_type`, then by the relation record they name."""
    later = _graph_edge_wire(
        relation_type="relates_to", record=_canonical_knowledge_record_wire(record_id="rel-2")
    )
    earlier = _graph_edge_wire(
        relation_type="derived_from", record=_canonical_knowledge_record_wire(record_id="rel-1")
    )
    ordered = _graph_result(
        nodes=_both_endpoint_nodes_wire(), edges=[earlier, later], applied_edge_limit=10
    )
    _validate_graph_result(ordered)
    result = _graph_result(nodes=_both_endpoint_nodes_wire(), edges=[later, earlier])
    with pytest.raises(ContractSemanticError, match="deterministic"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_same_relation_type_with_relation_records_out_of_order() -> None:
    later = _graph_edge_wire(record=_canonical_knowledge_record_wire(record_id="rel-2"))
    earlier = _graph_edge_wire(record=_canonical_knowledge_record_wire(record_id="rel-1"))
    result = _graph_result(nodes=_both_endpoint_nodes_wire(), edges=[later, earlier])
    with pytest.raises(ContractSemanticError, match="deterministic"):
        _validate_graph_result(result)


# --- result: relation identity binding ----------------------------------------


def test_graph_traversal_result_rejects_relation_reference_not_matching_the_relation_record() -> None:
    edge = _graph_edge_wire(relation_reference=_record_version_reference_wire(record_id="rel-9"))
    result = _graph_result(edges=[edge])
    with pytest.raises(ContractSemanticError, match="relation_reference does not identify"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_duplicate_relation_record_identity() -> None:
    edge = _graph_edge_wire()
    result = _graph_result(nodes=_both_endpoint_nodes_wire(), edges=[edge, copy.deepcopy(edge)])
    with pytest.raises(ContractSemanticError, match="duplicates relation record identity"):
        _validate_graph_result(result)


def test_graph_edge_schema_requires_the_relation_reference() -> None:
    edge = _graph_edge_wire()
    del edge["relation_reference"]
    assert not _is_schema_valid("GraphEdge", edge)


# --- result: request filters --------------------------------------------------


def test_graph_traversal_result_rejects_node_outside_the_requested_domain_scope() -> None:
    result = _graph_result()
    with pytest.raises(ContractSemanticError, match="domain_scope"):
        _validate_graph_result(result, request=_graph_request(domain_scope="team.decisions"))


def test_graph_traversal_result_rejects_relation_record_outside_the_requested_domain_scope() -> None:
    edge = _graph_edge_wire(
        record=_canonical_knowledge_record_wire(record_id="rel-1", domain_scope="team.decisions")
    )
    result = _graph_result(edges=[edge])
    with pytest.raises(ContractSemanticError, match="domain_scope"):
        _validate_graph_result(result, request=_graph_request(domain_scope="personal.preferences"))


def test_graph_traversal_result_rejects_relation_type_that_was_not_requested() -> None:
    result = _graph_result(edges=[_graph_edge_wire(relation_type="derived_from")])
    with pytest.raises(ContractSemanticError, match="was not among the requested"):
        _validate_graph_result(result, request=_graph_request(relation_types=["relates_to"]))


def test_graph_traversal_result_accepts_relation_type_that_was_requested() -> None:
    result = _graph_result(
        nodes=_both_endpoint_nodes_wire(), edges=[_graph_edge_wire(relation_type="relates_to")]
    )
    _validate_graph_result(result, request=_graph_request(relation_types=["relates_to", "derived_from"]))


# --- result: view trust boundary ----------------------------------------------


def test_graph_traversal_result_rejects_non_canonical_record_under_default_view() -> None:
    node = _graph_node_wire(record=_candidate_knowledge_record_wire())
    result = _graph_result(nodes=[node])
    with pytest.raises(ContractSemanticError, match="non-canonical"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_accepted_record_with_non_canonical_authority() -> None:
    """`l2`/`accepted`/`current` is not enough: an `accepted` record still carrying
    `reviewed` authority has not reached the canonical bar a default traversal may return."""
    record = _canonical_knowledge_record_wire()
    record["authority_level"] = "reviewed"
    result = _graph_result(nodes=[_graph_node_wire(record=record)])
    with pytest.raises(ContractSemanticError, match="authority_level must be exactly"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_canonical_record_missing_a_reviewer() -> None:
    """Two independent rules forbid this and the generic authority-coherence one fires
    first, so the message is matched loosely; the view-level check behind it stays as
    defence in depth, exactly as `knowledge.search` keeps its own."""
    record = _canonical_knowledge_record_wire(reviewer=None)
    result = _graph_result(nodes=[_graph_node_wire(record=record)])
    with pytest.raises(ContractSemanticError, match="reviewer"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_relation_record_missing_a_reviewer() -> None:
    record = _canonical_knowledge_record_wire(record_id="rel-1", reviewer=None)
    result = _graph_result(edges=[_graph_edge_wire(record=record)])
    with pytest.raises(ContractSemanticError, match="reviewer"):
        _validate_graph_result(result)


@pytest.mark.parametrize(
    "valid_from,valid_until,expected",
    [
        pytest.param(T1, None, "not yet valid", id="not-yet-valid"),
        pytest.param(None, "2023-12-31T00:00:00Z", "no longer valid", id="already-expired"),
    ],
)
def test_graph_traversal_result_rejects_record_outside_its_validity_window(
    valid_from: str | None, valid_until: str | None, expected: str
) -> None:
    record = _canonical_knowledge_record_wire(valid_from=valid_from, valid_until=valid_until)
    result = _graph_result(nodes=[_graph_node_wire(record=record)])
    with pytest.raises(ContractSemanticError, match=expected):
        _validate_graph_result(result)


@pytest.mark.parametrize("view", ["candidates", "history"])
def test_graph_traversal_result_rejects_wider_view_without_authorization(view: str) -> None:
    builder = _candidate_knowledge_record_wire if view == "candidates" else _history_knowledge_record_wire
    result = _graph_result(nodes=[_graph_node_wire(record=builder())])
    with pytest.raises(ContractSemanticError, match="never widens trust"):
        _validate_graph_result(result, request=_graph_request(view=view), authorized_views=frozenset())


@pytest.mark.parametrize("view", ["candidates", "history"])
def test_graph_traversal_result_accepts_wider_view_with_authorization(view: str) -> None:
    builder = _candidate_knowledge_record_wire if view == "candidates" else _history_knowledge_record_wire
    result = _graph_result(
        nodes=_both_endpoint_nodes_wire(builder),
        edges=[_graph_edge_wire(record=builder(record_id="rel-1"))],
    )
    _validate_graph_result(
        result, request=_graph_request(view=view), authorized_views=frozenset({view})
    )


@pytest.mark.parametrize("view", ["candidates", "history"])
def test_graph_traversal_result_rejects_wrong_state_under_an_authorized_wider_view(view: str) -> None:
    """Authorization widens *which* slice may be returned, never what may be in it."""
    result = _graph_result(nodes=[_graph_node_wire(record=_canonical_knowledge_record_wire())])
    with pytest.raises(ContractSemanticError, match="under the"):
        _validate_graph_result(
            result, request=_graph_request(view=view), authorized_views=frozenset({view})
        )


def test_graph_traversal_result_rejects_candidate_record_carrying_a_reviewer() -> None:
    record = _candidate_knowledge_record_wire()
    record["reviewer"] = "reviewer-1"
    result = _graph_result(nodes=[_graph_node_wire(record=record)])
    with pytest.raises(ContractSemanticError, match="carries a reviewer"):
        _validate_graph_result(
            result, request=_graph_request(view="candidates"), authorized_views=frozenset({"candidates"})
        )


def test_graph_traversal_result_default_view_never_requires_a_wider_authorization() -> None:
    """An absent view is `current_canonical`, which is always allowed and never gated."""
    _validate_graph_result(_graph_result(), authorized_views=frozenset())
    _validate_graph_result(
        _graph_result(), request=_graph_request(view="current_canonical"), authorized_views=frozenset()
    )


def test_graph_traversal_result_rejects_unknown_authorized_view_value() -> None:
    with pytest.raises(ContractSemanticError, match="GovernedRecordView"):
        _validate_graph_result(_graph_result(), authorized_views=frozenset({"some_future_view"}))


def test_graph_traversal_result_rejects_unknown_requested_view_even_when_authorized() -> None:
    """A view this build cannot check fails closed in the request validation, so naming it
    in `authorized_views` too can never talk it back into being honoured."""
    request = GraphTraversalInput.from_wire(
        {"start": [_record_version_reference_wire()], "view": "some_future_view"}
    )
    with pytest.raises(ContractSemanticError, match="GovernedRecordView"):
        _validate_graph_result(
            _graph_result(), request=request, authorized_views=frozenset({"some_future_view"})
        )


# --- result: endpoint and boundary closure ------------------------------------


def test_graph_edge_schema_allows_both_endpoints_absent_but_semantics_never_does() -> None:
    """Structural optionality is what makes a boundary representable; it is not permission
    to return an edge that anchors to nothing."""
    edge = _graph_edge_wire(include_source=False, include_target=False, boundary_reason="page_boundary")
    assert _is_schema_valid("GraphEdge", edge)
    result = _graph_result(edges=[edge])
    with pytest.raises(ContractSemanticError, match="names neither a source nor a target"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_absent_endpoint_with_no_reason() -> None:
    edge = _graph_edge_wire(include_target=False)
    result = _graph_result(edges=[edge])
    with pytest.raises(ContractSemanticError, match="states no boundary_reason"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_unknown_boundary_reason() -> None:
    edge = _graph_edge_wire(include_target=False, boundary_reason="some_future_boundary")
    result = _graph_result(edges=[edge])
    with pytest.raises(ContractSemanticError, match="GraphBoundaryReason"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_boundary_reason_when_both_endpoints_are_present() -> None:
    edge = _graph_edge_wire(boundary_reason="page_boundary")
    result = _graph_result(edges=[edge])
    with pytest.raises(ContractSemanticError, match="no boundary to justify"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_boundary_edge_anchored_to_no_returned_node() -> None:
    edge = _graph_edge_wire(
        source=_record_version_reference_wire(record_id="rec-missing"),
        include_target=False,
        boundary_reason="page_boundary",
    )
    result = _graph_result(edges=[edge], applied_node_limit=1, page={"continuation_token": "cont-1"})
    with pytest.raises(ContractSemanticError, match="must anchor to a returned node"):
        _validate_graph_result(result)


def test_graph_traversal_result_accepts_a_fully_materialized_edge() -> None:
    """The positive half of the fully-materialized-edge contract: both endpoints present and
    both returned as nodes is exactly what "no boundary here" means, and is accepted."""
    result = _graph_result(
        nodes=_both_endpoint_nodes_wire(),
        edges=[
            _graph_edge_wire(
                source=_record_version_reference_wire(record_id="rec-1"),
                target=_record_version_reference_wire(record_id="rec-2"),
            )
        ],
    )
    _validate_graph_result(result)


@pytest.mark.parametrize(
    "source_id,target_id,expected",
    [
        pytest.param("rec-1", "rec-missing", "returned no node for: target", id="target-missing"),
        pytest.param("rec-missing", "rec-1", "returned no node for: source", id="source-missing"),
    ],
)
def test_graph_traversal_result_rejects_a_named_endpoint_it_never_returned(
    source_id: str, target_id: str, expected: str
) -> None:
    """Naming both endpoints *is* the claim that this edge is fully materialized here, so one
    of them pointing at a record this page never returned is a contradiction, not a boundary.
    A traversal that did not reach an end omits that endpoint and justifies the omission with
    a `boundary_reason`; it never names an end it did not reach."""
    result = _graph_result(
        nodes=[_graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1"))],
        edges=[
            _graph_edge_wire(
                source=_record_version_reference_wire(record_id=source_id),
                target=_record_version_reference_wire(record_id=target_id),
            )
        ],
    )
    with pytest.raises(ContractSemanticError, match=expected):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_dangling_edge_endpoint() -> None:
    """Both endpoints named and neither returned stays rejected, and the message names both:
    the rule is per-endpoint, not "at least one"."""
    result = _graph_result(
        nodes=[_graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1"))],
        edges=[
            _graph_edge_wire(
                source=_record_version_reference_wire(record_id="rec-missing-a"),
                target=_record_version_reference_wire(record_id="rec-missing-b"),
            )
        ],
    )
    with pytest.raises(ContractSemanticError, match="returned no node for: source, target"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_an_endpoint_returned_only_at_another_version() -> None:
    """Endpoint identity is `(record_id, version)`, so a returned node for the same record at
    a different version is not the endpoint this edge named."""
    result = _graph_result(
        nodes=[
            _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-1")),
            _graph_node_wire(
                reference=_record_version_reference_wire(record_id="rec-2", version="v2"),
                record=_canonical_knowledge_record_wire(record_id="rec-2", version="v2"),
                depth=1,
            ),
        ],
        edges=[_graph_edge_wire()],
    )
    with pytest.raises(ContractSemanticError, match="returned no node for: target"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_page_boundary_without_a_continuation_token() -> None:
    edge = _graph_edge_wire(include_target=False, boundary_reason="page_boundary")
    result = _graph_result(edges=[edge], applied_node_limit=1)
    with pytest.raises(ContractSemanticError, match="offers no page continuation token"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_page_boundary_before_the_node_limit_is_reached() -> None:
    edge = _graph_edge_wire(include_target=False, boundary_reason="page_boundary")
    result = _graph_result(
        edges=[edge], applied_node_limit=10, page={"continuation_token": "cont-1"}
    )
    with pytest.raises(ContractSemanticError, match="node limit was not reached exactly"):
        _validate_graph_result(result)


def test_graph_traversal_result_accepts_a_coherent_page_boundary() -> None:
    edge = _graph_edge_wire(include_target=False, boundary_reason="page_boundary")
    result = _graph_result(
        edges=[edge], applied_node_limit=1, page={"continuation_token": "cont-1"}
    )
    _validate_graph_result(result)


def test_graph_traversal_result_rejects_depth_boundary_before_the_applied_depth() -> None:
    edge = _graph_edge_wire(include_target=False, boundary_reason="depth_boundary")
    result = _graph_result(edges=[edge], applied_depth_limit=3)
    with pytest.raises(ContractSemanticError, match="not at applied_depth_limit"):
        _validate_graph_result(result)


def test_graph_traversal_result_accepts_a_coherent_depth_boundary() -> None:
    deep_node = _graph_node_wire(reference=_record_version_reference_wire(record_id="rec-2"), depth=1)
    edge = _graph_edge_wire(
        source=_record_version_reference_wire(record_id="rec-2"),
        include_target=False,
        boundary_reason="depth_boundary",
    )
    result = _graph_result(
        nodes=[_graph_node_wire(), deep_node], edges=[edge], applied_depth_limit=1
    )
    _validate_graph_result(result)


def test_graph_traversal_result_rejects_page_boundary_backed_by_an_exhausted_page() -> None:
    """`page_boundary` requires a real continuation offer. `page: {}` is the opposite claim --
    this traversal is exhausted -- so there is no further page for the missing endpoint to be
    waiting on, and the boundary is unjustified."""
    edge = _graph_edge_wire(include_target=False, boundary_reason="page_boundary")
    result = _graph_result(edges=[edge], applied_node_limit=1, page={})
    with pytest.raises(ContractSemanticError, match="offers no page continuation token"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_a_continuation_token_with_no_limit_reached() -> None:
    result = _graph_result(
        applied_node_limit=10, applied_edge_limit=10, page={"continuation_token": "cont-1"}
    )
    with pytest.raises(ContractSemanticError, match="offers a continuation token"):
        _validate_graph_result(result)


def test_graph_traversal_result_accepts_page_metadata_when_node_limit_reached() -> None:
    result = _graph_result(applied_node_limit=1, applied_edge_limit=10, page={"continuation_token": "cont-1"})
    _validate_graph_result(result)


def test_graph_traversal_result_accepts_an_exhausted_page_at_exact_node_limit_saturation() -> None:
    """A traversal may stop exactly at its node limit and still have nothing further to give.
    `{}` says exactly that, and it is the only spelling of it: `page` is required on every
    paginated result in this contract, so exhaustion is stated rather than left to be inferred
    from a field that is not there."""
    wire = _graph_traversal_result_wire(applied_node_limit=1, applied_edge_limit=10, page={})
    assert _is_schema_valid("GraphTraversalResult", wire)
    _validate_graph_result(GraphTraversalResult.from_wire(wire))


def test_graph_traversal_result_accepts_an_exhausted_page_at_exact_edge_limit_saturation() -> None:
    """The same holds for the other limit that can be saturated."""
    result = _graph_result(
        nodes=_both_endpoint_nodes_wire(),
        edges=[_graph_edge_wire()],
        applied_node_limit=10,
        applied_edge_limit=1,
        page={},
    )
    _validate_graph_result(result)


def test_graph_traversal_result_accepts_an_exhausted_page_with_no_limit_reached() -> None:
    """The ordinary last page: nothing was truncated, and the read is over."""
    result = _graph_result(applied_node_limit=10, applied_edge_limit=10, page={})
    _validate_graph_result(result)


def test_graph_traversal_result_rejects_a_malformed_continuation_token() -> None:
    result = _graph_result(
        applied_node_limit=1, applied_edge_limit=10, page={"continuation_token": "cont 1"}
    )
    with pytest.raises(ContractSemanticError, match="OpaqueToken"):
        _validate_graph_result(result)


def test_graph_traversal_result_rejects_a_missing_result_page() -> None:
    """The half that makes `{}` unambiguous: a result may not omit `page`. Rejected by the
    strict schema, by the tolerant decoder, and -- for a hand-built DTO that never went
    through either -- by the semantic validator itself."""
    wire = _graph_traversal_result_wire()
    del wire["page"]
    assert not _is_schema_valid("GraphTraversalResult", wire)
    with pytest.raises(ContractDecodeError, match="missing required field 'page'"):
        GraphTraversalResult.from_wire(wire)
    hand_built = dataclasses.replace(_graph_result(), page=None)
    with pytest.raises(ContractSemanticError, match="page"):
        _validate_graph_result(hand_built)


# --- result: shared projection freshness ---------------------------------------


def test_graph_traversal_result_rejects_malformed_freshness_timestamp() -> None:
    result = _graph_result(freshness=_freshness_wire(as_of="not-a-timestamp"))
    with pytest.raises(ContractSemanticError, match="freshness.as_of"):
        _validate_graph_result(result)


@pytest.mark.parametrize(
    "freshness,expected",
    [
        pytest.param(_freshness_wire(projection_versions={}), "names no projection", id="empty-versions"),
        pytest.param(_freshness_wire(projection_watermarks={}), "names no projection", id="empty-watermarks"),
        pytest.param(
            _freshness_wire(projection_versions={"graph_index": "pv-1"}, projection_watermarks={"other": "wm-1"}),
            "same projections",
            id="mismatched-keys",
        ),
        pytest.param(
            _freshness_wire(projection_versions={"Graph Index": "pv-1"}, projection_watermarks={"Graph Index": "wm-1"}),
            "OpenCode",
            id="malformed-key",
        ),
        pytest.param(
            _freshness_wire(projection_versions={"graph_index": "pv 1"}, projection_watermarks={"graph_index": "wm-1"}),
            "ProjectionVersion",
            id="malformed-version",
        ),
    ],
)
def test_graph_traversal_result_rejects_incoherent_freshness(
    freshness: dict[str, Any], expected: str
) -> None:
    result = _graph_result(freshness=freshness)
    with pytest.raises(ContractSemanticError, match=expected):
        _validate_graph_result(result)


@pytest.mark.parametrize(
    "freshness",
    [
        pytest.param(_freshness_wire(projection_versions={}), id="empty-versions"),
        pytest.param(_freshness_wire(projection_watermarks={}), id="empty-watermarks"),
        pytest.param(
            _freshness_wire(projection_versions={"Graph Index": "pv-1"}, projection_watermarks={"Graph Index": "wm-1"}),
            id="malformed-key",
        ),
        pytest.param(
            _freshness_wire(projection_versions={"graph_index": "pv 1"}, projection_watermarks={"graph_index": "wm-1"}),
            id="malformed-version",
        ),
    ],
)
def test_strict_schema_rejects_incoherent_freshness(freshness: dict[str, Any]) -> None:
    assert not _is_schema_valid("GraphTraversalResult", _graph_traversal_result_wire(freshness=freshness))


def test_strict_schema_requires_projection_watermarks() -> None:
    freshness = _freshness_wire()
    del freshness["projection_watermarks"]
    assert not _is_schema_valid("GraphTraversalResult", _graph_traversal_result_wire(freshness=freshness))


def test_strict_schema_cannot_see_a_mismatched_key_set_only_semantics_can() -> None:
    """The key-set equality rule is the reason the semantic validator exists at all: the two
    maps are individually schema-valid here, and only cross-field validation catches it."""
    freshness = _freshness_wire(
        projection_versions={"graph_index": "pv-1"}, projection_watermarks={"other_index": "wm-1"}
    )
    assert _is_schema_valid("GraphTraversalResult", _graph_traversal_result_wire(freshness=freshness))
    with pytest.raises(ContractSemanticError, match="same projections"):
        _validate_graph_result(_graph_result(freshness=freshness))


def test_graph_traversal_result_rejects_non_boolean_stale_on_a_hand_built_dto() -> None:
    """`stale` is a trust statement: a truthy non-boolean must never read as one."""
    result = _graph_result()
    broken = dataclasses.replace(result, freshness=dataclasses.replace(result.freshness, stale=1))
    with pytest.raises(ContractSemanticError, match="stale"):
        _validate_graph_result(broken)


def test_graph_traversal_result_rejects_freshness_as_of_other_than_the_resolution_time() -> None:
    result = _graph_result(freshness=_freshness_wire(as_of=T1))
    with pytest.raises(ContractSemanticError, match="canonical_resolution_time"):
        _validate_graph_result(result, canonical_resolution_time=T0)


def test_graph_traversal_result_rejects_requested_as_of_other_than_the_resolution_time() -> None:
    with pytest.raises(ContractSemanticError, match="asked for"):
        _validate_graph_result(
            _graph_result(freshness=_freshness_wire(as_of=T0)),
            request=_graph_request(as_of=T1),
            canonical_resolution_time=T0,
        )


def test_graph_traversal_result_accepts_a_historical_traversal_served_at_the_instant_requested() -> None:
    _validate_graph_result(
        _graph_result(freshness=_freshness_wire(as_of=T1)),
        request=_graph_request(as_of=T1),
        canonical_resolution_time=T1,
    )


def test_validate_projection_freshness_is_exported_and_returns_the_parsed_instant() -> None:
    freshness = _graph_result().freshness
    assert sem_knowledge.validate_projection_freshness(freshness).isoformat() == "2024-01-01T00:00:00+00:00"
    assert "validate_projection_freshness" in sem_knowledge.__all__


# --- direct-entry safety -------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: sem_knowledge.validate_graph_traversal_input(None),
        lambda: sem_knowledge.validate_graph_traversal_input("not an input"),
        lambda: sem_knowledge.validate_graph_traversal_input(
            dataclasses.replace(_graph_request(), start="rec-1")
        ),
        lambda: sem_knowledge.validate_graph_traversal_input(
            dataclasses.replace(_graph_request(), start=("rec-1",))
        ),
        lambda: sem_knowledge.validate_graph_traversal_input(
            dataclasses.replace(_graph_request(), relation_types="relates_to")
        ),
        lambda: sem_knowledge.validate_graph_traversal_input(
            dataclasses.replace(_graph_request(), direction=123)
        ),
        lambda: sem_knowledge.validate_graph_traversal_input(
            dataclasses.replace(_graph_request(), depth_limit="1")
        ),
        lambda: sem_knowledge.validate_graph_traversal_input(
            dataclasses.replace(_graph_request(), node_limit=True)
        ),
        lambda: sem_knowledge.validate_graph_traversal_input(
            dataclasses.replace(_graph_request(), page="cont-1")
        ),
        lambda: _validate_graph_result("not a result"),
        lambda: sem_knowledge.validate_graph_traversal_result(
            _graph_result(), "not a request", "ws-1", T0, frozenset()
        ),
        lambda: sem_knowledge.validate_graph_traversal_result(
            _graph_result(), _graph_request(), 123, T0, frozenset()
        ),
        lambda: sem_knowledge.validate_graph_traversal_result(
            _graph_result(), _graph_request(), "ws-1", None, frozenset()
        ),
        lambda: sem_knowledge.validate_graph_traversal_result(
            _graph_result(), _graph_request(), "ws-1", T0, ["candidates"]
        ),
        lambda: _validate_graph_result(dataclasses.replace(_graph_result(), nodes=(object(),))),
        lambda: _validate_graph_result(dataclasses.replace(_graph_result(), edges=(object(),))),
        lambda: _validate_graph_result(dataclasses.replace(_graph_result(), nodes="nodes")),
        lambda: _validate_graph_result(dataclasses.replace(_graph_result(), freshness=None)),
        lambda: _validate_graph_result(dataclasses.replace(_graph_result(), ordering_basis=123)),
        lambda: _validate_graph_result(dataclasses.replace(_graph_result(), applied_node_limit="10")),
        lambda: _validate_graph_result(dataclasses.replace(_graph_result(), page="cont-1")),
        lambda: sem_knowledge.validate_projection_freshness(None),
        lambda: sem_knowledge.validate_projection_freshness(
            dataclasses.replace(_graph_result().freshness, projection_versions=["graph_index"])
        ),
        lambda: sem_knowledge.validate_projection_freshness(
            dataclasses.replace(_graph_result().freshness, projection_versions={"graph_index": 1})
        ),
        lambda: sem_knowledge.validate_projection_freshness(
            dataclasses.replace(_graph_result().freshness, projection_versions={1: "pv-1"})
        ),
    ],
)
def test_graph_direct_helpers_reject_malformed_types_as_contract_semantic_error(call: Any) -> None:
    with pytest.raises(ContractSemanticError):
        call()


def test_graph_traversal_result_revalidates_the_original_request() -> None:
    """The request is re-validated here, so a hand-built one cannot smuggle past the input
    rules and then be treated as the thing the result is checked against."""
    request = dataclasses.replace(
        _graph_request(),
        start=tuple(
            GraphTraversalInput.from_wire(
                {"start": [_record_version_reference_wire(record_id=f"rec-{i}") for i in range(65)]}
            ).start
        ),
    )
    with pytest.raises(ContractSemanticError, match="bounded range"):
        _validate_graph_result(_graph_result(), request=request)


def test_graph_traversal_result_rejects_a_node_carrying_a_non_record() -> None:
    node = dataclasses.replace(_graph_result().nodes[0], record="not a record")
    with pytest.raises(ContractSemanticError):
        _validate_graph_result(dataclasses.replace(_graph_result(), nodes=(node,)))


# --------------------------------------------------------------------------
# 5b. the history view: one rule, shared by knowledge.search and graph.traverse
# --------------------------------------------------------------------------
#
# `history` returns the versions that *were* canonical knowledge, so proving that is the
# whole bar: exact `l2`/`accepted`/`superseded`, `canonical` authority, a reviewer, and a
# supersession that had already happened by the canonical-resolution time. Both operations
# can return the same governed versions through different projections, so every case here
# runs against both -- a predicate the two spelled separately is a predicate that can drift,
# and a traversal must never hand back under `history` what a search would refuse.


def _history_through_knowledge_search(record: dict[str, Any], resolution_time: str) -> None:
    """Offer `record` as a `knowledge.search` history result at `resolution_time`."""
    sem_knowledge.validate_knowledge_search_result(
        _knowledge_search_result([record]),
        _knowledge_search_request(view="history"),
        "ws-1",
        resolution_time,
        {"history"},
    )


def _history_through_graph_traverse(record: dict[str, Any], resolution_time: str) -> None:
    """Offer the same `record` as a `graph.traverse` history node at `resolution_time`."""
    identity = record["provenance"]["identity"]
    reference = _record_version_reference_wire(
        record_id=identity["record_id"], version=identity["version"]
    )
    sem_knowledge.validate_graph_traversal_result(
        _graph_result(
            nodes=[_graph_node_wire(reference=reference, record=record)],
            freshness=_freshness_wire(as_of=resolution_time),
        ),
        _graph_request(view="history", start=[reference]),
        "ws-1",
        resolution_time,
        frozenset({"history"}),
    )


HISTORY_VIEW_OPERATIONS = [
    pytest.param(_history_through_knowledge_search, id="knowledge-search"),
    pytest.param(_history_through_graph_traverse, id="graph-traverse"),
]


@pytest.mark.parametrize("operation", HISTORY_VIEW_OPERATIONS)
def test_history_view_rejects_a_never_canonical_authority_level(operation: Any) -> None:
    """Being superseded is not a promotion. A version that only ever reached `accepted`
    authority was never citable canonical knowledge, and offering it under `history` would
    let a caller cite as settled something that never settled."""
    record = _history_knowledge_record_wire(authority_level="accepted")
    with pytest.raises(ContractSemanticError, match="were canonical knowledge"):
        operation(record, T0)


@pytest.mark.parametrize("operation", HISTORY_VIEW_OPERATIONS)
def test_history_view_rejects_a_record_carrying_no_reviewer(operation: Any) -> None:
    """A version nobody reviewed was never canonical knowledge.

    Defence in depth rather than the only line: `validate_governed_record` already refuses
    an `accepted` governance state with no reviewer, and it runs first on both paths, so
    this case is caught before the history rule restates it. The history rule states the
    complete bar anyway, so reading it does not require reconstructing which of the four
    axes some other validator happens to cover.
    """
    record = _history_knowledge_record_wire(reviewer=None)
    with pytest.raises(ContractSemanticError, match="reviewer"):
        operation(record, T0)


@pytest.mark.parametrize("operation", HISTORY_VIEW_OPERATIONS)
def test_history_view_rejects_supersession_after_the_resolution_time(operation: Any) -> None:
    """The check that makes the view honest: a version replaced only *after* the instant this
    read resolved at was still the canonical answer at that instant, so returning it as
    history misstates what the workspace knew then."""
    record = _history_knowledge_record_wire(superseded_at=T1)
    with pytest.raises(ContractSemanticError, match="still the canonical answer"):
        operation(record, T0)


@pytest.mark.parametrize("operation", HISTORY_VIEW_OPERATIONS)
def test_history_view_accepts_supersession_exactly_at_the_resolution_time(operation: Any) -> None:
    """The boundary is inclusive: a version superseded exactly at the resolution instant is
    already history at it."""
    operation(_history_knowledge_record_wire(superseded_at=T0), T0)


@pytest.mark.parametrize("operation", HISTORY_VIEW_OPERATIONS)
def test_history_view_accepts_supersession_before_the_resolution_time(operation: Any) -> None:
    """And the ordinary case: canonical, reviewed, and already replaced when the read
    resolved, which is exactly what `history` exists to return."""
    operation(_history_knowledge_record_wire(superseded_at=T0), T1)


# --------------------------------------------------------------------------
# 6. context_pack.build adversarial cases
#
# A Context Pack is content-addressed, so most of what follows is one of two shapes:
# "mutate one thing and re-sign, and the semantic rule must fire", or "sign and then
# mutate, and the digest must fire". Each case says which it is by whether it calls
# `_signed`/`_pack` (which re-signs) or mutates a signed document afterwards.
# --------------------------------------------------------------------------


def _mutate(document: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Deep-copy `document` and set a dotted `path`, descending into list indices.

    The Context Pack fixture nests arrays inside objects inside arrays, so unlike
    :func:`_with` this one accepts numeric segments (`sections.0.content`).
    """
    clone = copy.deepcopy(document)
    target: Any = clone
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if part.isdigit() else target[part]
    last = parts[-1]
    if last.isdigit():
        target[int(last)] = value
    else:
        target[last] = value
    return clone


def test_context_pack_canonical_pack_validates() -> None:
    """The positive case every negative case below is one mutation away from."""
    _validate_pack(_pack())


def test_context_pack_document_round_trip_validates() -> None:
    """And the same pack through the raw-wire entry point, which is the stricter path."""
    document = json.dumps(_signed(_context_pack_build_result_wire()))
    result = sem_knowledge.validate_context_pack_build_result_document(
        document,
        request=_context_pack_request(),
        expected_workspace_id="ws-1",
        expected_authority=GrantedAuthority.from_wire(_granted_authority_wire()),
        expected_scopes=set(EXPECTED_SCOPES),
        expected_purpose="assist.chat",
        expected_policy_versions=dict(EXPECTED_POLICY_VERSIONS),
        expected_authorized_candidate_set=_candidate_manifest(),
        canonical_resolution_time=T1,
        response_freshness=ProjectionFreshness.from_wire(_freshness_wire(as_of=T1)),
    )
    assert result.pack_id == result.reproducibility.artifact_checksum


# --- 6a. mode fails closed ----------------------------------------------------


@pytest.mark.parametrize("mode", ["immutable_snapshot", "returned_artifact"])
def test_context_pack_input_rejects_a_named_non_v1_mode(mode: str) -> None:
    """`immutable_snapshot` asks for persistence this operation does not have, and
    `returned_artifact` was never a wire mode at all: both are refused by name."""
    input_ = ContextPackBuildInput.from_wire(
        {"query": "hello", "mode": mode, "token_budget": TOKEN_BUDGET}
    )
    with pytest.raises(ContractSemanticError, match="not a v1 ContextPackMode"):
        sem_knowledge.validate_context_pack_build_input(input_)


def test_context_pack_input_rejects_an_unknown_mode() -> None:
    input_ = ContextPackBuildInput.from_wire(
        {"query": "hello", "mode": "some_future_mode", "token_budget": TOKEN_BUDGET}
    )
    with pytest.raises(ContractSemanticError, match="not a recognized ContextPackMode"):
        sem_knowledge.validate_context_pack_build_input(input_)


def test_context_pack_input_rejects_a_malformed_mode() -> None:
    input_ = ContextPackBuildInput.from_wire(
        {"query": "hello", "mode": "Not Valid!", "token_budget": TOKEN_BUDGET}
    )
    with pytest.raises(ContractSemanticError, match="not a valid ContextPackMode"):
        sem_knowledge.validate_context_pack_build_input(input_)


def test_context_pack_input_accepts_only_deterministic_view() -> None:
    assert sem_knowledge.CONTEXT_PACK_MODES == frozenset({"deterministic_view"})
    sem_knowledge.validate_context_pack_build_input(_context_pack_request())


@pytest.mark.parametrize("mode", ["immutable_snapshot", "returned_artifact", "future_mode"])
def test_context_pack_result_mode_fails_closed(mode: str) -> None:
    """The mutation probe: a pack that names any other mode must fail, not be tolerated."""
    result = _pack(mode=mode)
    with pytest.raises(ContractSemanticError, match="ContextPackMode"):
        _validate_pack(result)


def test_context_pack_rejected_modes_are_named() -> None:
    assert sem_knowledge.CONTEXT_PACK_REJECTED_MODES == frozenset(
        {"immutable_snapshot", "returned_artifact"}
    )
    assert sem_knowledge.CONTEXT_PACK_MODES.isdisjoint(sem_knowledge.CONTEXT_PACK_REJECTED_MODES)


# --- 6b. the request carries no view, time, page, persistence, or job control --


REJECTED_INPUT_CONTROLS: tuple[tuple[str, Any], ...] = (
    ("view", "candidates"),
    ("as_of", T0),
    ("page", {"continuation_token": "tok-1"}),
    ("limit", 10),
    ("continuation_token", "tok-1"),
    ("persist", True),
    ("persistence", "durable"),
    ("expires_at", T1),
    ("expiry", T1),
    ("retention_policy", "keep_forever"),
    ("snapshot", True),
    ("snapshot_id", "snap-1"),
    ("async", True),
    ("job", {"job_id": "job-1"}),
    ("job_id", "job-1"),
)


@pytest.mark.parametrize("field_name,value", REJECTED_INPUT_CONTROLS)
def test_context_pack_input_refuses_a_control_it_does_not_have(
    field_name: str, value: Any
) -> None:
    """The tolerant decoder would silently drop these, handing back a synchronous,
    non-persisted, unpaginated, current-view pack to a caller who asked for something
    else."""
    payload = {
        "query": "hello",
        "mode": "deterministic_view",
        "token_budget": TOKEN_BUDGET,
        field_name: value,
    }
    assert not _is_schema_valid("ContextPackBuildInput", payload)
    # The tolerant decoder really does drop it, which is exactly why the raw scan exists.
    assert ContextPackBuildInput.from_wire(payload) == _context_pack_request()
    with pytest.raises(ContractSemanticError, match="accepts no such control"):
        sem_knowledge.decode_context_pack_build_input(payload)


def test_context_pack_rejected_input_fields_are_frozen() -> None:
    assert set(sem_knowledge.CONTEXT_PACK_REJECTED_INPUT_FIELDS) == {
        name for name, _ in REJECTED_INPUT_CONTROLS
    }


NESTED_UNKNOWN_PAYLOADS: tuple[tuple[str, Any], ...] = (
    ("object", {"nested": {"snapshot_id": "snap-1"}}),
    ("array of objects", {"nested": [{"snapshot_id": "snap-1"}]}),
    ("every reserved name at once", {name: "x" for name, _ in REJECTED_INPUT_CONTROLS}),
    ("deeply nested", {"a": {"b": {"c": {"persist": True, "job_id": "job-1"}}}}),
)


@pytest.mark.parametrize("shape,nested", NESTED_UNKNOWN_PAYLOADS, ids=[s for s, _ in NESTED_UNKNOWN_PAYLOADS])
def test_context_pack_input_accepts_a_reserved_name_inside_an_unknown_field(
    shape: str, nested: Any
) -> None:
    """ADR-038 compatibility, stated as a positive case.

    An older production client must ignore an additive unknown optional field rather than
    fail on it, and the tolerant decoder drops such a field *whole* -- everything under it
    goes with it. A reserved control name nested inside `future_envelope` therefore never
    reaches the five scalar fields `ContextPackBuildInput` has: there is no position for it
    to land in, so it cannot be honoured in part, and rejecting it would fail a document a
    compatible later minor release is entitled to send.
    """
    payload = {
        "query": "hello",
        "mode": "deterministic_view",
        "token_budget": TOKEN_BUDGET,
        "future_envelope": nested,
    }
    decoded = sem_knowledge.decode_context_pack_build_input(payload)
    assert decoded == _context_pack_request()
    # And the nested name really is gone from the value the validator ever sees.
    assert dataclasses.asdict(decoded) == {
        "query": "hello",
        "mode": "deterministic_view",
        "token_budget": TOKEN_BUDGET,
        "domain_scope": None,
        "record_type": None,
    }


@pytest.mark.parametrize("field_name,value", REJECTED_INPUT_CONTROLS)
def test_context_pack_input_refuses_a_control_even_beside_an_unknown_field(
    field_name: str, value: Any
) -> None:
    """The narrowed scan still refuses every reserved name at the depth that matters, and an
    additive unknown neighbour does not shield it."""
    payload = {
        "query": "hello",
        "mode": "deterministic_view",
        "token_budget": TOKEN_BUDGET,
        "future_envelope": {"nested": [{"snapshot_id": "snap-1"}]},
        field_name: value,
    }
    with pytest.raises(ContractSemanticError, match="accepts no such control"):
        sem_knowledge.decode_context_pack_build_input(payload)


def test_context_pack_input_decodes_a_clean_payload() -> None:
    decoded = sem_knowledge.decode_context_pack_build_input(
        {
            "query": "hello",
            "mode": "deterministic_view",
            "token_budget": TOKEN_BUDGET,
            "domain_scope": "personal.preferences",
            "record_type": "memory.fact",
        }
    )
    assert decoded.domain_scope == "personal.preferences"
    assert decoded.record_type == "memory.fact"


def test_context_pack_build_input_declares_exactly_five_properties() -> None:
    """A schema-level restatement of the same rule: the removed controls are gone from the
    contract, not merely refused by a validator."""
    schema = json.loads((SCHEMA_DIR / "context-pack.schema.json").read_text(encoding="utf-8"))
    properties = schema["$defs"]["ContextPackBuildInput"]["properties"]
    assert set(properties) == {"query", "mode", "token_budget", "domain_scope", "record_type"}


def test_context_pack_result_declares_no_persistence_vocabulary() -> None:
    schema = json.loads((SCHEMA_DIR / "context-pack.schema.json").read_text(encoding="utf-8"))
    declared: set[str] = set()
    for definition in schema["$defs"].values():
        declared.update(definition.get("properties", {}))
    assert declared.isdisjoint(sem_knowledge.CONTEXT_PACK_REJECTED_INPUT_FIELDS - {"view"})
    # `view` survives only on the normalized request, where it records a resolved value
    # rather than accepting a caller-selected one.
    assert "view" in schema["$defs"]["ContextPackNormalizedRequest"]["properties"]
    assert "view" not in schema["$defs"]["ContextPackBuildInput"]["properties"]


# --- 6c. token budget ---------------------------------------------------------


@pytest.mark.parametrize("budget", [0, -1, 10_000_001])
def test_context_pack_input_rejects_an_out_of_range_budget(budget: int) -> None:
    input_ = ContextPackBuildInput.from_wire(
        {"query": "hello", "mode": "deterministic_view", "token_budget": budget}
    )
    with pytest.raises(ContractSemanticError, match="bounded range"):
        sem_knowledge.validate_context_pack_build_input(input_)
    assert not _is_schema_valid(
        "ContextPackBuildInput",
        {"query": "hello", "mode": "deterministic_view", "token_budget": budget},
    )


def test_context_pack_input_rejects_a_boolean_budget() -> None:
    """Python makes `True` an integer equal to 1, so a bool would otherwise pass as a
    one-token budget."""
    input_ = dataclasses.replace(_context_pack_request(), token_budget=True)
    with pytest.raises(ContractSemanticError, match="expected an integer"):
        sem_knowledge.validate_context_pack_build_input(input_)


def test_context_pack_token_count_still_allows_zero_for_an_observation() -> None:
    """The budget is positive; an observed count is not. A no-results pack used nothing."""
    empty = _signed(
        _context_pack_build_result_wire(
            sections=[],
            evidence=[],
            records=[],
            history=[],
            context_models=[],
            citations=[],
            budget=_budget_wire(tokens_used=0),
            reproducibility=_reproducibility_wire(evidence_versions=[], record_versions=[]),
        )
    )
    _validate_pack(ContextPackBuildResult.from_wire(empty))


def test_context_pack_budget_must_equal_the_requested_budget() -> None:
    result = _pack(
        budget=_budget_wire(token_budget=500),
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(token_budget=500)
        ),
    )
    with pytest.raises(ContractSemanticError, match="budget.token_budget"):
        _validate_pack(result)


def test_context_pack_tokens_used_must_equal_the_section_sum() -> None:
    result = _pack(budget=_budget_wire(tokens_used=SECTION_TOKENS + 1))
    with pytest.raises(ContractSemanticError, match="not the sum"):
        _validate_pack(result)


def test_context_pack_tokens_used_must_not_exceed_the_budget() -> None:
    result = _pack(
        sections=[_section_wire(token_count=TOKEN_BUDGET + 1)],
        budget=_budget_wire(tokens_used=TOKEN_BUDGET + 1),
    )
    with pytest.raises(ContractSemanticError, match="exceeds budget.token_budget"):
        _validate_pack(result)


def test_context_pack_section_with_content_must_cost_tokens() -> None:
    result = _pack(sections=[_section_wire(token_count=0)], budget=_budget_wire(tokens_used=0))
    with pytest.raises(ContractSemanticError, match="costs nothing to send"):
        _validate_pack(result)


def test_context_pack_section_token_count_covers_only_content() -> None:
    """A title is not counted, so adding one must not change the accounting."""
    _validate_pack(_pack(sections=[_section_wire(title="Summary")]))


# --- 6d. direct safety: a hand-built value never raises a raw Python error -----


MALFORMED_CALLER_CONTEXT: tuple[tuple[str, Any], ...] = (
    ("request", None),
    ("request", {"query": "hello"}),
    ("expected_workspace_id", 42),
    ("expected_authority", "principal-1"),
    ("expected_scopes", "memory:read"),
    ("expected_scopes", set()),
    ("expected_purpose", None),
    ("expected_policy_versions", ["acl"]),
    ("expected_policy_versions", {}),
    ("canonical_resolution_time", 0),
    ("response_freshness", {"as_of": T1}),
)


@pytest.mark.parametrize("argument,value", MALFORMED_CALLER_CONTEXT)
def test_context_pack_result_validator_guards_its_caller_context(
    argument: str, value: Any
) -> None:
    """A wrongly typed expectation is a `ContractSemanticError`, never a raw Python error."""
    with pytest.raises(ContractSemanticError):
        _validate_pack(_pack(), **{argument: value})


@pytest.mark.parametrize("result", [None, {"pack_id": "x"}, 7, "a pack"])
def test_context_pack_result_validator_guards_its_result_argument(result: Any) -> None:
    with pytest.raises(ContractSemanticError):
        _validate_pack(result)


MALFORMED_NESTED_FIELDS: tuple[str, ...] = (
    "mode",
    "query",
    "pack_id",
    "sections",
    "evidence",
    "records",
    "history",
    "context_models",
    "citations",
    "conflicts",
    "uncertainties",
    "omissions",
    "budget",
    "reproducibility",
    "fresh_authorization_required",
)


@pytest.mark.parametrize("field_name", MALFORMED_NESTED_FIELDS)
def test_context_pack_result_rejects_a_wrongly_typed_field_without_a_python_error(
    field_name: str,
) -> None:
    """Every top-level field replaced by an object of the wrong type, one at a time.

    `ContractSemanticError` and nothing else -- no `TypeError` from a `len()`, no
    `AttributeError` from a field access, no `KeyError` from a lookup.
    """
    broken = dataclasses.replace(_pack(), **{field_name: object()})
    with pytest.raises(ContractSemanticError):
        _validate_pack(broken)


def test_context_pack_result_rejects_a_wrongly_typed_nested_value() -> None:
    result = _pack()
    broken = dataclasses.replace(
        result,
        reproducibility=dataclasses.replace(result.reproducibility, model_versions=["not-a-map"]),
    )
    with pytest.raises(ContractSemanticError, match="expected a mapping"):
        _validate_pack(broken)


@pytest.mark.parametrize("content", [None, [], "opaque", 7])
def test_context_pack_guards_a_records_opaque_content(content: Any) -> None:
    """`validate_governed_record` deliberately never inspects opaque application content,
    so nothing above this layer establishes that it is even a mapping -- and the digest
    step re-encodes the whole result, which would otherwise surface a raw `AttributeError`
    from the encoder rather than a `ContractSemanticError` from the contract."""
    result = _pack()
    for partition, items in (
        ("records", result.records),
        ("history", result.history),
        ("context_models", result.context_models),
    ):
        broken = dataclasses.replace(
            result, **{partition: (dataclasses.replace(items[0], content=content),)}
        )
        with pytest.raises(ContractSemanticError, match="expected a mapping"):
            _validate_pack(broken)


def _replace_first_record(result: ContextPackBuildResult, partition: str, **changes: Any) -> Any:
    items = getattr(result, partition)
    return dataclasses.replace(
        result,
        **{partition: (dataclasses.replace(items[0], **changes),)},
    )


def _pack_with_provenance(result: ContextPackBuildResult, partition: str, history: Any) -> Any:
    record = getattr(result, partition)[0]
    return _replace_first_record(
        result, partition, provenance=dataclasses.replace(record.provenance, history=history)
    )


def test_context_pack_repaired_surfaces_never_leak_a_raw_exception() -> None:
    """Every public Context Pack validator is a direct entry point: a hand-built DTO reaches
    it with no tolerant decode in front, so each repaired surface must turn a wrongly typed
    value into `ContractSemanticError` rather than let an `AttributeError`/`TypeError`/
    bare `ValueError` escape the contract layer.

    The assertion is on the *exact* type, not `isinstance`. `ContractSemanticError` is
    itself a `ValueError` subclass, so an `isinstance` check would pass for a raw
    `ValueError` raised incidentally by `datetime.fromisoformat` or `int()` -- precisely the
    leak this is meant to catch.
    """
    result = _pack()
    artifact = result.evidence[0]
    authority = result.reproducibility.authorization_context.authority

    broken_packs: list[tuple[str, Any]] = [
        (
            "evidence permission_labels is not a sequence",
            dataclasses.replace(
                result, evidence=(dataclasses.replace(artifact, permission_labels="labels"),)
            ),
        ),
        (
            "evidence provenance_history is not a sequence",
            dataclasses.replace(
                result, evidence=(dataclasses.replace(artifact, provenance_history=7),)
            ),
        ),
        (
            "evidence provenance_history holds a non-entry",
            dataclasses.replace(
                result, evidence=(dataclasses.replace(artifact, provenance_history=(None,)),)
            ),
        ),
        ("record history is not a sequence", _pack_with_provenance(result, "records", "history")),
        (
            "record history holds a non-entry",
            _pack_with_provenance(result, "history", ("created",)),
        ),
        (
            "context_models history holds a non-entry",
            _pack_with_provenance(result, "context_models", (7,)),
        ),
        (
            "authority roles is not a sequence",
            dataclasses.replace(
                result,
                reproducibility=dataclasses.replace(
                    result.reproducibility,
                    authorization_context=dataclasses.replace(
                        result.reproducibility.authorization_context,
                        authority=dataclasses.replace(authority, roles=None),
                    ),
                ),
            ),
        ),
        (
            "authority capabilities is not a sequence",
            dataclasses.replace(
                result,
                reproducibility=dataclasses.replace(
                    result.reproducibility,
                    authorization_context=dataclasses.replace(
                        result.reproducibility.authorization_context,
                        authority=dataclasses.replace(authority, capabilities=3),
                    ),
                ),
            ),
        ),
    ]

    for description, broken in broken_packs:
        with pytest.raises(ContractSemanticError) as caught:
            _validate_pack(broken)
        assert type(caught.value) is ContractSemanticError, description


def test_context_pack_evidence_validator_is_direct_safe() -> None:
    """The same property on `validate_evidence_artifact`, which a Context Pack composes and
    which a caller may also reach on its own."""
    artifact = _pack().evidence[0]
    for changes in (
        {"tombstoned": "yes"},
        {"permission_labels": 1},
        {"provenance_history": None},
        {"provenance_history": ({"action": "created"},)},
    ):
        with pytest.raises(ContractSemanticError) as caught:
            sem_evidence.validate_evidence_artifact(dataclasses.replace(artifact, **changes))
        assert type(caught.value) is ContractSemanticError, changes


def test_context_pack_citation_validator_is_direct_safe() -> None:
    with pytest.raises(ContractSemanticError):
        sem_knowledge.validate_context_pack_citation(None)
    with pytest.raises(ContractSemanticError):
        sem_knowledge.validate_context_pack_citation({"citation_id": "cit-1"})


def test_context_pack_input_validator_is_direct_safe() -> None:
    with pytest.raises(ContractSemanticError):
        sem_knowledge.validate_context_pack_build_input(None)
    with pytest.raises(ContractSemanticError):
        sem_knowledge.validate_context_pack_build_input(
            dataclasses.replace(_context_pack_request(), query=7)
        )


def test_context_pack_digest_helper_is_direct_safe() -> None:
    with pytest.raises(ContractSemanticError, match="expected a mapping"):
        sem_knowledge.compute_context_pack_artifact_digest(None)
    with pytest.raises(ContractSemanticError, match="pack_id is absent"):
        sem_knowledge.compute_context_pack_artifact_digest({"reproducibility": {}})
    with pytest.raises(ContractSemanticError, match="artifact_checksum is absent"):
        sem_knowledge.compute_context_pack_artifact_digest(
            {"pack_id": PLACEHOLDER_DIGEST, "reproducibility": {}}
        )


# --- 6e. normalized request binding -------------------------------------------


def test_context_pack_normalized_request_mode_must_bind_to_the_request() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(mode="immutable_snapshot")
        )
    )
    with pytest.raises(ContractSemanticError, match="normalized_request.mode"):
        _validate_pack(result)


def test_context_pack_normalized_request_view_must_be_current_canonical() -> None:
    for view in ("candidates", "history"):
        result = _pack(
            reproducibility=_reproducibility_wire(
                normalized_request=_normalized_request_wire(view=view)
            )
        )
        with pytest.raises(ContractSemanticError, match="normalized_request.view"):
            _validate_pack(result)


def test_context_pack_normalized_request_rejects_an_unrecognized_view() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(view="some_future_view")
        )
    )
    with pytest.raises(ContractSemanticError, match="GovernedRecordView"):
        _validate_pack(result)


def test_context_pack_normalized_request_budget_must_bind_to_the_request() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(token_budget=999)
        )
    )
    with pytest.raises(ContractSemanticError, match="normalized_request.token_budget"):
        _validate_pack(result)


def test_context_pack_normalized_request_rejects_an_empty_normalized_query() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(normalized_query="")
        )
    )
    with pytest.raises(ContractSemanticError, match="normalized_query"):
        _validate_pack(result)


def test_context_pack_normalized_request_requires_a_normalization_version() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(normalization_version="Not Valid!")
        )
    )
    with pytest.raises(ContractSemanticError, match="normalization_version"):
        _validate_pack(result)


def test_context_pack_normalized_request_filters_must_bind_to_the_request() -> None:
    """Present exactly when the request carried one, absent exactly when it did not."""
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(domain_scope="personal.preferences")
        )
    )
    with pytest.raises(ContractSemanticError, match="normalized_request.domain_scope"):
        _validate_pack(result)

    filtered_request = _context_pack_request(domain_scope="personal.preferences")
    with pytest.raises(ContractSemanticError, match="normalized_request.domain_scope"):
        _validate_pack(_pack(), request=filtered_request)


def test_context_pack_honours_a_requested_filter_end_to_end() -> None:
    request = _context_pack_request(
        domain_scope="personal.preferences", record_type="memory.fact"
    )
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(
                domain_scope="personal.preferences", record_type="memory.fact"
            )
        )
    )
    _validate_pack(result, request=request)


@pytest.mark.parametrize(
    "partition,builder",
    [
        ("records", _pack_record_wire),
        ("history", _pack_history_wire),
        ("context_models", _pack_context_model_wire),
    ],
)
def test_context_pack_rejects_a_record_outside_the_requested_domain_scope(
    partition: str, builder: Any
) -> None:
    request = _context_pack_request(domain_scope="work.projects")
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(domain_scope="work.projects")
        ),
        **{partition: [builder(domain_scope="personal.preferences")]},
    )
    with pytest.raises(ContractSemanticError, match="domain_scope"):
        _validate_pack(result, request=request)


@pytest.mark.parametrize(
    "partition,builder",
    [
        ("records", _pack_record_wire),
        ("history", _pack_history_wire),
        ("context_models", _pack_context_model_wire),
    ],
)
def test_context_pack_rejects_a_record_outside_the_requested_record_type(
    partition: str, builder: Any
) -> None:
    request = _context_pack_request(record_type="memory.decision")
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(record_type="memory.decision")
        ),
        **{partition: [builder(record_type="memory.fact")]},
    )
    with pytest.raises(ContractSemanticError, match="record_type"):
        _validate_pack(result, request=request)


# --- 6f. authorization context is exact and caller-vouched --------------------


def test_context_pack_authorization_workspace_must_match() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(workspace_id="ws-other")
        )
    )
    with pytest.raises(ContractSemanticError, match="workspace_id"):
        _validate_pack(result)


def test_context_pack_authorization_principal_must_match() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                authority=_granted_authority_wire(principal_id="principal-other")
            )
        )
    )
    with pytest.raises(ContractSemanticError, match="principal_id"):
        _validate_pack(result)


@pytest.mark.parametrize(
    "roles",
    [
        ["analyst"],
        ["analyst", "reader", "admin"],
        ["analyst", "auditor"],
    ],
)
def test_context_pack_authorization_roles_must_match_exactly(roles: list[str]) -> None:
    """A subset, a superset, and a substitution: each is a different authority."""
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                authority=_granted_authority_wire(roles=sorted(roles))
            )
        )
    )
    with pytest.raises(ContractSemanticError, match="roles"):
        _validate_pack(result)


def test_context_pack_authorization_capability_id_is_granted_once() -> None:
    """A capability id appears once, whatever version it carries.

    A `GrantedAuthority` on a response envelope is refused by
    `validate_granted_authority` for a repeated capability id. This rule read
    `(id, version)` pairs instead, so `memory.read` at 1.0 *and* 2.0 was a legal
    authority inside a Context Pack and an illegal one on the envelope that
    delivered it -- the same value valid or invalid depending on position, which
    is not something a contract can mean.
    """
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                authority=_granted_authority_wire(
                    capabilities=[
                        {"id": "memory.read", "version": "1.0"},
                        {"id": "memory.read", "version": "2.0"},
                        {"id": "workspace.read", "version": "1.0"},
                    ]
                )
            )
        )
    )
    with pytest.raises(ContractSemanticError, match="repeats capability id 'memory.read'"):
        _validate_pack(result)


def test_context_pack_authorization_capability_id_repeat_is_refused_by_the_authority_rule(
) -> None:
    """Refused by the uniqueness rule itself, not by the expected-set comparison.

    The duplicate must fail on its own terms. Passing the very same authority as
    the expectation removes the set comparison as an explanation: if uniqueness
    were not enforced, the two sides would agree exactly and the pack would be
    accepted.
    """
    duplicated = [
        {"id": "memory.read", "version": "1.0"},
        {"id": "memory.read", "version": "2.0"},
        {"id": "workspace.read", "version": "1.0"},
    ]
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                authority=_granted_authority_wire(capabilities=duplicated)
            )
        )
    )
    with pytest.raises(ContractSemanticError, match="names each capability once"):
        _validate_pack(
            result,
            expected_authority=GrantedAuthority.from_wire(
                _granted_authority_wire(capabilities=duplicated)
            ),
        )


def test_context_pack_authorization_capability_version_is_part_of_identity() -> None:
    """A capability at the wrong version is not the grant the caller expected.

    This used to be guaranteed by the fixture's shape rather than by an
    assertion: it carried one id at two versions, so a comparison that collapsed
    to ids alone would have lost an entry and failed. Unique ids removed that
    guarantee -- collapse this comparison to ids and the pack below matches -- so
    the property is now asserted directly rather than inferred from test data.
    """
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                authority=_granted_authority_wire(
                    capabilities=[
                        {"id": "memory.read", "version": "1.0"},
                        {"id": "memory.write", "version": "9.9"},
                        {"id": "workspace.read", "version": "1.0"},
                    ]
                )
            )
        )
    )
    with pytest.raises(ContractSemanticError, match="expected capability set"):
        _validate_pack(result)


def test_context_pack_authorization_accepts_distinct_capability_ids() -> None:
    """The control: unique ids in ascending order remain valid.

    Without this, the rule above could be satisfied by refusing every authority.
    """
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                authority=_granted_authority_wire()
            )
        )
    )
    _validate_pack(result)


def test_context_pack_authorization_capabilities_stay_ordered() -> None:
    """Uniqueness did not displace the deterministic ordering requirement.

    A stored pack is content-addressed, so its authority has one canonical
    spelling; ids that are unique but out of order are still refused.
    """
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                authority=_granted_authority_wire(
                    capabilities=[
                        {"id": "workspace.read", "version": "1.0"},
                        {"id": "memory.read", "version": "1.0"},
                    ]
                )
            )
        )
    )
    with pytest.raises(ContractSemanticError, match="ascending"):
        _validate_pack(result)


def test_context_pack_authorization_scopes_must_match_exactly() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(scopes=["memory:read"])
        )
    )
    with pytest.raises(ContractSemanticError, match="scopes"):
        _validate_pack(result)


def test_context_pack_authorization_purpose_must_match() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(purpose="analytics.export")
        )
    )
    with pytest.raises(ContractSemanticError, match="purpose"):
        _validate_pack(result)


def test_context_pack_authorization_policy_versions_must_match_exactly() -> None:
    for policies in (
        {"acl": "pv-acl-1"},
        {"acl": "pv-acl-2", "sensitivity": "pv-sens-1"},
        {"acl": "pv-acl-1", "sensitivity": "pv-sens-1", "extra": "pv-extra-1"},
    ):
        result = _pack(
            reproducibility=_reproducibility_wire(
                authorization_context=_authorization_context_wire(policy_versions=policies)
            )
        )
        with pytest.raises(ContractSemanticError, match="policy_versions"):
            _validate_pack(result)


def test_context_pack_authorization_requires_at_least_one_policy_version() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(policy_versions={})
        )
    )
    with pytest.raises(ContractSemanticError, match="names no policy"):
        _validate_pack(result)


UNSORTED_AUTHORITY_CASES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("roles", {"roles": ["reader", "analyst"]}),
    ("roles", {"roles": ["analyst", "analyst", "reader"]}),
    (
        "capabilities",
        {
            "capabilities": [
                {"id": "workspace.read", "version": "1.0"},
                {"id": "memory.read", "version": "1.0"},
                {"id": "memory.read", "version": "1.1"},
            ]
        },
    ),
    (
        "capabilities",
        {
            "capabilities": [
                {"id": "memory.read", "version": "1.1"},
                {"id": "memory.read", "version": "1.0"},
                {"id": "workspace.read", "version": "1.0"},
            ]
        },
    ),
    (
        "capabilities",
        {
            "capabilities": [
                {"id": "memory.read", "version": "1.0"},
                {"id": "memory.read", "version": "1.0"},
                {"id": "memory.read", "version": "1.1"},
                {"id": "workspace.read", "version": "1.0"},
            ]
        },
    ),
)


@pytest.mark.parametrize("field_name,override", UNSORTED_AUTHORITY_CASES)
def test_context_pack_authority_arrays_must_be_sorted_and_unique(
    field_name: str, override: dict[str, Any]
) -> None:
    """The stored arrays are what gets hashed, so their order has to be a function of the
    content even though the *comparison* against the caller's expectation is set-based."""
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                authority=_granted_authority_wire(**override)
            )
        )
    )
    with pytest.raises(ContractSemanticError, match=field_name):
        _validate_pack(result)


@pytest.mark.parametrize(
    "scopes",
    [
        ["workspace:read", "memory:read"],
        ["memory:read", "memory:read", "workspace:read"],
    ],
)
def test_context_pack_scopes_must_be_sorted_and_unique(scopes: list[str]) -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(scopes=scopes)
        )
    )
    with pytest.raises(ContractSemanticError, match="scope"):
        _validate_pack(result)


def test_context_pack_caller_expectation_need_not_be_sorted() -> None:
    """The caller vouches for a membership set; only the artifact has to be ordered."""
    _validate_pack(
        _pack(),
        expected_authority=GrantedAuthority.from_wire(
            _granted_authority_wire(
                roles=["reader", "analyst"],
                capabilities=[
                    {"id": "workspace.read", "version": "1.0"},
                    {"id": "memory.write", "version": "1.1"},
                    {"id": "memory.read", "version": "1.0"},
                ],
            )
        ),
        expected_scopes=["workspace:read", "memory:read"],
    )


# --- 6f-bis. stored authority cardinalities -----------------------------------
#
# `GrantedAuthority` bounds both arrays (`roles` maxItems 64, `capabilities` maxItems 256),
# and the tolerant decoder applies neither. Without the semantic restatement an over-long
# stored authority context passes here while failing strict JSON Schema -- the exact drift
# every other ceiling in this module exists to prevent.

AUTHORITY_ROLES_MAX = 64
AUTHORITY_CAPABILITIES_MAX = 256


def _many_roles(count: int) -> list[str]:
    """`count` distinct roles already in ascending UTF-16 order (ASCII, fixed width)."""
    return [f"role-{index:03d}" for index in range(count)]


def _many_capabilities(count: int) -> list[dict[str, Any]]:
    """`count` distinct capabilities already in ascending `(id, version)` order."""
    return [{"id": f"cap.a{index:03d}", "version": "1.0"} for index in range(count)]


@pytest.mark.parametrize(
    "field_name,count,build",
    [
        ("roles", AUTHORITY_ROLES_MAX, _many_roles),
        ("capabilities", AUTHORITY_CAPABILITIES_MAX, _many_capabilities),
    ],
)
def test_context_pack_accepts_authority_arrays_at_the_maximum(
    field_name: str, count: int, build: Any
) -> None:
    authority = _granted_authority_wire(**{field_name: build(count)})
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(authority=authority)
        )
    )
    _validate_pack(result, expected_authority=GrantedAuthority.from_wire(authority))


@pytest.mark.parametrize(
    "field_name,count,build",
    [
        ("roles", AUTHORITY_ROLES_MAX + 1, _many_roles),
        ("capabilities", AUTHORITY_CAPABILITIES_MAX + 1, _many_capabilities),
    ],
)
def test_context_pack_rejects_authority_arrays_past_the_maximum(
    field_name: str, count: int, build: Any
) -> None:
    authority = _granted_authority_wire(**{field_name: build(count)})
    document = _signed(
        _context_pack_build_result_wire(
            reproducibility=_reproducibility_wire(
                authorization_context=_authorization_context_wire(authority=authority)
            )
        )
    )
    assert not _is_schema_valid("ContextPackBuildResult", document)
    expected = GrantedAuthority.from_wire(authority)
    # Trusted-DTO entry point.
    with pytest.raises(ContractSemanticError, match=f"{field_name} carries {count} entries"):
        _validate_pack(
            ContextPackBuildResult.from_wire(document), expected_authority=expected
        )
    # Raw-document entry point.
    with pytest.raises(ContractSemanticError, match=f"{field_name} carries {count} entries"):
        _validate_pack_document(document, expected_authority=expected)


def test_context_pack_requires_pre_ranking_authorization() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                pre_ranking_authorization_enforced=False
            )
        )
    )
    with pytest.raises(ContractSemanticError, match="pre_ranking_authorization_enforced"):
        _validate_pack(result)


@pytest.mark.parametrize(
    "checksum",
    [
        "SHA256:" + "1a" * 32,
        "sha256:" + "1A" * 32,
        "sha256:" + "1a" * 31,
        "md5:" + "1a" * 16,
        "not-a-digest",
    ],
)
def test_context_pack_rejects_a_malformed_candidate_set_checksum(checksum: str) -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                authorized_candidate_set_checksum=checksum
            )
        )
    )
    with pytest.raises(ContractSemanticError, match="authorized_candidate_set_checksum"):
        _validate_pack(result)


def test_context_pack_carries_no_audit_reference() -> None:
    """A per-request audit id in a content-addressed artifact would give two identical
    builds two identities; the response envelope owns audit linkage instead."""
    schema = json.loads((SCHEMA_DIR / "context-pack.schema.json").read_text(encoding="utf-8"))
    for definition in schema["$defs"].values():
        assert "audit_reference" not in definition.get("properties", {})


# --- 6f-ter. the authorized candidate set is recomputed, never taken on trust ---
#
# `authorization_context.authorized_candidate_set_checksum` is the one claim in a pack that
# byte-level integrity cannot touch: it is a digest of something that never appears in the
# artifact. Checking it therefore means recomputing it from an independent manifest of the
# frontier -- the complete post-authorization candidate set, frozen before the first ranking,
# reranking, selection, or budget decision. Everything below is about that recomputation and
# about the subset rule that makes "nothing enters a pack afterwards" checkable.

CANDIDATE_SET_FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "context-pack-authorized-candidate-set-v1.json")
    .read_text(encoding="utf-8")
)

EMPTY_CANDIDATE_SET_DIGEST = (
    "sha256:666dd0b418f32f6fc03a5f87e430efaf6f9a6e5d50569b5fd74eb87e8b864b41"
)
EMPTY_CANDIDATE_SET_BYTES = (
    b'{"candidates":[],"format":"omnivia.context-pack.authorized-candidate-set.v1",'
    b'"workspace_id":"ws-1"}'
)


def _candidate_checksum(manifest: ContextPackAuthorizedCandidateSetManifest) -> str:
    return sem_knowledge.compute_authorized_candidate_set_checksum(manifest)


def test_authorized_candidate_set_empty_digest_is_frozen() -> None:
    """The empty frontier is valid, and its canonical bytes are stated rather than implied:
    a frozen digest whose preimage nobody can write down is not independently checkable."""
    manifest = _candidate_manifest(candidates=())
    assert _candidate_checksum(manifest) == EMPTY_CANDIDATE_SET_DIGEST
    assert (
        hashlib.sha256(EMPTY_CANDIDATE_SET_BYTES).hexdigest()
        == EMPTY_CANDIDATE_SET_DIGEST.removeprefix("sha256:")
    )


def _manifest_from_wire(document: Any) -> ContextPackAuthorizedCandidateSetManifest:
    return ContextPackAuthorizedCandidateSetManifest.from_wire(document)


@pytest.mark.parametrize(
    "vector",
    CANDIDATE_SET_FIXTURE["accepted"],
    ids=[vector["id"] for vector in CANDIDATE_SET_FIXTURE["accepted"]],
)
def test_authorized_candidate_set_accepted_vector(vector: dict[str, Any]) -> None:
    """Every accepted vector is schema-valid and digests to exactly the frozen value."""
    assert _is_schema_valid("ContextPackAuthorizedCandidateSetManifest", vector["manifest"])
    assert _candidate_checksum(_manifest_from_wire(vector["manifest"])) == vector["checksum"]


@pytest.mark.parametrize(
    "vector",
    CANDIDATE_SET_FIXTURE["rejected"],
    ids=[vector["id"] for vector in CANDIDATE_SET_FIXTURE["rejected"]],
)
def test_authorized_candidate_set_rejected_vector(vector: dict[str, Any]) -> None:
    """Every rejected vector is refused by exactly the layer the fixture names it for.

    The layer matters: strict JSON Schema is what refuses an unknown member, because the
    tolerant DTO decoder drops one by ADR-038 design, and saying so in the fixture keeps the
    file honest about which guarantee a second implementation is inheriting.
    """
    schema_refuses = not _is_schema_valid(
        "ContextPackAuthorizedCandidateSetManifest", vector["manifest"]
    )
    try:
        _candidate_checksum(_manifest_from_wire(vector["manifest"]))
        semantics_refuse = False
    except ContractSemanticError:
        semantics_refuse = True
    assert schema_refuses == ("schema" in vector["rejected_by"])
    assert semantics_refuse == ("semantics" in vector["rejected_by"])
    assert vector["rejected_by"], "a rejected vector must name at least one refusing layer"


# Each semantic rejection has its own diagnostic, and the mapping is asserted rather than
# left implicit. Several of these rules overlap -- an exactly repeated evidence tuple is also
# an evidence-checksum collision, and an unknown partition on a governed-shaped candidate is
# also not one of the three governed partitions -- so without pinning *which* rule fires, a
# removed rule would be silently covered by its neighbour and the failure a reader eventually
# saw would name the wrong thing.
CANDIDATE_REJECTION_DIAGNOSTICS: tuple[tuple[str, str], ...] = (
    ("duplicate-tuple", "repeats the candidate"),
    ("one-evidence-id-two-checksums", "cannot have been authorized in two content states"),
    ("governed-version-repeated-within-a-partition", "repeats the candidate"),
    ("governed-version-in-two-partitions", "never two of them"),
    ("governed-version-as-record-and-context-model", "never two of them"),
    ("unknown-partition", "is not one of"),
    ("evidence-identity-in-a-governed-partition", "only ever authorized in"),
    ("governed-identity-in-the-evidence-partition", "is authorized in one of"),
    ("manifest-with-the-wrong-format", "manifest.format"),
    ("candidate-with-an-empty-identity-component", "is not a valid EvidenceId"),
    ("evidence-id-outside-the-identity-domain", "is not a valid EvidenceId"),
    ("record-id-with-spaces", "is not a valid RecordId"),
    ("malformed-evidence-checksum", "is not a valid EvidenceChecksum"),
    ("record-version-with-a-non-printable-character", "is not a valid RecordVersion"),
)


def test_every_rejected_vector_has_a_named_diagnostic() -> None:
    """The table above is complete for the vectors the semantic layer refuses, so a vector
    added to the fixture without a stated diagnostic fails here rather than being carried
    along as a rejection nobody can explain."""
    semantic = {
        vector["id"]
        for vector in CANDIDATE_SET_FIXTURE["rejected"]
        if "semantics" in vector["rejected_by"]
    }
    assert semantic == {vector_id for vector_id, _ in CANDIDATE_REJECTION_DIAGNOSTICS}


@pytest.mark.parametrize(
    "vector_id,fragment",
    CANDIDATE_REJECTION_DIAGNOSTICS,
    ids=[vector_id for vector_id, _ in CANDIDATE_REJECTION_DIAGNOSTICS],
)
def test_authorized_candidate_set_rejection_names_its_own_rule(
    vector_id: str, fragment: str
) -> None:
    manifest = _fixture_manifest("rejected", vector_id)
    with pytest.raises(ContractSemanticError, match=re.escape(fragment)):
        _candidate_checksum(_manifest_from_wire(manifest))


def test_authorized_candidate_set_extra_member_never_reaches_the_preimage() -> None:
    """The complement of the fixture's `candidate-with-an-extra-member` entry: the tolerant
    decoder drops the extra, so the digest is the one the three declared members produce, and
    the frozen dataclass has no field for it to be set on at all."""
    fixture = {
        vector["id"]: vector for vector in CANDIDATE_SET_FIXTURE["rejected"]
    }["candidate-with-an-extra-member"]
    plain = {
        vector["id"]: vector for vector in CANDIDATE_SET_FIXTURE["accepted"]
    }["one-evidence-id-one-checksum"]
    assert _candidate_checksum(_manifest_from_wire(fixture["manifest"])) == plain["checksum"]
    with pytest.raises(TypeError):
        ContextPackAuthorizedEvidenceCandidate(  # type: ignore[call-arg]
            partition="evidence", evidence_id="ev-1", content_checksum=EV_CHECKSUM, score="0.91"
        )


def _fixture_manifest(section: str, vector_id: str) -> dict[str, Any]:
    for vector in CANDIDATE_SET_FIXTURE[section]:
        if vector["id"] == vector_id:
            return dict(vector["manifest"])
    raise AssertionError(f"no {section} vector named {vector_id!r}")


def _candidate_components(candidate: dict[str, Any]) -> tuple[str, str, str]:
    if "evidence_id" in candidate:
        return (candidate["partition"], candidate["evidence_id"], candidate["content_checksum"])
    return (candidate["partition"], candidate["record_id"], candidate["version"])


def test_authorized_candidate_set_orders_by_raw_code_units_not_by_a_collation() -> None:
    """The mixed vector's two evidence identifiers differ only in the case of their last
    character, so the resulting order separates a raw code-unit comparison from a
    case-insensitive or locale-aware collation: `ev-B` sorts first because 0x42 precedes
    0x61, which is the opposite of what either collation would produce."""
    manifest = _fixture_manifest("accepted", "mixed-four-partitions")
    evidence_ids = [
        candidate["evidence_id"]
        for candidate in manifest["candidates"]
        if "evidence_id" in candidate
    ]
    assert set(evidence_ids) == {"ev-a", "ev-B"}
    assert "ev-B".encode("utf-16-be") < "ev-a".encode("utf-16-be")
    assert sorted(["ev-a", "ev-B"], key=str.lower) == ["ev-a", "ev-B"]  # the collation's answer
    ordered = sorted(
        manifest["candidates"],
        key=lambda candidate: tuple(
            value.encode("utf-16-be") for value in _candidate_components(candidate)
        ),
    )
    assert [candidate["partition"] for candidate in ordered] == [
        "context_models",
        "evidence",
        "evidence",
        "history",
        "records",
    ]
    assert [candidate["evidence_id"] for candidate in ordered if "evidence_id" in candidate] == [
        "ev-B",
        "ev-a",
    ]


def test_authorized_candidate_set_digest_would_differ_under_a_case_insensitive_order() -> None:
    """A second implementation that case-folded before comparing would produce a different
    digest for this manifest, which is what makes the frozen value a real check on the
    ordering rule rather than a value that any sort happens to reproduce."""
    manifest = _fixture_manifest("accepted", "mixed-four-partitions")

    def preimage(key: Any) -> bytes:
        return sem_knowledge.canonical_bytes(
            {
                "format": manifest["format"],
                "workspace_id": manifest["workspace_id"],
                "candidates": sorted(manifest["candidates"], key=key),
            }
        )

    code_unit = preimage(
        lambda c: tuple(v.encode("utf-16-be") for v in _candidate_components(c))
    )
    case_folded = preimage(lambda c: tuple(v.lower() for v in _candidate_components(c)))
    assert code_unit != case_folded
    stated = _candidate_checksum(_manifest_from_wire(_fixture_manifest("accepted", "mixed-four-partitions")))
    assert "sha256:" + hashlib.sha256(code_unit).hexdigest() == stated
    assert "sha256:" + hashlib.sha256(case_folded).hexdigest() != stated


def test_authorized_candidate_set_identity_alphabets_cannot_separate_utf16_from_code_point() -> None:
    """The stated reason the UTF-16 rule is not exercised by any candidate in this contract.

    The rule is normative because the sort must agree with the canonicalization RFC 8785
    already defines over member names -- not because a v1 identity could distinguish the two
    orderings. Every v1 identity alphabet is ASCII or printable ASCII, where a code unit *is*
    a code point, so the claim is asserted here rather than left as prose that a later
    alphabet change could silently falsify. The genuine divergence is proved one layer down,
    against `utf16_sort_key` itself, in `test_canonical_json.py`.
    """
    alphabets = {
        "EvidenceId": string.ascii_letters + string.digits + "._:-",
        "RecordId": string.ascii_letters + string.digits + "._:-",
        "RecordVersion": "".join(chr(code) for code in range(0x21, 0x7F)),
        "EvidenceChecksum": string.ascii_letters + string.digits + "+/=_-:",
    }
    for name, alphabet in alphabets.items():
        assert all(ord(character) < 0x80 for character in alphabet), name
        by_code_unit = sorted(alphabet, key=lambda c: c.encode("utf-16-be"))
        by_code_point = sorted(alphabet)
        assert by_code_unit == by_code_point, name
    # And every identity the accepted vectors actually carry is inside that ASCII range.
    for vector in CANDIDATE_SET_FIXTURE["accepted"]:
        for candidate in vector["manifest"]["candidates"]:
            for component in _candidate_components(candidate):
                assert component.isascii(), (vector["id"], component)


def test_authorized_candidate_set_is_order_insensitive() -> None:
    """Candidates are a set: any wire order digests identically."""
    candidates = list(_canonical_candidates())
    baseline = _candidate_checksum(_candidate_manifest())
    for rotation in range(len(candidates)):
        rotated = tuple(candidates[rotation:] + candidates[:rotation])
        assert _candidate_checksum(_candidate_manifest(candidates=rotated)) == baseline
    assert _candidate_checksum(_candidate_manifest(candidates=tuple(reversed(candidates)))) == baseline


CANDIDATE_IDENTITY_MUTATIONS: tuple[tuple[str, tuple[Any, ...]], ...] = (
    (
        "evidence-id",
        (
            _evidence_candidate(evidence_id="ev-2"),
            _record_candidate("records", PACK_RECORD_ID),
            _record_candidate("history", PACK_HISTORY_ID),
            _record_candidate("context_models", PACK_CONTEXT_MODEL_ID),
        ),
    ),
    (
        "evidence-content-checksum",
        (
            _evidence_candidate(content_checksum="sha256:" + "11" * 32),
            _record_candidate("records", PACK_RECORD_ID),
            _record_candidate("history", PACK_HISTORY_ID),
            _record_candidate("context_models", PACK_CONTEXT_MODEL_ID),
        ),
    ),
    (
        "record-id",
        (
            _evidence_candidate(),
            _record_candidate("records", "rec-2"),
            _record_candidate("history", PACK_HISTORY_ID),
            _record_candidate("context_models", PACK_CONTEXT_MODEL_ID),
        ),
    ),
    (
        "record-version",
        (
            _evidence_candidate(),
            _record_candidate("records", PACK_RECORD_ID, "v2"),
            _record_candidate("history", PACK_HISTORY_ID),
            _record_candidate("context_models", PACK_CONTEXT_MODEL_ID),
        ),
    ),
    (
        "governed-partition",
        (
            _evidence_candidate(),
            _record_candidate("context_models", PACK_RECORD_ID),
            _record_candidate("history", PACK_HISTORY_ID),
            _record_candidate("context_models", PACK_CONTEXT_MODEL_ID),
        ),
    ),
    (
        "candidate-added",
        (*_canonical_candidates(), _record_candidate("records", "rec-extra")),
    ),
    ("candidate-removed", _canonical_candidates()[:-1]),
    ("emptied", ()),
)


@pytest.mark.parametrize("label,candidates", CANDIDATE_IDENTITY_MUTATIONS)
def test_authorized_candidate_set_digest_covers_every_identity_component(
    label: str, candidates: tuple[Any, ...]
) -> None:
    """Every component of every identity, and membership itself, is inside the digest."""
    assert _candidate_checksum(_candidate_manifest(candidates=candidates)) != CANDIDATE_SET_DIGEST


def test_authorized_candidate_set_digest_covers_the_workspace() -> None:
    """The only domain separation the preimage carries, and it really separates."""
    assert _candidate_checksum(_candidate_manifest(workspace_id="ws-2")) != CANDIDATE_SET_DIGEST
    assert _candidate_checksum(
        _candidate_manifest(workspace_id="ws-2", candidates=())
    ) != EMPTY_CANDIDATE_SET_DIGEST


def test_authorized_candidate_set_rejects_a_foreign_format() -> None:
    with pytest.raises(ContractSemanticError, match="manifest.format"):
        _candidate_checksum(_candidate_manifest(format_="something.else.v1"))


MALFORMED_CANDIDATE_MANIFESTS: tuple[tuple[str, Any], ...] = (
    ("manifest", None),
    ("manifest", {"format": CANDIDATE_MANIFEST_FORMAT}),
    ("manifest.workspace_id", _candidate_manifest(workspace_id="not a workspace")),
    ("manifest.workspace_id", _candidate_manifest(workspace_id=7)),
    ("manifest.candidates", ContextPackAuthorizedCandidateSetManifest(
        format=CANDIDATE_MANIFEST_FORMAT, workspace_id="ws-1", candidates=None  # type: ignore[arg-type]
    )),
    ("manifest.candidates[0]", _candidate_manifest(candidates=("not-a-candidate",))),
    ("manifest.candidates[0].evidence_id", _candidate_manifest(
        candidates=(_evidence_candidate(evidence_id=""),)
    )),
    ("manifest.candidates[0].evidence_id", _candidate_manifest(
        candidates=(ContextPackAuthorizedEvidenceCandidate(
            partition="evidence", evidence_id=7, content_checksum=EV_CHECKSUM  # type: ignore[arg-type]
        ),)
    )),
    ("manifest.candidates[0].version", _candidate_manifest(
        candidates=(_record_candidate("records", "rec-1", "v" * 513),)
    )),
    ("manifest.candidates[0].partition", _candidate_manifest(
        candidates=(_record_candidate("future_partition", "rec-1"),)
    )),
)


@pytest.mark.parametrize("label,manifest", MALFORMED_CANDIDATE_MANIFESTS)
def test_authorized_candidate_set_rejects_a_malformed_manifest(label: str, manifest: Any) -> None:
    """A direct entry point: a hand-built manifest raises `ContractSemanticError`, never a
    raw `TypeError`/`AttributeError`."""
    with pytest.raises(ContractSemanticError, match=re.escape(label)):
        _candidate_checksum(manifest)


def test_authorized_candidate_set_rejects_a_lone_surrogate_identity() -> None:
    """A lone surrogate is outside `EvidenceId` before it is ever a canonicalization problem,
    so the identity domain refuses it and the digest is never reached."""
    manifest = _candidate_manifest(
        candidates=(_evidence_candidate(evidence_id="ev-\ud800"),)
    )
    with pytest.raises(ContractSemanticError, match="not a valid EvidenceId"):
        _candidate_checksum(manifest)


# --- candidate identities are exactly the domains of the items they name -------
#
# The preimage names real artifacts and real governed versions, so each component is held to
# the same domain the item itself is. A wider domain would let a digest attest to a frontier
# membership that no artifact and no record could ever satisfy, which is precisely the claim
# a verifier reads this digest to check. One case per component, each naming its own domain
# in the diagnostic, alongside the language-neutral vectors in the fixture.

OUT_OF_DOMAIN_CANDIDATE_IDENTITIES: tuple[tuple[str, Any, str], ...] = (
    (
        "evidence_id-supplementary-plane",
        _evidence_candidate(evidence_id="ev-\U0001f600"),
        "not a valid EvidenceId",
    ),
    (
        "evidence_id-with-a-space",
        _evidence_candidate(evidence_id="ev 1"),
        "not a valid EvidenceId",
    ),
    (
        "evidence_id-leading-punctuation",
        _evidence_candidate(evidence_id="-ev-1"),
        "not a valid EvidenceId",
    ),
    (
        "content_checksum-without-an-algorithm",
        _evidence_candidate(content_checksum="9f" * 32),
        "not a valid EvidenceChecksum",
    ),
    (
        "content_checksum-uppercase-algorithm",
        _evidence_candidate(content_checksum="SHA256:" + "9f" * 32),
        "not a valid EvidenceChecksum",
    ),
    (
        "record_id-with-spaces",
        _record_candidate("records", "rec 1"),
        "not a valid RecordId",
    ),
    (
        "record_id-supplementary-plane",
        _record_candidate("records", "rec-\U0001f600"),
        "not a valid RecordId",
    ),
    (
        "version-non-printable",
        _record_candidate("records", "rec-1", "v1\u0001"),
        "not a valid RecordVersion",
    ),
    (
        "version-with-a-space",
        _record_candidate("records", "rec-1", "v 1"),
        "not a valid RecordVersion",
    ),
    (
        "version-supplementary-plane",
        _record_candidate("records", "rec-1", "v-\U0001f600"),
        "not a valid RecordVersion",
    ),
)


@pytest.mark.parametrize(
    "label,candidate,expected",
    OUT_OF_DOMAIN_CANDIDATE_IDENTITIES,
    ids=[label for label, _, _ in OUT_OF_DOMAIN_CANDIDATE_IDENTITIES],
)
def test_authorized_candidate_set_rejects_an_out_of_domain_identity(
    label: str, candidate: Any, expected: str
) -> None:
    """Refused by the checksum helper, naming the exact domain that refused it."""
    with pytest.raises(ContractSemanticError, match=re.escape(expected)):
        _candidate_checksum(_candidate_manifest(candidates=(candidate,)))


@pytest.mark.parametrize(
    "label,candidate,expected",
    OUT_OF_DOMAIN_CANDIDATE_IDENTITIES,
    ids=[label for label, _, _ in OUT_OF_DOMAIN_CANDIDATE_IDENTITIES],
)
def test_authorized_candidate_set_out_of_domain_identity_is_not_schema_valid(
    label: str, candidate: Any, expected: str
) -> None:
    """And refused by strict JSON Schema too: the `$ref`s are to the same identity
    definitions, so neither layer is the only thing standing between the digest and an
    identity the contract does not admit."""
    manifest = _candidate_manifest(candidates=(candidate,)).to_wire()
    assert not _is_schema_valid("ContextPackAuthorizedCandidateSetManifest", manifest)


CANDIDATE_IDENTITY_REFS: tuple[tuple[str, str, str], ...] = (
    (
        "ContextPackAuthorizedEvidenceCandidate",
        "evidence_id",
        "https://contracts.omnivia.dev/application/v1/evidence.schema.json#/$defs/EvidenceId",
    ),
    (
        "ContextPackAuthorizedEvidenceCandidate",
        "content_checksum",
        "https://contracts.omnivia.dev/application/v1/evidence.schema.json#/$defs/EvidenceChecksum",
    ),
    (
        "ContextPackAuthorizedRecordCandidate",
        "record_id",
        "https://contracts.omnivia.dev/application/v1/records.schema.json#/$defs/RecordId",
    ),
    (
        "ContextPackAuthorizedRecordCandidate",
        "version",
        "https://contracts.omnivia.dev/application/v1/records.schema.json#/$defs/RecordVersion",
    ),
)


@pytest.mark.parametrize(
    "definition,member,expected_ref",
    CANDIDATE_IDENTITY_REFS,
    ids=[f"{definition}.{member}" for definition, member, _ in CANDIDATE_IDENTITY_REFS],
)
def test_authorized_candidate_member_refs_the_exact_identity_definition(
    definition: str, member: str, expected_ref: str
) -> None:
    """The domains are *shared* with the items, not merely similar to them.

    Asserted against the `$ref` rather than against a restated `pattern`/`maxLength` pair,
    because a restatement is exactly the thing that drifts: a later change to `EvidenceId`
    would silently stop applying to the frontier that names evidence, and the digest would go
    on admitting identities no artifact could carry.
    """
    schema = json.loads((SCHEMA_DIR / "context-pack.schema.json").read_text(encoding="utf-8"))
    property_schema = schema["$defs"][definition]["properties"][member]
    assert property_schema["$ref"] == expected_ref
    assert "pattern" not in property_schema, "the domain is referenced, never restated"


# --- the candidate bound -------------------------------------------------------


CANDIDATE_SET_MAX = 20_000


def _bounded_candidates(count: int) -> tuple[Any, ...]:
    """`count` distinct governed candidates, built once per call and reused across the two
    boundary tests so the bound is exercised without hashing 20,000 identities repeatedly."""
    return tuple(
        _record_candidate("records", f"rec-{index:05d}") for index in range(count)
    )


def test_authorized_candidate_set_accepts_exactly_the_bound() -> None:
    """The bound is inclusive: a frontier of exactly 20,000 candidates digests normally.
    Asserted rather than inferred from the rejection below, since a bound that was actually
    exclusive would reject this and no other test would notice."""
    manifest = _candidate_manifest(candidates=_bounded_candidates(CANDIDATE_SET_MAX))
    assert _candidate_checksum(manifest).startswith("sha256:")


def test_authorized_candidate_set_rejects_more_candidates_than_the_bound() -> None:
    oversized = _bounded_candidates(CANDIDATE_SET_MAX + 1)
    with pytest.raises(ContractSemanticError, match="exceeding the maximum"):
        _candidate_checksum(_candidate_manifest(candidates=oversized))


# --- selected items must be members of the frozen frontier ---------------------


SUBSET_PARTITIONS: tuple[tuple[str, str], ...] = (
    ("evidence", PACK_EVIDENCE_ID),
    ("records", PACK_RECORD_ID),
    ("history", PACK_HISTORY_ID),
    ("context_models", PACK_CONTEXT_MODEL_ID),
)


@pytest.mark.parametrize("partition,identity", SUBSET_PARTITIONS, ids=[p for p, _ in SUBSET_PARTITIONS])
def test_context_pack_requires_every_selected_item_on_the_frontier(
    partition: str, identity: str
) -> None:
    """Drop exactly one identity from the frontier and the pack that still selects it is
    refused, on both entry points. Nothing may enter a pack after the frontier is frozen."""
    narrowed = tuple(
        candidate
        for candidate in _canonical_candidates()
        if candidate.partition != partition
    )
    manifest = _candidate_manifest(candidates=narrowed)
    document = _signed(_context_pack_build_result_wire())
    expected = "the authorized candidate set does not contain"
    with pytest.raises(ContractSemanticError, match=expected):
        _validate_pack(
            ContextPackBuildResult.from_wire(document),
            expected_authorized_candidate_set=manifest,
        )
    with pytest.raises(ContractSemanticError, match=expected):
        _validate_pack_document(document, expected_authorized_candidate_set=manifest)


def test_context_pack_requires_the_selected_item_under_its_exact_partition() -> None:
    """Membership is keyed by partition: the same governed version authorized as history is
    not authorization to return it as current."""
    moved = (
        _evidence_candidate(),
        _record_candidate("history", PACK_RECORD_ID),
        _record_candidate("history", PACK_HISTORY_ID),
        _record_candidate("context_models", PACK_CONTEXT_MODEL_ID),
    )
    with pytest.raises(
        ContractSemanticError, match="does not contain in partition 'records'"
    ):
        _validate_pack(_pack(), expected_authorized_candidate_set=_candidate_manifest(candidates=moved))


def test_context_pack_requires_the_selected_evidence_content_state_on_the_frontier() -> None:
    """The content checksum is part of the evidence identity, so an artifact authorized at
    one content state does not authorize returning it at another."""
    restated = (
        _evidence_candidate(content_checksum="sha256:" + "11" * 32),
        _record_candidate("records", PACK_RECORD_ID),
        _record_candidate("history", PACK_HISTORY_ID),
        _record_candidate("context_models", PACK_CONTEXT_MODEL_ID),
    )
    with pytest.raises(ContractSemanticError, match="the authorized candidate set does not contain"):
        _validate_pack(
            _pack(), expected_authorized_candidate_set=_candidate_manifest(candidates=restated)
        )


def test_context_pack_allows_a_frontier_wider_than_what_was_selected() -> None:
    """A frontier is what the build was *entitled* to rank over, so it is ordinarily wider
    than what fit the budget. Only the stated checksum has to match it."""
    wider = (*_canonical_candidates(), _record_candidate("records", "rec-unselected"))
    manifest = _candidate_manifest(candidates=wider)
    document = _context_pack_build_result_wire(
        reproducibility=_reproducibility_wire(
            authorization_context=_authorization_context_wire(
                authorized_candidate_set_checksum=_candidate_checksum(manifest)
            )
        )
    )
    signed = _signed(document)
    _validate_pack(
        ContextPackBuildResult.from_wire(signed), expected_authorized_candidate_set=manifest
    )
    _validate_pack_document(signed, expected_authorized_candidate_set=manifest)


def test_context_pack_frontier_must_be_for_the_validated_workspace() -> None:
    with pytest.raises(
        ContractSemanticError, match="expected_authorized_candidate_set.workspace_id"
    ):
        _validate_pack(_pack(), expected_authorized_candidate_set=_candidate_manifest(workspace_id="ws-2"))


def test_context_pack_rejects_an_arbitrary_re_signed_candidate_checksum() -> None:
    """The whole point of the manifest, stated as an attack.

    A producer that writes any digest it likes into the authorization context and re-signs
    the artifact produces a pack that is internally perfect: `pack_id` and
    `artifact_checksum` are the true digest of its own bytes, the raw-document integrity
    check passes, and every field round-trips. It still fails, because the checksum is
    compared against one recomputed from the caller's own frontier rather than against
    itself.
    """
    for forged in ("sha256:" + "2b" * 32, EMPTY_CANDIDATE_SET_DIGEST, CANDIDATE_SET_DIGEST[:-1] + "0"):
        document = _signed(
            _context_pack_build_result_wire(
                reproducibility=_reproducibility_wire(
                    authorization_context=_authorization_context_wire(
                        authorized_candidate_set_checksum=forged
                    )
                )
            )
        )
        # The artifact is entirely self-consistent: integrity verification accepts it.
        sem_knowledge.verify_context_pack_artifact_document(json.dumps(document))
        with pytest.raises(
            ContractSemanticError, match="authorized_candidate_set_checksum"
        ):
            _validate_pack(ContextPackBuildResult.from_wire(document))
        with pytest.raises(
            ContractSemanticError, match="authorized_candidate_set_checksum"
        ):
            _validate_pack_document(document)


def test_context_pack_integrity_entry_point_documents_that_it_skips_the_frontier() -> None:
    """`verify_context_pack_artifact_document` is honestly named and honestly documented: it
    proves the bytes match their digest and says so, rather than implying it checked the
    authorized candidate set it structurally cannot check."""
    doc = sem_knowledge.verify_context_pack_artifact_document.__doc__ or ""
    assert "never verifies" in doc
    assert "authorized_candidate_set_checksum" in doc
    assert "validate_context_pack_build_result_document" in doc


def test_authorized_candidate_set_manifest_is_not_a_result_member() -> None:
    """Identity-only trusted input, not a response field: no v1 payload carries it, so it is
    never returned to a caller and never lands in a log by way of one."""
    schema = json.loads((SCHEMA_DIR / "context-pack.schema.json").read_text(encoding="utf-8"))
    manifest_ref = f"{BASE_URI}context-pack.schema.json#/$defs/ContextPackAuthorizedCandidateSetManifest"
    for name, definition in schema["$defs"].items():
        if name == "ContextPackAuthorizedCandidateSetManifest":
            continue
        assert manifest_ref not in json.dumps(definition)
    assert "authorized_candidate_set" not in json.dumps(
        schema["$defs"]["ContextPackBuildResult"]
    )


def test_authorized_candidate_set_types_are_published_in_typescript() -> None:
    """Python/TypeScript parity: both generated surfaces declare the same four definitions
    with the same members, so a TypeScript verifier reads the same fixture into the same
    shape and can be held to the same frozen digests."""
    typescript = (
        REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts"
    ).read_text(encoding="utf-8")
    for interface, members in (
        ("ContextPackAuthorizedEvidenceCandidate", ("partition", "evidence_id", "content_checksum")),
        ("ContextPackAuthorizedRecordCandidate", ("partition", "record_id", "version")),
        ("ContextPackAuthorizedCandidateSetManifest", ("format", "workspace_id", "candidates")),
    ):
        body = typescript.split(f"export interface {interface} {{", 1)[1].split("\n}", 1)[0]
        declared = tuple(re.findall(r"^\s*readonly (\w+)", body, re.MULTILINE))
        assert declared == members, (interface, declared)
        python_fields = tuple(
            field.name
            for field in dataclasses.fields(getattr(generated, interface))
        )
        assert python_fields == members, (interface, python_fields)
    assert (
        "export type ContextPackAuthorizedCandidate = "
        "ContextPackAuthorizedEvidenceCandidate | ContextPackAuthorizedRecordCandidate;"
    ) in typescript


# --- 6g. selected evidence ----------------------------------------------------


def test_context_pack_rejects_tombstoned_evidence() -> None:
    result = _pack(evidence=[_pack_evidence_wire(tombstoned=True)])
    with pytest.raises(ContractSemanticError, match="tombstoned"):
        _validate_pack(result)


def test_context_pack_rejects_evidence_recorded_after_the_resolution_time() -> None:
    result = _pack(
        evidence=[_pack_evidence_wire(temporal=_temporal_wire(ingested_at=T2, recorded_at=T2))]
    )
    with pytest.raises(ContractSemanticError, match="after the canonical-resolution time"):
        _validate_pack(result)


def test_context_pack_rejects_evidence_not_yet_valid() -> None:
    result = _pack(evidence=[_pack_evidence_wire(temporal=_temporal_wire(valid_from=T2))])
    with pytest.raises(ContractSemanticError, match="not yet valid"):
        _validate_pack(result)


def test_context_pack_rejects_evidence_no_longer_valid() -> None:
    result = _pack(evidence=[_pack_evidence_wire(temporal=_temporal_wire(valid_until=T0))])
    with pytest.raises(ContractSemanticError, match="no longer valid"):
        _validate_pack(result)


def test_context_pack_rejects_evidence_superseded_by_the_resolution_time() -> None:
    result = _pack(evidence=[_pack_evidence_wire(temporal=_temporal_wire(superseded_at=T1))])
    with pytest.raises(ContractSemanticError, match="superseded at"):
        _validate_pack(result)


def test_context_pack_accepts_evidence_superseded_only_afterwards() -> None:
    document = _context_pack_build_result_wire(
        evidence=[_pack_evidence_wire(temporal=_temporal_wire(superseded_at=T2))]
    )
    _validate_pack(ContextPackBuildResult.from_wire(_signed(document)))


def test_context_pack_rejects_evidence_from_another_workspace() -> None:
    result = _pack(evidence=[_pack_evidence_wire(workspace_id="ws-other")])
    with pytest.raises(ContractSemanticError, match="does not match the selected workspace"):
        _validate_pack(result)


@pytest.mark.parametrize(
    "field_name,count,build",
    [
        (
            "permission_labels",
            EVIDENCE_PERMISSION_LABELS_MAX + 1,
            lambda count: {"permission_labels": _labels(count)},
        ),
        (
            "provenance_history",
            EVIDENCE_PROVENANCE_HISTORY_MAX + 1,
            lambda count: {"provenance_history": [_history_entry_wire()] * count},
        ),
        (
            "evidence",
            PROVENANCE_ENTRY_EVIDENCE_MAX + 1,
            lambda count: {
                "provenance_history": [
                    _history_entry_wire(evidence=_self_evidence_references(count))
                ]
            },
        ),
    ],
)
def test_context_pack_rejects_selected_evidence_past_a_schema_cardinality(
    field_name: str, count: int, build: Any
) -> None:
    """The evidence parity bounds, reached the way a trust boundary actually reaches them.

    A Context Pack selecting an over-long artifact must be refused on both public entry
    points, not only when the artifact is validated on its own: the tolerant decoder applies
    no cardinality, so without the semantic restatement the whole pack -- digest and all --
    would verify against a bar the schema does not agree with.
    """
    document = _signed(
        _context_pack_build_result_wire(evidence=[_pack_evidence_wire(**build(count))])
    )
    assert not _is_schema_valid("ContextPackBuildResult", document)
    with pytest.raises(ContractSemanticError, match=f"{field_name} has {count} entries"):
        _validate_pack(ContextPackBuildResult.from_wire(document))
    with pytest.raises(ContractSemanticError, match=f"{field_name} has {count} entries"):
        _validate_pack_document(document)


# --- 6g-bis. resolution-time closure over provenance --------------------------
#
# The partition rules above each read one instant off an item's `temporal` envelope. An
# item's own audit trail is a second, independent set of instants, and a pack that selects
# an item whose history reaches past the moment it resolved at is presenting an act that had
# not happened yet -- the deterministic-view guarantee read backwards. Every partition is
# held to it, and the boundary is inclusive: an act *at* the resolution instant had happened
# by it.


def _pack_with_evidence_history_at(occurred_at: str) -> dict[str, Any]:
    return _context_pack_build_result_wire(
        evidence=[
            _pack_evidence_wire(
                provenance_history=[_history_entry_wire(occurred_at=occurred_at)]
            )
        ]
    )


def _pack_with_record_history_at(partition: str, occurred_at: str) -> dict[str, Any]:
    history = [_history_entry_wire(occurred_at=occurred_at)]
    builder = {
        "records": lambda: {"records": [_pack_record_wire(history=history)]},
        "history": lambda: {"history": [_pack_history_wire(history=history)]},
        "context_models": lambda: {
            "context_models": [_pack_context_model_wire(history=history)]
        },
    }[partition]
    return _context_pack_build_result_wire(**builder())


PROVENANCE_INSTANT_CASES = (
    ("before", T0),
    ("equal", T1),
)


@pytest.mark.parametrize("position,occurred_at", PROVENANCE_INSTANT_CASES)
def test_context_pack_accepts_evidence_provenance_at_or_before_the_resolution_time(
    position: str, occurred_at: str
) -> None:
    _validate_pack(
        ContextPackBuildResult.from_wire(_signed(_pack_with_evidence_history_at(occurred_at)))
    )


def test_context_pack_rejects_evidence_provenance_after_the_resolution_time() -> None:
    document = _signed(_pack_with_evidence_history_at(T2))
    with pytest.raises(
        ContractSemanticError,
        match=r"evidence\[0\]\.provenance_history\[0\]\.occurred_at is",
    ):
        _validate_pack(ContextPackBuildResult.from_wire(document))
    with pytest.raises(ContractSemanticError, match="had not happened at the instant"):
        _validate_pack_document(document)


@pytest.mark.parametrize("partition", ["records", "history", "context_models"])
@pytest.mark.parametrize("position,occurred_at", PROVENANCE_INSTANT_CASES)
def test_context_pack_accepts_record_provenance_at_or_before_the_resolution_time(
    partition: str, position: str, occurred_at: str
) -> None:
    _validate_pack(
        ContextPackBuildResult.from_wire(
            _signed(_pack_with_record_history_at(partition, occurred_at))
        )
    )


@pytest.mark.parametrize("partition", ["records", "history", "context_models"])
def test_context_pack_rejects_record_provenance_after_the_resolution_time(
    partition: str,
) -> None:
    document = _signed(_pack_with_record_history_at(partition, T2))
    with pytest.raises(
        ContractSemanticError,
        match=rf"{partition}\[0\]\.provenance\.history\[0\]\.occurred_at is",
    ):
        _validate_pack(ContextPackBuildResult.from_wire(document))
    with pytest.raises(ContractSemanticError, match="had not happened at the instant"):
        _validate_pack_document(document)


def test_context_pack_future_provenance_is_refused_not_repaired() -> None:
    """Provenance is append-only, so an out-of-range entry is never dropped or truncated to
    make the item selectable: the item simply was not this pack's to select."""
    accepted = _pack_with_evidence_history_at(T0)
    rejected = _pack_with_evidence_history_at(T2)
    _validate_pack(ContextPackBuildResult.from_wire(_signed(accepted)))
    with pytest.raises(ContractSemanticError):
        _validate_pack(ContextPackBuildResult.from_wire(_signed(rejected)))
    # Both documents are schema-valid; only the resolution-time rule separates them.
    assert _is_schema_valid("ContextPackBuildResult", _signed(accepted))
    assert _is_schema_valid("ContextPackBuildResult", _signed(rejected))


# --- 6g-quater. the closure is complete: every nested instant is bounded --------
#
# `canonical_resolution_time` is an inclusive upper bound on every *act and observation* a
# selected item carries, not only on the one or two instants a partition rule happens to
# read. The tables below enumerate every bounded path on both sides -- evidence and governed
# record -- and each is exercised twice: once with the instant exactly at the resolution time,
# which must pass, and once one step later, which must be refused naming that exact path. An
# enumerated table rather than a few representative cases, because "complete" is the claim.


def _evidence_pack_at(path: str, instant: str) -> dict[str, Any]:
    """The canonical pack with exactly one evidence instant moved to `instant`.

    Some paths need a second field moved with them to stay past the *intrinsic* artifact
    rules (`observed_at` may not follow `ingested_at`, `ingested_at` may not follow
    `recorded_at`), since a case blocked by those would prove nothing about this one. The
    closure checks the four `temporal` instants in declaration order, so the named path is
    still the one that fires.
    """
    builders: dict[str, dict[str, Any]] = {
        "temporal.event_at": {
            "temporal": {**_temporal_wire(), "event_at": instant},
        },
        "temporal.observed_at": {
            "temporal": _temporal_wire(
                observed_at=instant, ingested_at=instant, recorded_at=instant
            ),
        },
        "temporal.ingested_at": {
            "temporal": _temporal_wire(ingested_at=instant, recorded_at=instant),
        },
        "temporal.recorded_at": {
            "temporal": _temporal_wire(ingested_at=T0, recorded_at=instant),
        },
        "source.retrieved_at": {"source": _source_wire(retrieved_at=instant)},
        "provenance_history[0].occurred_at": {
            "provenance_history": [_history_entry_wire(occurred_at=instant)],
        },
        "provenance_history[0].evidence[0].source.retrieved_at": {
            "provenance_history": [
                _history_entry_wire(
                    occurred_at=T0,
                    evidence=[
                        _evidence_reference_wire(source=_source_wire(retrieved_at=instant))
                    ],
                )
            ],
        },
    }
    return _context_pack_build_result_wire(evidence=[_pack_evidence_wire(**builders[path])])


EVIDENCE_INSTANT_PATHS: tuple[str, ...] = (
    "temporal.event_at",
    "temporal.observed_at",
    "temporal.ingested_at",
    "temporal.recorded_at",
    "source.retrieved_at",
    "provenance_history[0].occurred_at",
    "provenance_history[0].evidence[0].source.retrieved_at",
)


@pytest.mark.parametrize("path", EVIDENCE_INSTANT_PATHS)
def test_context_pack_accepts_every_evidence_instant_at_the_resolution_time(path: str) -> None:
    """Inclusive: an act or observation *at* the instant a pack resolved at had happened."""
    document = _signed(_evidence_pack_at(path, T1))
    assert _is_schema_valid("ContextPackBuildResult", document)
    _validate_pack(ContextPackBuildResult.from_wire(document))
    _validate_pack_document(document)


@pytest.mark.parametrize("path", EVIDENCE_INSTANT_PATHS)
def test_context_pack_rejects_every_evidence_instant_after_the_resolution_time(
    path: str,
) -> None:
    document = _signed(_evidence_pack_at(path, T2))
    # Schema-valid either way: only the resolution-time closure separates the two.
    assert _is_schema_valid("ContextPackBuildResult", document)
    expected = re.escape(f"evidence[0].{path}") + " is"
    with pytest.raises(ContractSemanticError, match=expected):
        _validate_pack(ContextPackBuildResult.from_wire(document))
    with pytest.raises(ContractSemanticError, match=expected):
        _validate_pack_document(document)


# The governed side of the closure is one rule applied at three partitions, so the builder
# takes the partition rather than hard-coding `records`. Each partition brings its own
# identity and its own required temporal envelope -- `history` in particular must carry a
# `superseded_at` -- and the path table is layered on top of that envelope rather than
# replacing it, so a case cannot pass by accidentally dropping the thing that makes the item
# belong to its partition at all.


def _governed_identity_for(partition: str) -> dict[str, Any]:
    if partition == "records":
        return _identity_wire(record_id=PACK_RECORD_ID, version="v1")
    if partition == "context_models":
        return _identity_wire(record_id=PACK_CONTEXT_MODEL_ID, version="v1", layer="l3")
    if partition == "history":
        return _identity_wire(
            record_id=PACK_HISTORY_ID,
            version="v1",
            currentness="superseded",
            superseded_by={"record_id": PACK_HISTORY_ID, "version": "v-next"},
        )
    raise AssertionError(f"unknown governed partition {partition!r}")


def _governed_base_temporal(partition: str) -> dict[str, Any]:
    """The temporal envelope a partition requires before any path is applied.

    `history` needs a `superseded_at` at or before the resolution instant, since a version
    superseded only afterwards was still canonical then and is not history's to carry.
    """
    if partition == "history":
        return _temporal_wire(superseded_at=T1)
    return _temporal_wire()


def _governed_pack_at(partition: str, path: str, instant: str) -> dict[str, Any]:
    """The canonical pack with exactly one governed-record provenance instant moved.

    Some paths move a second field with them to stay past the *intrinsic* record rules
    (`ingested_at` may not follow `recorded_at`), since a case blocked by those would prove
    nothing about the closure. The closure checks the four `temporal` instants in declaration
    order, so the named path is still the one that fires.
    """
    base = _governed_base_temporal(partition)

    def temporal(**changes: Any) -> dict[str, Any]:
        return {"temporal": {**base, **changes}}

    provenance: dict[str, Any] = {
        "temporal.event_at": temporal(event_at=instant),
        "temporal.observed_at": temporal(observed_at=instant),
        "temporal.ingested_at": temporal(ingested_at=instant, recorded_at=instant),
        "temporal.recorded_at": temporal(ingested_at=T0, recorded_at=instant),
        "sources[0].retrieved_at": {"sources": [_source_wire(retrieved_at=instant)]},
        "history[0].occurred_at": {"history": [_history_entry_wire(occurred_at=instant)]},
        "history[0].evidence[0].source.retrieved_at": {
            "history": [
                _history_entry_wire(
                    occurred_at=T0,
                    evidence=[
                        _evidence_reference_wire(source=_source_wire(retrieved_at=instant))
                    ],
                )
            ]
        },
        "assertion.asserted_at": {"assertion": _assertion_wire(asserted_at=instant)},
        "assertion.evidence[0].source.retrieved_at": {
            "assertion": _assertion_wire(
                asserted_at=T0,
                evidence=[_evidence_reference_wire(source=_source_wire(retrieved_at=instant))],
            )
        },
        "extraction.extracted_at": {"extraction": _extraction_wire(extracted_at=instant)},
    }[path]
    provenance.setdefault("temporal", base)
    record = _governed_record_wire(
        authority_level="canonical",
        reviewer="reviewer-1",
        provenance=_provenance_wire(
            identity=_governed_identity_for(partition), **provenance
        ),
    )
    return _context_pack_build_result_wire(**{partition: [record]})


RECORD_INSTANT_PATHS: tuple[str, ...] = (
    "temporal.event_at",
    "temporal.observed_at",
    "temporal.ingested_at",
    "temporal.recorded_at",
    "sources[0].retrieved_at",
    "history[0].occurred_at",
    "history[0].evidence[0].source.retrieved_at",
    "assertion.asserted_at",
    "assertion.evidence[0].source.retrieved_at",
    "extraction.extracted_at",
)

# `history` cannot isolate the closure for `ingested_at`/`recorded_at`, and the reason is a
# rule rather than an inconvenience: a historical version must satisfy the intrinsic chain
# `recorded_at <= superseded_at`, and its partition rule requires `superseded_at <=
# resolution`. Composed, those already force `recorded_at <= resolution`, so a future
# `recorded_at` is refused *before* the nested closure is ever consulted -- and
# `ingested_at <= recorded_at` carries the same conclusion one step earlier. Excluding the
# two paths here and proving that refusal explicitly below is the honest version of
# "complete": the instants are still bounded, just not by this rule.
HISTORY_INTRINSICALLY_BOUNDED_PATHS: tuple[str, ...] = (
    "temporal.ingested_at",
    "temporal.recorded_at",
)
HISTORY_INSTANT_PATHS: tuple[str, ...] = tuple(
    path for path in RECORD_INSTANT_PATHS if path not in HISTORY_INTRINSICALLY_BOUNDED_PATHS
)

GOVERNED_CLOSURE_CASES: tuple[tuple[str, str], ...] = tuple(
    (partition, path)
    for partition in ("records", "context_models")
    for path in RECORD_INSTANT_PATHS
) + tuple(("history", path) for path in HISTORY_INSTANT_PATHS)


@pytest.mark.parametrize(
    "partition,path",
    GOVERNED_CLOSURE_CASES,
    ids=[f"{partition}-{path}" for partition, path in GOVERNED_CLOSURE_CASES],
)
def test_context_pack_accepts_every_governed_instant_at_the_resolution_time(
    partition: str, path: str
) -> None:
    """Inclusive, at every governed partition: an act *at* the instant a pack resolved at had
    happened by it."""
    document = _signed(_governed_pack_at(partition, path, T1))
    assert _is_schema_valid("ContextPackBuildResult", document)
    _validate_pack(ContextPackBuildResult.from_wire(document))
    _validate_pack_document(document)


@pytest.mark.parametrize(
    "partition,path",
    GOVERNED_CLOSURE_CASES,
    ids=[f"{partition}-{path}" for partition, path in GOVERNED_CLOSURE_CASES],
)
def test_context_pack_rejects_every_governed_instant_after_the_resolution_time(
    partition: str, path: str
) -> None:
    document = _signed(_governed_pack_at(partition, path, T2))
    assert _is_schema_valid("ContextPackBuildResult", document)
    expected = re.escape(f"{partition}[0].provenance.{path}") + " is"
    with pytest.raises(ContractSemanticError, match=expected):
        _validate_pack(ContextPackBuildResult.from_wire(document))
    with pytest.raises(ContractSemanticError, match=expected):
        _validate_pack_document(document)


@pytest.mark.parametrize("path", HISTORY_INTRINSICALLY_BOUNDED_PATHS)
def test_history_future_ingest_and_record_instants_are_refused_intrinsically(
    path: str,
) -> None:
    """The two paths the table above excludes for `history`, proved rather than skipped.

    A future `ingested_at`/`recorded_at` on a historical version is refused by the intrinsic
    chain `recorded_at <= superseded_at <= resolution` before the nested closure is reached.
    So the instant is still bounded -- the diagnostic simply names the intrinsic rule, and
    asserting the closure's own message here would be asserting something untrue.
    """
    document = _signed(_governed_pack_at("history", path, T2))
    closure_message = re.escape(f"history[0].provenance.{path}") + " is"
    with pytest.raises(ContractSemanticError) as raised:
        _validate_pack(ContextPackBuildResult.from_wire(document))
    message = str(raised.value)
    assert not re.search(closure_message, message), (
        "the closure fired first; this test no longer proves the intrinsic rule bounds it"
    )
    assert "superseded_at" in message and "recorded_at" in message
    # And the bound really is upheld: T1 -- the resolution instant itself -- is accepted,
    # so the refusal above is about being *past* it rather than about the path at all.
    accepted = _signed(_governed_pack_at("history", path, T1))
    _validate_pack(ContextPackBuildResult.from_wire(accepted))
    _validate_pack_document(accepted)


@pytest.mark.parametrize("field_name", ["proposed_valid_from", "proposed_valid_until"])
@pytest.mark.parametrize("instant", [T1, T2])
def test_context_pack_does_not_bound_a_proposed_effective_date(
    field_name: str, instant: str
) -> None:
    """A proposed effective date is a claim about the future, not an act that had to have
    happened. Bounding it would make a forward-dated proposal unselectable for a reason that
    has nothing to do with when the pack resolved."""
    identity = _identity_wire(record_id=PACK_RECORD_ID, version="v1")
    record = _governed_record_wire(
        authority_level="canonical",
        reviewer="reviewer-1",
        provenance=_provenance_wire(
            identity=identity, assertion=_assertion_wire(**{field_name: instant})
        ),
    )
    document = _signed(_context_pack_build_result_wire(records=[record]))
    _validate_pack(ContextPackBuildResult.from_wire(document))
    _validate_pack_document(document)


T_FAR_FUTURE = "2030-01-01T00:00:00Z"


def test_context_pack_accepts_a_wholly_future_proposed_validity_window() -> None:
    """Both ends of a proposed window in the future, on a record whose *actual* validity
    contains the resolution instant.

    The single case that separates the two notions completely: the record really was the
    canonical answer at T1, and it separately proposes taking effect from T2 until later
    still. Bounding the proposal would refuse a record that is valid now because of a claim
    about what it should become -- and reading the proposal *as* the validity would refuse it
    for a window it never asserted was current.
    """
    identity = _identity_wire(record_id=PACK_RECORD_ID, version="v1")
    record = _governed_record_wire(
        authority_level="canonical",
        reviewer="reviewer-1",
        provenance=_provenance_wire(
            identity=identity,
            temporal=_temporal_wire(valid_from=T0, valid_until=T2),
            assertion=_assertion_wire(
                proposed_valid_from=T2, proposed_valid_until=T_FAR_FUTURE
            ),
        ),
    )
    document = _signed(_context_pack_build_result_wire(records=[record]))
    assert _is_schema_valid("ContextPackBuildResult", document)
    _validate_pack(ContextPackBuildResult.from_wire(document))
    _validate_pack_document(document)


# The bounds that are *not* upper-bounded by the closure keep their own existing meaning, so
# each is pinned at its own boundary rather than being left to the closure's coverage.


@pytest.mark.parametrize(
    "valid_until,accepted",
    [(T2, True), (T1, True), (T0, False)],
    ids=["valid-past-the-instant", "still-valid-at-the-instant", "expired-before-it"],
)
def test_context_pack_current_record_validity_upper_boundary(
    valid_until: str, accepted: bool
) -> None:
    """`valid_until` is inclusive: a version valid *until* the resolution instant was still
    the answer at it, and one valid past it plainly was too.

    The `T2` case is the one that keeps the closure honest about direction. `valid_until` is
    a validity bound, not an act, so the resolution-time closure deliberately does not bound
    it from above -- a record that stays valid into the future is ordinary, and refusing it
    would confuse "still true later" with "happened later".
    """
    record = _canonical_knowledge_record_wire(
        record_id=PACK_RECORD_ID, version="v1", valid_until=valid_until
    )
    document = _signed(_context_pack_build_result_wire(records=[record]))
    if accepted:
        assert _is_schema_valid("ContextPackBuildResult", document)
        _validate_pack(ContextPackBuildResult.from_wire(document))
        _validate_pack_document(document)
    else:
        with pytest.raises(ContractSemanticError, match="no longer valid"):
            _validate_pack(ContextPackBuildResult.from_wire(document))


@pytest.mark.parametrize(
    "valid_from,accepted",
    [(T0, True), (T1, True), (T2, False)],
    ids=["valid-from-before-the-instant", "valid-from-at-the-instant", "not-yet-valid"],
)
def test_context_pack_current_record_validity_lower_boundary(
    valid_from: str, accepted: bool
) -> None:
    """The other end of the same containment, and inclusive at the same instant.

    A version whose validity *begins* at the resolution instant was already the answer at it,
    so `T1` is accepted; one that only becomes valid at `T2` was not yet in force when the
    pack resolved, so selecting it into `records` would present as current something that had
    not started. Pinned in both directions here rather than inferred from the `valid_until`
    boundary, since an implementation could easily get one end inclusive and the other not.
    """
    record = _canonical_knowledge_record_wire(
        record_id=PACK_RECORD_ID, version="v1", valid_from=valid_from
    )
    document = _signed(_context_pack_build_result_wire(records=[record]))
    if accepted:
        assert _is_schema_valid("ContextPackBuildResult", document)
        _validate_pack(ContextPackBuildResult.from_wire(document))
        _validate_pack_document(document)
    else:
        with pytest.raises(ContractSemanticError, match="not yet valid"):
            _validate_pack(ContextPackBuildResult.from_wire(document))


@pytest.mark.parametrize(
    "superseded_at,accepted",
    [(T1, False), (T2, True)],
    ids=["superseded-at-the-instant", "superseded-only-afterwards"],
)
def test_context_pack_evidence_supersession_boundary(
    superseded_at: str, accepted: bool
) -> None:
    """Supersession is exclusive where the closure is inclusive, and deliberately so: an
    artifact replaced *at* the instant a pack resolved was already not the live one."""
    document = _signed(
        _context_pack_build_result_wire(
            evidence=[_pack_evidence_wire(temporal=_temporal_wire(superseded_at=superseded_at))]
        )
    )
    if accepted:
        _validate_pack(ContextPackBuildResult.from_wire(document))
    else:
        with pytest.raises(ContractSemanticError, match="superseded at"):
            _validate_pack(ContextPackBuildResult.from_wire(document))


@pytest.mark.parametrize(
    "superseded_at,accepted",
    [(T1, True), (T2, False)],
    ids=["superseded-at-the-instant", "superseded-only-afterwards"],
)
def test_context_pack_historical_supersession_boundary(
    superseded_at: str, accepted: bool
) -> None:
    """The mirror image on the `history` partition: a version superseded *at* the instant is
    already history, and one superseded only afterwards was still canonical then, so it is
    not this partition's to carry."""
    document = _signed(
        _context_pack_build_result_wire(
            history=[_pack_history_wire(superseded_at=superseded_at)]
        )
    )
    if accepted:
        _validate_pack(ContextPackBuildResult.from_wire(document))
    else:
        with pytest.raises(ContractSemanticError):
            _validate_pack(ContextPackBuildResult.from_wire(document))


# --- 6g-ter. selected-record source cardinality -------------------------------

RECORD_SOURCES_MAX = 256


def _distinct_sources(count: int) -> list[dict[str, Any]]:
    """`count` sources with distinct `(kind, source_id)` keys, since a repeated declaration
    is rejected before any cardinality is reached."""
    return [_source_wire(source_id=f"doc-{index:03d}") for index in range(count)]


def test_context_pack_accepts_a_selected_record_at_the_source_maximum() -> None:
    document = _signed(
        _context_pack_build_result_wire(
            records=[_pack_record_wire(sources=_distinct_sources(RECORD_SOURCES_MAX))]
        )
    )
    assert _is_schema_valid("ContextPackBuildResult", document)
    _validate_pack(ContextPackBuildResult.from_wire(document))


def test_context_pack_rejects_a_selected_record_past_the_source_maximum() -> None:
    document = _signed(
        _context_pack_build_result_wire(
            records=[_pack_record_wire(sources=_distinct_sources(RECORD_SOURCES_MAX + 1))]
        )
    )
    assert not _is_schema_valid("ContextPackBuildResult", document)
    with pytest.raises(
        ContractSemanticError, match=f"sources has {RECORD_SOURCES_MAX + 1} entries"
    ):
        _validate_pack(ContextPackBuildResult.from_wire(document))
    with pytest.raises(
        ContractSemanticError, match=f"sources has {RECORD_SOURCES_MAX + 1} entries"
    ):
        _validate_pack_document(document)


def test_context_pack_citation_must_name_the_exact_evidence_id() -> None:
    """Same source, wrong identity: the citation resolves to nothing this pack selected."""
    citations = _canonical_citations()
    citations[0] = _evidence_citation_wire(
        citation_id="cit-1",
        evidence_reference=_context_pack_evidence_reference_wire(evidence_id="ev-other"),
    )
    result = _pack(citations=citations)
    with pytest.raises(ContractSemanticError, match="which this pack did not select"):
        _validate_pack(result)


def test_context_pack_citation_must_name_the_exact_evidence_checksum() -> None:
    """Right artifact, wrong content state -- which is the whole point of pinning it."""
    citations = _canonical_citations()
    citations[0] = _evidence_citation_wire(
        citation_id="cit-1",
        evidence_reference=_context_pack_evidence_reference_wire(
            content_checksum="sha256:" + "11" * 32
        ),
    )
    result = _pack(citations=citations)
    with pytest.raises(ContractSemanticError, match="which this pack did not select"):
        _validate_pack(result)


def test_context_pack_evidence_is_not_filtered_by_domain_or_type() -> None:
    """`EvidenceArtifact` carries neither field, so a filter here could only be invented."""
    request = _context_pack_request(
        domain_scope="personal.preferences", record_type="memory.fact"
    )
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(
                domain_scope="personal.preferences", record_type="memory.fact"
            )
        )
    )
    _validate_pack(result, request=request)


# --- 6h. selected governed records --------------------------------------------


# Each axis is broken with an unrecognized-but-well-formed open value rather than a known
# one, so no *other* validator has a contradiction to raise on first: these cases genuinely
# isolate the Context Pack partition rule instead of passing on a coincidence.
CURRENT_PARTITION_BREAKAGES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("layer", {"layer": "l4"}),
    ("governance_state", {"governance_state": "some_future_state"}),
    ("currentness", {"currentness": "some_future_currentness"}),
)


@pytest.mark.parametrize("axis,override", CURRENT_PARTITION_BREAKAGES)
def test_context_pack_records_partition_requires_exact_l2_current_canonical(
    axis: str, override: dict[str, Any]
) -> None:
    identity = _identity_wire(record_id=PACK_RECORD_ID, version="v1", **override)
    record = _governed_record_wire(
        authority_level="canonical",
        reviewer="reviewer-1",
        provenance=_provenance_wire(identity=identity),
    )
    with pytest.raises(ContractSemanticError, match="leaks non-canonical"):
        _validate_pack(_pack(records=[record]))


def test_context_pack_records_partition_requires_canonical_authority() -> None:
    result = _pack(records=[_pack_record_wire(authority_level="reviewed")])
    with pytest.raises(ContractSemanticError, match="authority_level"):
        _validate_pack(result)


def test_context_pack_records_partition_requires_a_reviewer() -> None:
    result = _pack(records=[_pack_record_wire(reviewer=None)])
    with pytest.raises(ContractSemanticError, match="reviewer"):
        _validate_pack(result)


def test_context_pack_records_partition_requires_validity_at_the_resolution_time() -> None:
    with pytest.raises(ContractSemanticError, match="not yet valid"):
        _validate_pack(_pack(records=[_pack_record_wire(valid_from=T2)]))
    with pytest.raises(ContractSemanticError, match="no longer valid"):
        _validate_pack(_pack(records=[_pack_record_wire(valid_until=T0)]))


def test_context_pack_records_partition_rejects_a_record_recorded_too_late() -> None:
    late = _canonical_knowledge_record_wire(record_id=PACK_RECORD_ID, version="v1")
    late["provenance"]["temporal"] = _temporal_wire(ingested_at=T2, recorded_at=T2)
    with pytest.raises(ContractSemanticError, match="after the canonical-resolution time"):
        _validate_pack(_pack(records=[late]))


def test_context_pack_context_models_partition_requires_l3() -> None:
    result = _pack(context_models=[_pack_context_model_wire(layer="l2")])
    with pytest.raises(ContractSemanticError, match="leaks non-canonical"):
        _validate_pack(result)


@pytest.mark.parametrize(
    "override,message",
    [
        ({"authority_level": "reviewed"}, "authority_level"),
        ({"reviewer": None}, "reviewer"),
        ({"governance_state": "some_future_state"}, "leaks non-canonical"),
        ({"currentness": "some_future_currentness"}, "leaks non-canonical"),
    ],
)
def test_context_pack_context_models_partition_holds_the_same_canonical_bar(
    override: dict[str, Any], message: str
) -> None:
    """L3 is a different namespace, not a weaker bar: every axis an L2 record is held to."""
    result = _pack(context_models=[_pack_context_model_wire(**override)])
    with pytest.raises(ContractSemanticError, match=message):
        _validate_pack(result)


def _current_record_superseded_at(partition: str, superseded_at: str) -> dict[str, Any]:
    """A record shaped for a *current* partition that nonetheless states when it was
    superseded, built by patching the instant onto the partition's own canonical wire so
    nothing else about the record moves."""
    builder = {
        "records": _pack_record_wire,
        "context_models": _pack_context_model_wire,
    }[partition]
    record = builder()
    record["provenance"]["temporal"]["superseded_at"] = superseded_at
    return record


@pytest.mark.parametrize("partition", ["records", "context_models"])
def test_context_pack_current_partitions_reject_a_superseded_at_after_the_resolution_time(
    partition: str,
) -> None:
    """A current partition requires `superseded_at` **absent**, irrespective of timestamp.

    `T2` is strictly *after* the resolution instant, so the "at or before the resolution
    instant" reading would admit it. It does not: a current version records no supersession
    at all, and one that states when it was replaced belongs to `history` whichever side of
    the resolution instant that statement falls on.

    The assertion is on the established current-record diagnostic
    (:func:`~semantics.validate_record_currentness_consistency`, composed by
    `validate_governed_record`) rather than on a nested-instant closure message: the closure
    deliberately does not bound `superseded_at`, so a nested-time diagnostic here would be
    asserting a rule that never fires on this field and would keep passing if the currentness
    rule were loosened.
    """
    result = _pack(**{partition: [_current_record_superseded_at(partition, T2)]})
    with pytest.raises(
        ContractSemanticError,
        match="currentness 'current' must not carry a superseded_at instant",
    ):
        _validate_pack(result)


def test_context_pack_history_partition_requires_a_superseded_version() -> None:
    result = _pack(history=[_pack_record_wire(record_id=PACK_HISTORY_ID, version="v1")])
    with pytest.raises(ContractSemanticError, match="l2/accepted/superseded"):
        _validate_pack(result)


def test_context_pack_history_partition_requires_canonical_authority() -> None:
    result = _pack(history=[_pack_history_wire(authority_level="reviewed")])
    with pytest.raises(ContractSemanticError, match="authority_level"):
        _validate_pack(result)


def test_context_pack_history_partition_requires_a_reviewer() -> None:
    result = _pack(history=[_pack_history_wire(reviewer=None)])
    with pytest.raises(ContractSemanticError, match="reviewer"):
        _validate_pack(result)


def test_context_pack_history_partition_requires_supersession_by_the_resolution_time() -> None:
    result = _pack(history=[_pack_history_wire(superseded_at=T2)])
    with pytest.raises(ContractSemanticError, match="after the"):
        _validate_pack(result)


def test_context_pack_rejects_a_record_from_another_workspace() -> None:
    result = _pack(records=[_pack_record_wire(workspace_id="ws-other")])
    with pytest.raises(ContractSemanticError, match="does not match the selected workspace"):
        _validate_pack(result)


def test_context_pack_rejects_the_same_identity_in_two_partitions() -> None:
    """A version returned as both current and historical is two contradictory claims, and
    neither the pack nor the frontier it was authorized from may state both."""
    citations = _canonical_citations()
    citations[2] = _record_citation_wire(
        citation_id="cit-3",
        record_reference=_record_version_reference_wire(record_id=PACK_RECORD_ID, version="v1"),
    )
    result = _pack(
        history=[_pack_history_wire(record_id=PACK_RECORD_ID, version="v1")],
        citations=citations,
        reproducibility=_reproducibility_wire(
            record_versions=[
                _record_version_reference_wire(record_id=PACK_CONTEXT_MODEL_ID, version="v1"),
                _record_version_reference_wire(record_id=PACK_RECORD_ID, version="v1"),
            ]
        ),
    )
    # Refused whichever way the caller frames the frontier, and the two framings fail for two
    # different stated reasons. A manifest that authorizes the version once leaves the second
    # partition's copy off the frontier entirely; a manifest that tries to authorize it in
    # both is itself the contradiction and never yields a digest. The pack's own
    # duplicate-identity check is kept as defence in depth behind them: no legal manifest can
    # reach it any more, which is exactly why loosening either manifest rule cannot silently
    # widen what a pack may present.
    authorized_once = _candidate_manifest(
        candidates=(
            _evidence_candidate(),
            _record_candidate("records", PACK_RECORD_ID),
            _record_candidate("context_models", PACK_CONTEXT_MODEL_ID),
        )
    )
    with pytest.raises(
        ContractSemanticError, match="the authorized candidate set does not contain"
    ):
        _validate_pack(result, expected_authorized_candidate_set=authorized_once)
    authorized_twice = _candidate_manifest(
        candidates=(
            _evidence_candidate(),
            _record_candidate("records", PACK_RECORD_ID),
            _record_candidate("history", PACK_RECORD_ID),
            _record_candidate("context_models", PACK_CONTEXT_MODEL_ID),
        )
    )
    with pytest.raises(
        ContractSemanticError, match="never two of them"
    ):
        _validate_pack(result, expected_authorized_candidate_set=authorized_twice)


def test_context_pack_rejects_unsorted_evidence() -> None:
    citations = [
        _evidence_citation_wire(
            citation_id="cit-1",
            evidence_reference=_context_pack_evidence_reference_wire(evidence_id="ev-2"),
        ),
        _evidence_citation_wire(
            citation_id="cit-2",
            evidence_reference=_context_pack_evidence_reference_wire(evidence_id="ev-1"),
        ),
    ]
    result = _pack(
        evidence=[_pack_evidence_wire(evidence_id="ev-2"), _pack_evidence_wire(evidence_id="ev-1")],
        records=[],
        history=[],
        context_models=[],
        citations=citations,
        sections=[_section_wire(citation_ids=["cit-1", "cit-2"])],
        reproducibility=_reproducibility_wire(
            evidence_versions=[
                _context_pack_evidence_reference_wire(evidence_id="ev-1"),
                _context_pack_evidence_reference_wire(evidence_id="ev-2"),
            ],
            record_versions=[],
        ),
    )
    frontier = _candidate_manifest(
        candidates=(
            _evidence_candidate(evidence_id="ev-1"),
            _evidence_candidate(evidence_id="ev-2"),
        )
    )
    with pytest.raises(ContractSemanticError, match="deterministic ascending"):
        _validate_pack(result, expected_authorized_candidate_set=frontier)


def test_context_pack_rejects_unsorted_records() -> None:
    citations = [
        _record_citation_wire(
            citation_id="cit-1",
            record_reference=_record_version_reference_wire(record_id="rec-a", version="v1"),
        ),
        _record_citation_wire(
            citation_id="cit-2",
            record_reference=_record_version_reference_wire(record_id="rec-b", version="v1"),
        ),
    ]
    result = _pack(
        evidence=[],
        records=[
            _pack_record_wire(record_id="rec-b", version="v1"),
            _pack_record_wire(record_id="rec-a", version="v1"),
        ],
        history=[],
        context_models=[],
        citations=citations,
        sections=[_section_wire(citation_ids=["cit-1", "cit-2"])],
        reproducibility=_reproducibility_wire(
            evidence_versions=[],
            record_versions=[
                _record_version_reference_wire(record_id="rec-a", version="v1"),
                _record_version_reference_wire(record_id="rec-b", version="v1"),
            ],
        ),
    )
    frontier = _candidate_manifest(
        candidates=(
            _record_candidate("records", "rec-a"),
            _record_candidate("records", "rec-b"),
        )
    )
    with pytest.raises(ContractSemanticError, match="deterministic ascending"):
        _validate_pack(result, expected_authorized_candidate_set=frontier)


# --- 6i. reproducibility version sets are exact -------------------------------


EVIDENCE_VERSION_BREAKAGES: tuple[tuple[str, list[dict[str, Any]]], ...] = (
    ("omission", []),
    (
        "addition",
        [
            {"evidence_id": PACK_EVIDENCE_ID, "content_checksum": EV_CHECKSUM},
            {"evidence_id": "ev-extra", "content_checksum": EV_CHECKSUM},
        ],
    ),
    (
        "duplicate",
        [
            {"evidence_id": PACK_EVIDENCE_ID, "content_checksum": EV_CHECKSUM},
            {"evidence_id": PACK_EVIDENCE_ID, "content_checksum": EV_CHECKSUM},
        ],
    ),
    (
        "wrong_checksum",
        [{"evidence_id": PACK_EVIDENCE_ID, "content_checksum": "sha256:" + "11" * 32}],
    ),
)


@pytest.mark.parametrize("kind,evidence_versions", EVIDENCE_VERSION_BREAKAGES)
def test_context_pack_evidence_versions_must_equal_the_selection(
    kind: str, evidence_versions: list[dict[str, Any]]
) -> None:
    result = _pack(reproducibility=_reproducibility_wire(evidence_versions=evidence_versions))
    with pytest.raises(ContractSemanticError, match="evidence_versions"):
        _validate_pack(result)


RECORD_VERSION_BREAKAGES: tuple[tuple[str, list[dict[str, Any]]], ...] = (
    ("omission", [{"record_id": PACK_RECORD_ID, "version": "v1"}]),
    (
        "addition",
        [
            {"record_id": PACK_CONTEXT_MODEL_ID, "version": "v1"},
            {"record_id": "rec-extra", "version": "v1"},
            {"record_id": PACK_RECORD_ID, "version": "v1"},
            {"record_id": PACK_HISTORY_ID, "version": "v1"},
        ],
    ),
    (
        "duplicate",
        [
            {"record_id": PACK_CONTEXT_MODEL_ID, "version": "v1"},
            {"record_id": PACK_CONTEXT_MODEL_ID, "version": "v1"},
            {"record_id": PACK_RECORD_ID, "version": "v1"},
            {"record_id": PACK_HISTORY_ID, "version": "v1"},
        ],
    ),
    (
        "order",
        [
            {"record_id": PACK_RECORD_ID, "version": "v1"},
            {"record_id": PACK_CONTEXT_MODEL_ID, "version": "v1"},
            {"record_id": PACK_HISTORY_ID, "version": "v1"},
        ],
    ),
    (
        "wrong_version",
        [
            {"record_id": PACK_CONTEXT_MODEL_ID, "version": "v1"},
            {"record_id": PACK_RECORD_ID, "version": "v2"},
            {"record_id": PACK_HISTORY_ID, "version": "v1"},
        ],
    ),
)


@pytest.mark.parametrize("kind,record_versions", RECORD_VERSION_BREAKAGES)
def test_context_pack_record_versions_must_equal_the_selected_union(
    kind: str, record_versions: list[dict[str, Any]]
) -> None:
    result = _pack(reproducibility=_reproducibility_wire(record_versions=record_versions))
    with pytest.raises(ContractSemanticError, match="record_versions"):
        _validate_pack(result)


FROZEN_REPRODUCIBILITY_LITERALS: tuple[tuple[str, str], ...] = (
    ("pack_format_version", "2.0"),
    ("artifact_canonicalization", "json_sorted_keys"),
)


@pytest.mark.parametrize("field_name,value", FROZEN_REPRODUCIBILITY_LITERALS)
def test_context_pack_frozen_reproducibility_literals(field_name: str, value: str) -> None:
    result = _pack(reproducibility=_reproducibility_wire(**{field_name: value}))
    with pytest.raises(ContractSemanticError, match=field_name):
        _validate_pack(result)


REPRODUCIBILITY_IDENTIFIER_FIELDS: tuple[str, ...] = (
    "builder_version",
    "retrieval_version",
    "ranking_version",
    "reranking_version",
    "selection_version",
    "tokenizer_id",
    "tokenizer_version",
    "summarizer_version",
)


@pytest.mark.parametrize("field_name", REPRODUCIBILITY_IDENTIFIER_FIELDS)
def test_context_pack_reproducibility_identifiers_are_shape_checked(field_name: str) -> None:
    result = _pack(reproducibility=_reproducibility_wire(**{field_name: "Not Valid!"}))
    with pytest.raises(ContractSemanticError, match=field_name):
        _validate_pack(result)


def test_context_pack_summarizer_version_is_required_and_may_say_disabled() -> None:
    assert sem_knowledge.CONTEXT_PACK_SUMMARIZER_DISABLED == "disabled"
    _validate_pack(_pack(reproducibility=_reproducibility_wire(summarizer_version="disabled")))
    _validate_pack(_pack(reproducibility=_reproducibility_wire(summarizer_version="sum-1")))
    with pytest.raises(ContractDecodeError, match="summarizer_version"):
        ContextPackBuildResult.from_wire(
            _mutate(
                _signed(_context_pack_build_result_wire()),
                "reproducibility",
                {
                    key: value
                    for key, value in _reproducibility_wire().items()
                    if key != "summarizer_version"
                },
            )
        )


def test_context_pack_model_versions_is_required_and_may_be_empty() -> None:
    _validate_pack(_pack(reproducibility=_reproducibility_wire(model_versions={})))
    _validate_pack(
        _pack(reproducibility=_reproducibility_wire(model_versions={"summarizer": "model-1"}))
    )
    result = _pack(reproducibility=_reproducibility_wire(model_versions={"summarizer": "Bad!"}))
    with pytest.raises(ContractSemanticError, match="model_versions"):
        _validate_pack(result)


# --- 6i-bis. producer assertions bound to caller-known values -----------------
#
# Everything in this block is *the producer's own statement about the producer*: nothing
# inside the artifact can contradict it, so with no expectation supplied the validator can
# only check shape -- which must not be mistaken for verification. Each case below mutates
# exactly one such value, re-signs so the digest is valid and the pack fails on the binding
# rather than incidentally on a stale checksum, and asserts three things: the pack passes
# with no expectation, passes with the matching expectation, and fails with a mismatching
# one. Without the third assertion an "expectation" that was accepted and never compared
# would pass this suite.

CALLER_BOUND_REPRODUCIBILITY_CASES: tuple[tuple[str, str, str, str], ...] = (
    ("builder_version", "builder-1", "builder-2", "reproducibility.builder_version"),
    ("retrieval_version", "retrieval-1", "retrieval-2", "reproducibility.retrieval_version"),
    ("ranking_version", "ranking-1", "ranking-2", "reproducibility.ranking_version"),
    ("reranking_version", "reranking-1", "reranking-2", "reproducibility.reranking_version"),
    ("selection_version", "selection-1", "selection-2", "reproducibility.selection_version"),
    ("tokenizer_id", "tokenizer-1", "tokenizer-2", "reproducibility.tokenizer_id"),
    ("tokenizer_version", "tokenizer-v1", "tokenizer-v2", "reproducibility.tokenizer_version"),
    ("summarizer_version", "disabled", "sum-1", "reproducibility.summarizer_version"),
)


@pytest.mark.parametrize(
    "field_name,stated,other,label", CALLER_BOUND_REPRODUCIBILITY_CASES
)
def test_context_pack_binds_each_expected_reproducibility_identifier(
    field_name: str, stated: str, other: str, label: str
) -> None:
    result = _pack(reproducibility=_reproducibility_wire(**{field_name: stated}))
    argument = f"expected_{field_name}"
    # No expectation: unchanged behaviour, shape only.
    _validate_pack(result)
    # Matching expectation: accepted.
    _validate_pack(result, **{argument: stated})
    # Mismatching expectation: the binding is real, not merely accepted and ignored.
    with pytest.raises(ContractSemanticError, match=label):
        _validate_pack(result, **{argument: other})


@pytest.mark.parametrize(
    "field_name,stated,other,label", CALLER_BOUND_REPRODUCIBILITY_CASES
)
def test_context_pack_document_binds_each_expected_reproducibility_identifier(
    field_name: str, stated: str, other: str, label: str
) -> None:
    """The same bindings on the raw-document entry point, which is the one a trust boundary
    uses and therefore the one that must not be missing them."""
    document = _signed(
        _context_pack_build_result_wire(
            reproducibility=_reproducibility_wire(**{field_name: stated})
        )
    )
    argument = f"expected_{field_name}"
    _validate_pack_document(document, **{argument: stated})
    with pytest.raises(ContractSemanticError, match=label):
        _validate_pack_document(document, **{argument: other})


def test_context_pack_binds_the_expected_normalized_query() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(normalized_query="hello world")
        )
    )
    _validate_pack(result)
    _validate_pack(result, expected_normalized_query="hello world")
    with pytest.raises(
        ContractSemanticError, match="normalized_request.normalized_query"
    ):
        _validate_pack(result, expected_normalized_query="something else")


def test_context_pack_binds_the_expected_normalization_version() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            normalized_request=_normalized_request_wire(normalization_version="norm-7")
        )
    )
    _validate_pack(result)
    _validate_pack(result, expected_normalization_version="norm-7")
    with pytest.raises(
        ContractSemanticError, match="normalized_request.normalization_version"
    ):
        _validate_pack(result, expected_normalization_version="norm-8")


def test_context_pack_recomputes_the_candidate_set_checksum_from_the_manifest() -> None:
    """The stated checksum is checked against a digest recomputed from the caller's own
    manifest, never against itself."""
    result = _pack()
    _validate_pack(result)
    wider = _candidate_manifest(
        candidates=(*_canonical_candidates(), _record_candidate("records", "rec-extra"))
    )
    with pytest.raises(ContractSemanticError, match="authorized_candidate_set_checksum"):
        _validate_pack(result, expected_authorized_candidate_set=wider)


@pytest.mark.parametrize(
    "stated,matching,mismatching",
    [
        ({}, {}, {"summarizer": "model-1"}),
        ({"summarizer": "model-1"}, {"summarizer": "model-1"}, {}),
        (
            {"summarizer": "model-1"},
            {"summarizer": "model-1"},
            {"summarizer": "model-2"},
        ),
    ],
    ids=["absent-is-expectable", "present-vs-absent", "wrong-version"],
)
def test_context_pack_binds_the_expected_model_versions(
    stated: dict[str, str], matching: dict[str, str], mismatching: dict[str, str]
) -> None:
    """`model_versions` is required and may be empty, so `{}` is a caller expecting a build
    that used no model -- distinct from `None`, which expects nothing. No sentinel needed."""
    result = _pack(reproducibility=_reproducibility_wire(model_versions=stated))
    _validate_pack(result)
    _validate_pack(result, expected_model_versions=matching)
    with pytest.raises(ContractSemanticError, match="model_versions"):
        _validate_pack(result, expected_model_versions=mismatching)


MALFORMED_EXPECTATIONS: tuple[tuple[str, Any], ...] = (
    ("expected_builder_version", "Not Valid!"),
    ("expected_builder_version", 7),
    ("expected_normalized_query", ""),
    ("expected_normalized_query", 7),
    ("expected_normalization_version", "Not Valid!"),
    ("expected_tokenizer_id", "Not Valid!"),
    ("expected_model_versions", {"Bad Role": "model-1"}),
    ("expected_model_versions", {"summarizer": "Not Valid!"}),
    ("expected_model_versions", ["summarizer"]),
)


@pytest.mark.parametrize("argument,value", MALFORMED_EXPECTATIONS)
def test_context_pack_rejects_a_malformed_expectation(argument: str, value: Any) -> None:
    """A caller vouching for a malformed value has made a mistake about what it is vouching
    for; saying so beats an inequality against something that could never have appeared."""
    with pytest.raises(ContractSemanticError, match=argument):
        _validate_pack(_pack(), **{argument: value})


def test_context_pack_expected_reproducibility_arguments_are_all_optional() -> None:
    """Producer assertions stay optional and every external binding stays mandatory --
    including `expected_authorized_candidate_set`, which is deliberately *not* one of the
    optional producer assertions: it is the one field that must never be taken on trust."""
    signature = inspect.signature(sem_knowledge.validate_context_pack_build_result)
    document_signature = inspect.signature(
        sem_knowledge.validate_context_pack_build_result_document
    )
    expected_arguments = {
        "expected_normalized_query",
        "expected_normalization_version",
        "expected_builder_version",
        "expected_retrieval_version",
        "expected_ranking_version",
        "expected_reranking_version",
        "expected_selection_version",
        "expected_tokenizer_id",
        "expected_tokenizer_version",
        "expected_summarizer_version",
        "expected_model_versions",
    }
    for name in expected_arguments:
        for target in (signature, document_signature):
            parameter = target.parameters[name]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is None
    # And the mandatory external bindings stayed mandatory.
    for name in (
        "request",
        "expected_workspace_id",
        "expected_authority",
        "expected_scopes",
        "expected_purpose",
        "expected_policy_versions",
        "expected_authorized_candidate_set",
        "canonical_resolution_time",
        "response_freshness",
    ):
        for target in (signature, document_signature):
            parameter = target.parameters[name]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is inspect.Parameter.empty


# --- 6j. freshness and the pinned instants ------------------------------------


def test_context_pack_requires_strict_freshness() -> None:
    for freshness in (
        _freshness_wire(as_of=T1, projection_versions={}),
        _freshness_wire(as_of=T1, projection_watermarks={}),
        _freshness_wire(as_of=T1, projection_watermarks={"other_index": "wm-1"}),
    ):
        result = _pack(reproducibility=_reproducibility_wire(freshness=freshness))
        with pytest.raises(ContractSemanticError):
            _validate_pack(result, response_freshness=ProjectionFreshness.from_wire(freshness))


def test_context_pack_freshness_must_equal_the_response_freshness() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            freshness=_freshness_wire(as_of=T1, projection_versions={"knowledge_index": "pv-2"})
        )
    )
    with pytest.raises(ContractSemanticError, match="response's own freshness"):
        _validate_pack(result)


def test_context_pack_freshness_as_of_must_be_the_resolution_time() -> None:
    freshness = _freshness_wire(as_of=T0)
    result = _pack(reproducibility=_reproducibility_wire(freshness=freshness))
    with pytest.raises(ContractSemanticError, match="freshness.as_of"):
        _validate_pack(result, response_freshness=ProjectionFreshness.from_wire(freshness))


def test_context_pack_resolution_time_must_equal_the_validator_argument() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(canonical_resolution_time=T2, generated_at=T2)
    )
    with pytest.raises(ContractSemanticError, match="canonical_resolution_time"):
        _validate_pack(result)


def test_context_pack_generated_at_must_equal_the_resolution_time() -> None:
    result = _pack(reproducibility=_reproducibility_wire(generated_at=T2))
    with pytest.raises(ContractSemanticError, match="generated_at"):
        _validate_pack(result)


def test_context_pack_rejects_a_malformed_resolution_time() -> None:
    result = _pack(
        reproducibility=_reproducibility_wire(
            canonical_resolution_time="not-a-timestamp", generated_at="not-a-timestamp"
        )
    )
    with pytest.raises(ContractSemanticError, match="canonical_resolution_time"):
        _validate_pack(result, canonical_resolution_time="not-a-timestamp")


# --- 6k. citations and sections -----------------------------------------------


def test_context_pack_citation_union_decodes_exactly_one_branch() -> None:
    evidence = generated.context_pack_citation_from_wire(_evidence_citation_wire())
    record = generated.context_pack_citation_from_wire(_record_citation_wire())
    assert isinstance(evidence, ContextPackEvidenceCitation)
    assert isinstance(record, ContextPackRecordCitation)
    assert generated.context_pack_citation_to_wire(evidence) == _evidence_citation_wire()
    both = {**_evidence_citation_wire(), "record_reference": _record_version_reference_wire()}
    with pytest.raises(ContractDecodeError, match="exactly one"):
        generated.context_pack_citation_from_wire(both)
    with pytest.raises(ContractDecodeError, match="exactly one"):
        generated.context_pack_citation_from_wire({"citation_id": "cit-1"})
    assert not _is_schema_valid("ContextPackCitation", both)
    assert not _is_schema_valid("ContextPackCitation", {"citation_id": "cit-1"})


def test_context_pack_rejects_a_dangling_citation() -> None:
    citations = _canonical_citations()
    citations[1] = _record_citation_wire(
        citation_id="cit-2",
        record_reference=_record_version_reference_wire(record_id="rec-unselected", version="v1"),
    )
    with pytest.raises(ContractSemanticError, match="which this pack did not select"):
        _validate_pack(_pack(citations=citations))


def test_context_pack_rejects_an_uncited_selected_item() -> None:
    citations = _canonical_citations()[:3]
    result = _pack(citations=citations, sections=[_section_wire(citation_ids=["cit-1", "cit-2", "cit-3"])])
    with pytest.raises(ContractSemanticError, match="selected content nothing cites"):
        _validate_pack(result)


def test_context_pack_rejects_a_citation_no_section_uses() -> None:
    result = _pack(sections=[_section_wire(citation_ids=["cit-1", "cit-2", "cit-3"])])
    with pytest.raises(ContractSemanticError, match="used by no section"):
        _validate_pack(result)


def test_context_pack_rejects_a_duplicate_citation_id() -> None:
    citations = _canonical_citations()
    citations[3] = _record_citation_wire(
        citation_id="cit-3",
        record_reference=_record_version_reference_wire(
            record_id=PACK_CONTEXT_MODEL_ID, version="v1"
        ),
    )
    result = _pack(
        citations=citations, sections=[_section_wire(citation_ids=["cit-1", "cit-2", "cit-3"])]
    )
    with pytest.raises(ContractSemanticError, match="duplicates citation_id"):
        _validate_pack(result)


def test_context_pack_rejects_unsorted_citations() -> None:
    citations = _canonical_citations()
    citations[0], citations[1] = citations[1], citations[0]
    with pytest.raises(ContractSemanticError, match="deterministic ascending"):
        _validate_pack(_pack(citations=citations))


def test_context_pack_rejects_two_citations_of_the_same_target_and_location() -> None:
    citations = _canonical_citations()
    citations.append(
        _record_citation_wire(
            citation_id="cit-5",
            record_reference=_record_version_reference_wire(
                record_id=PACK_RECORD_ID, version="v1"
            ),
        )
    )
    result = _pack(
        citations=citations,
        sections=[_section_wire(citation_ids=[*ALL_CITATION_IDS, "cit-5"])],
    )
    with pytest.raises(ContractSemanticError, match="same content_pointer and source_span"):
        _validate_pack(result)


def test_context_pack_accepts_the_same_target_at_two_distinct_locations() -> None:
    citations = _canonical_citations()
    citations.append(
        _record_citation_wire(
            citation_id="cit-5",
            record_reference=_record_version_reference_wire(
                record_id=PACK_RECORD_ID, version="v1"
            ),
            content_pointer="/claims/1",
            source_span={"pointer": "/body", "start_offset": 10, "end_offset": 20},
        )
    )
    _validate_pack(
        _pack(
            citations=citations,
            sections=[_section_wire(citation_ids=[*ALL_CITATION_IDS, "cit-5"])],
        )
    )


def test_context_pack_citation_source_span_reuses_the_records_rule() -> None:
    citations = _canonical_citations()
    citations[1] = _record_citation_wire(
        citation_id="cit-2",
        record_reference=_record_version_reference_wire(record_id=PACK_RECORD_ID, version="v1"),
        source_span={"pointer": "/body", "start_offset": 20, "end_offset": 10},
    )
    with pytest.raises(ContractSemanticError, match="start_offset"):
        _validate_pack(_pack(citations=citations))


def test_context_pack_citation_bounds_its_opaque_locator_and_excerpt() -> None:
    for override, message in (
        ({"content_pointer": "p" * 2049}, "content_pointer"),
        ({"excerpt": "e" * 4097}, "excerpt"),
    ):
        citations = _canonical_citations()
        citations[1] = _record_citation_wire(
            citation_id="cit-2",
            record_reference=_record_version_reference_wire(
                record_id=PACK_RECORD_ID, version="v1"
            ),
            **override,
        )
        with pytest.raises(ContractSemanticError, match=message):
            _validate_pack(_pack(citations=citations))


def test_context_pack_rejects_a_section_citation_id_that_does_not_resolve() -> None:
    result = _pack(sections=[_section_wire(citation_ids=[*ALL_CITATION_IDS, "cit-missing"])])
    with pytest.raises(ContractSemanticError, match="does not resolve to a citation"):
        _validate_pack(result)


def test_context_pack_rejects_a_duplicate_section_id() -> None:
    result = _pack(
        sections=[
            _section_wire(section_id="sec-1", citation_ids=["cit-1", "cit-2"]),
            _section_wire(section_id="sec-1", citation_ids=["cit-3", "cit-4"]),
        ],
        budget=_budget_wire(tokens_used=SECTION_TOKENS * 2),
    )
    with pytest.raises(ContractSemanticError, match="duplicates section_id"):
        _validate_pack(result)


def test_context_pack_rejects_unsorted_sections() -> None:
    result = _pack(
        sections=[
            _section_wire(section_id="sec-2", citation_ids=["cit-1", "cit-2"]),
            _section_wire(section_id="sec-1", citation_ids=["cit-3", "cit-4"]),
        ],
        budget=_budget_wire(tokens_used=SECTION_TOKENS * 2),
    )
    with pytest.raises(ContractSemanticError, match="deterministic ascending"):
        _validate_pack(result)


def test_context_pack_rejects_unsorted_section_citation_ids() -> None:
    result = _pack(sections=[_section_wire(citation_ids=["cit-2", "cit-1", "cit-3", "cit-4"])])
    with pytest.raises(ContractSemanticError, match="deterministic ascending"):
        _validate_pack(result)


def test_context_pack_section_ids_and_citation_ids_are_independent_namespaces() -> None:
    """The same string may name a section and a citation; neither collides with the other."""
    citations = _canonical_citations()
    citations[0] = _evidence_citation_wire(citation_id="shared-id")
    result = _pack(
        citations=sorted(citations, key=lambda item: item["citation_id"]),
        sections=[
            _section_wire(
                section_id="shared-id",
                citation_ids=sorted(["shared-id", "cit-2", "cit-3", "cit-4"]),
            )
        ],
    )
    _validate_pack(result)


def test_context_pack_section_content_is_bounded_and_non_empty() -> None:
    for content, message in (("", "content"), ("x" * 16385, "content")):
        result = _pack(sections=[_section_wire(content=content)])
        with pytest.raises(ContractSemanticError, match=message):
            _validate_pack(result)


# --- 6l. conflicts, uncertainties, omissions ----------------------------------


def test_context_pack_conflict_ids_must_resolve() -> None:
    result = _pack(
        conflicts=[{"description": "clash", "conflicting_citation_ids": ["cit-1", "cit-missing"]}]
    )
    with pytest.raises(ContractSemanticError, match="does not resolve to a citation"):
        _validate_pack(result)


def test_context_pack_conflict_needs_two_sides() -> None:
    result = _pack(conflicts=[{"description": "clash", "conflicting_citation_ids": ["cit-1"]}])
    with pytest.raises(ContractSemanticError, match="bounded range"):
        _validate_pack(result)


def test_context_pack_uncertainty_ids_must_resolve() -> None:
    result = _pack(uncertainties=[{"description": "unsure", "related_citation_ids": ["cit-x"]}])
    with pytest.raises(ContractSemanticError, match="does not resolve to a citation"):
        _validate_pack(result)


def test_context_pack_uncertainty_needs_an_anchor() -> None:
    result = _pack(uncertainties=[{"description": "unsure", "related_citation_ids": []}])
    with pytest.raises(ContractSemanticError, match="bounded range"):
        _validate_pack(result)


def test_context_pack_conflicts_and_uncertainties_span_evidence_and_records() -> None:
    """The citation-id anchor is what makes one reference system cover both."""
    _validate_pack(
        _pack(
            conflicts=[
                {"description": "clash", "conflicting_citation_ids": ["cit-1", "cit-2"]},
            ],
            uncertainties=[
                {"description": "unsure", "related_citation_ids": ["cit-1", "cit-4"]},
            ],
        )
    )


def test_context_pack_rejects_unsorted_conflicts() -> None:
    result = _pack(
        conflicts=[
            {"description": "b", "conflicting_citation_ids": ["cit-2", "cit-3"]},
            {"description": "a", "conflicting_citation_ids": ["cit-1", "cit-2"]},
        ]
    )
    with pytest.raises(ContractSemanticError, match="deterministic ascending"):
        _validate_pack(result)


def test_context_pack_orders_conflicts_by_description_when_ids_match() -> None:
    result = _pack(
        conflicts=[
            {"description": "b", "conflicting_citation_ids": ["cit-1", "cit-2"]},
            {"description": "a", "conflicting_citation_ids": ["cit-1", "cit-2"]},
        ]
    )
    with pytest.raises(ContractSemanticError, match="deterministic ascending"):
        _validate_pack(result)


def test_context_pack_rejects_unsorted_uncertainties() -> None:
    result = _pack(
        uncertainties=[
            {"description": "a", "related_citation_ids": ["cit-3"]},
            {"description": "a", "related_citation_ids": ["cit-1"]},
        ]
    )
    with pytest.raises(ContractSemanticError, match="deterministic ascending"):
        _validate_pack(result)


def test_context_pack_rejects_unsorted_omissions() -> None:
    result = _pack(
        omissions=[
            {"code": "budget_exceeded", "message": "trimmed"},
            {"code": "acl_filtered"},
        ]
    )
    with pytest.raises(ContractSemanticError, match="deterministic ascending"):
        _validate_pack(result)


def test_context_pack_orders_omissions_with_absent_optionals_as_empty() -> None:
    _validate_pack(
        _pack(
            omissions=[
                {"code": "acl_filtered"},
                {"code": "acl_filtered", "path": "/records"},
                {"code": "budget_exceeded", "message": "trimmed"},
            ]
        )
    )


def test_context_pack_rejects_a_duplicate_omission() -> None:
    result = _pack(omissions=[{"code": "acl_filtered"}, {"code": "acl_filtered"}])
    with pytest.raises(ContractSemanticError, match="deterministic ascending"):
        _validate_pack(result)


# --- 6m. authority is never granted by possession ------------------------------


def test_context_pack_requires_fresh_authorization_flag_true() -> None:
    result = _pack(fresh_authorization_required=False)
    with pytest.raises(ContractSemanticError, match="fresh_authorization_required"):
        _validate_pack(result)


# --- 6n. content addressing ---------------------------------------------------


INTEGRITY_BEARING_MUTATIONS: tuple[tuple[str, Any], ...] = (
    ("query", "a different query"),
    ("mode", "future_mode"),
    ("sections.0.content", "different content"),
    ("sections.0.token_count", 13),
    ("sections.0.kind", "evidence_digest"),
    ("evidence.0.evidence_id", "ev-2"),
    ("evidence.0.sensitivity", "confidential"),
    ("records.0.authority_level", "reviewed"),
    ("records.0.content", {"fact": "changed"}),
    ("history.0.reviewer", "reviewer-2"),
    ("context_models.0.domain_scope", "work.projects"),
    ("citations.0.citation_id", "cit-0"),
    ("conflicts", [{"description": "clash", "conflicting_citation_ids": ["cit-1", "cit-2"]}]),
    ("uncertainties", [{"description": "unsure", "related_citation_ids": ["cit-1"]}]),
    ("omissions", [{"code": "acl_filtered"}]),
    ("budget.tokens_used", 13),
    ("budget.token_budget", 999),
    ("fresh_authorization_required", False),
    ("reproducibility.builder_version", "builder-2"),
    ("reproducibility.generated_at", T2),
    ("reproducibility.canonical_resolution_time", T2),
    ("reproducibility.freshness.stale", True),
    ("reproducibility.freshness.projection_versions", {"knowledge_index": "pv-9"}),
    ("reproducibility.authorization_context.purpose", "analytics.export"),
    ("reproducibility.authorization_context.policy_versions", {"acl": "pv-acl-9"}),
    (
        "reproducibility.authorization_context.authority.principal_id",
        "principal-2",
    ),
    ("reproducibility.authorization_context.pre_ranking_authorization_enforced", False),
    ("reproducibility.normalized_request.normalized_query", "hi"),
    ("reproducibility.normalized_request.normalization_version", "norm-2"),
    ("reproducibility.evidence_versions", []),
    ("reproducibility.record_versions", []),
    ("reproducibility.tokenizer_version", "tokenizer-v2"),
    ("reproducibility.model_versions", {"summarizer": "model-1"}),
    ("reproducibility.summarizer_version", "sum-1"),
)


@pytest.mark.parametrize("path,value", INTEGRITY_BEARING_MUTATIONS)
def test_context_pack_digest_covers_every_integrity_bearing_field(path: str, value: Any) -> None:
    """Nothing in the result is outside the digest except the two members it becomes."""
    signed = _signed(_context_pack_build_result_wire())
    original = signed["pack_id"]
    mutated = _mutate(signed, path, value)
    assert sem_knowledge.compute_context_pack_artifact_digest(mutated) != original


def test_context_pack_digest_ignores_only_the_two_members_it_becomes() -> None:
    signed = _signed(_context_pack_build_result_wire())
    original = signed["pack_id"]
    for path in ("pack_id", "reproducibility.artifact_checksum"):
        mutated = _mutate(signed, path, "sha256:" + "cc" * 32)
        assert sem_knowledge.compute_context_pack_artifact_digest(mutated) == original


def test_context_pack_digest_is_stable_under_object_member_reordering() -> None:
    """Member order is not part of a JSON object's value, and JCS says so."""
    signed = _signed(_context_pack_build_result_wire())
    reordered = json.loads(json.dumps(dict(reversed(list(signed.items())))))
    assert sem_knowledge.compute_context_pack_artifact_digest(reordered) == signed["pack_id"]
    assert list(reordered) != list(signed)


def test_context_pack_digest_changes_under_array_reordering() -> None:
    """An array is ordered by definition, so reordering one is a different pack."""
    signed = _signed(_context_pack_build_result_wire())
    reordered = _mutate(signed, "sections.0.citation_ids", list(reversed(ALL_CITATION_IDS)))
    assert sem_knowledge.compute_context_pack_artifact_digest(reordered) != signed["pack_id"]


def test_context_pack_requires_pack_id_to_equal_the_checksum() -> None:
    signed = _signed(_context_pack_build_result_wire())
    broken = _mutate(signed, "pack_id", "sha256:" + "cc" * 32)
    with pytest.raises(ContractSemanticError, match="pack_id"):
        _validate_pack(ContextPackBuildResult.from_wire(broken))


def test_context_pack_requires_the_checksum_to_be_the_digest() -> None:
    signed = _signed(_context_pack_build_result_wire())
    broken = _mutate(signed, "reproducibility.artifact_checksum", "sha256:" + "cc" * 32)
    with pytest.raises(ContractSemanticError, match="artifact_checksum"):
        _validate_pack(ContextPackBuildResult.from_wire(broken))


MALFORMED_DIGESTS: tuple[str, ...] = (
    "sha256:" + "AB" * 32,
    "sha256:" + "ab" * 31,
    "sha256:" + "ab" * 33,
    "sha512:" + "ab" * 32,
    "ab" * 32,
    "sha256:",
    "sha256:" + "zz" * 32,
)


@pytest.mark.parametrize("digest", MALFORMED_DIGESTS)
def test_context_pack_rejects_a_malformed_pack_id(digest: str) -> None:
    signed = _signed(_context_pack_build_result_wire())
    broken = _mutate(signed, "pack_id", digest)
    assert not _is_schema_valid("ContextPackBuildResult", broken)
    with pytest.raises(ContractSemanticError, match="ContextPackDigest"):
        _validate_pack(ContextPackBuildResult.from_wire(broken))


# --- 6o. raw-wire verification ------------------------------------------------


def test_context_pack_document_rejects_an_unknown_additive_field() -> None:
    """The one thing the DTO path cannot see: a field the tolerant decoder drops before the
    digest is ever computed, so the bytes verified would not be the bytes received."""
    signed = _signed(_context_pack_build_result_wire())
    document = json.dumps({**signed, "future_hint_field": "some-value"})
    with pytest.raises(ContractSemanticError, match="round-trip exactly"):
        sem_knowledge.verify_context_pack_artifact_document(document)
    # The DTO path really does drop it, which is why the raw path exists.
    assert ContextPackBuildResult.from_wire(json.loads(document)) == ContextPackBuildResult.from_wire(signed)


def test_context_pack_document_rejects_a_nested_unknown_field() -> None:
    signed = _signed(_context_pack_build_result_wire())
    tampered = _mutate(
        signed, "reproducibility", {**signed["reproducibility"], "future_hint_field": 1}
    )
    with pytest.raises(ContractSemanticError, match="round-trip exactly"):
        sem_knowledge.verify_context_pack_artifact_document(json.dumps(tampered))


def test_context_pack_document_rejects_a_duplicated_member_name() -> None:
    signed = _signed(_context_pack_build_result_wire())
    document = json.dumps(signed)
    tampered = document.replace('"query":', '"query": "shadowed", "query":', 1)
    with pytest.raises(ContractSemanticError, match="duplicate object member name"):
        sem_knowledge.verify_context_pack_artifact_document(tampered)


def test_context_pack_document_accepts_utf8_bytes() -> None:
    signed = _signed(_context_pack_build_result_wire())
    result = sem_knowledge.verify_context_pack_artifact_document(json.dumps(signed).encode("utf-8"))
    assert result.pack_id == signed["pack_id"]


def test_context_pack_document_rejects_a_tampered_digest() -> None:
    signed = _signed(_context_pack_build_result_wire())
    tampered = _mutate(signed, "query", "a different query")
    with pytest.raises(ContractSemanticError, match="artifact_checksum"):
        sem_knowledge.verify_context_pack_artifact_document(json.dumps(tampered))


def test_context_pack_document_rejects_an_unrecognized_canonicalization() -> None:
    """A checksum means nothing without the canonical form it was computed over."""
    document = _context_pack_build_result_wire(
        reproducibility=_reproducibility_wire(artifact_canonicalization="json_sorted_keys")
    )
    signed = _signed(document)
    with pytest.raises(ContractSemanticError, match="artifact_canonicalization"):
        sem_knowledge.verify_context_pack_artifact_document(json.dumps(signed))


def test_context_pack_document_rejects_an_unrecognized_pack_format() -> None:
    signed = _signed(
        _context_pack_build_result_wire(reproducibility=_reproducibility_wire(pack_format_version="2.0"))
    )
    with pytest.raises(ContractSemanticError, match="pack_format_version"):
        sem_knowledge.verify_context_pack_artifact_document(json.dumps(signed))


def test_context_pack_document_rejects_malformed_json() -> None:
    with pytest.raises(ContractSemanticError, match="not valid JSON"):
        sem_knowledge.verify_context_pack_artifact_document("{")


def test_context_pack_document_rejects_a_non_object_root() -> None:
    with pytest.raises(ContractSemanticError, match="expected a mapping"):
        sem_knowledge.verify_context_pack_artifact_document("[]")


def test_context_pack_dto_digest_helper_documents_its_boundary() -> None:
    """A mapping helper cannot recover what an earlier parser already discarded, so both
    the strict object and the raw document must agree on the same digest."""
    signed = _signed(_context_pack_build_result_wire())
    decoded = ContextPackBuildResult.from_wire(signed)
    assert sem_knowledge.compute_context_pack_artifact_digest(decoded.to_wire()) == signed["pack_id"]
    assert sem_knowledge.compute_context_pack_artifact_digest(signed) == signed["pack_id"]


# --- 6p. public surface and module boundary -----------------------------------


CONTEXT_PACK_PUBLIC_NAMES: tuple[str, ...] = (
    "CONTEXT_PACK_ARTIFACT_CANONICALIZATION",
    "CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT",
    "CONTEXT_PACK_CANDIDATE_PARTITIONS",
    "CONTEXT_PACK_CANDIDATE_PARTITION_CONTEXT_MODELS",
    "CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE",
    "CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY",
    "CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS",
    "CONTEXT_PACK_DIGEST_ALGORITHM",
    "CONTEXT_PACK_FORMAT_VERSION",
    "CONTEXT_PACK_GOVERNED_CANDIDATE_PARTITIONS",
    "CONTEXT_PACK_MAX_TOKEN_BUDGET",
    "CONTEXT_PACK_MAX_TOKEN_COUNT",
    "CONTEXT_PACK_MIN_TOKEN_BUDGET",
    "CONTEXT_PACK_MODES",
    "CONTEXT_PACK_MODE_DETERMINISTIC_VIEW",
    "CONTEXT_PACK_NORMALIZED_REQUEST_VIEW",
    "CONTEXT_PACK_REJECTED_INPUT_FIELDS",
    "CONTEXT_PACK_REJECTED_MODES",
    "CONTEXT_PACK_SUMMARIZER_DISABLED",
    "compute_authorized_candidate_set_checksum",
    "compute_context_pack_artifact_digest",
    "decode_context_pack_build_input",
    "validate_context_pack_build_input",
    "validate_context_pack_build_result",
    "validate_context_pack_build_result_document",
    "validate_context_pack_citation",
    "verify_context_pack_artifact_document",
)


@pytest.mark.parametrize("name", CONTEXT_PACK_PUBLIC_NAMES)
def test_context_pack_names_are_publicly_exported(name: str) -> None:
    assert name in sem_knowledge.__all__
    assert name in v1.__all__
    assert getattr(v1, name) is getattr(sem_knowledge, name)


CONTEXT_PACK_GENERATED_NAMES: tuple[str, ...] = (
    "ContextPackAuthorizationContext",
    "ContextPackAuthorizedCandidate",
    "ContextPackAuthorizedCandidateSetManifest",
    "ContextPackAuthorizedEvidenceCandidate",
    "ContextPackAuthorizedRecordCandidate",
    "ContextPackBudget",
    "ContextPackBuildInput",
    "ContextPackBuildResult",
    "ContextPackCitation",
    "ContextPackConflict",
    "ContextPackDigest",
    "ContextPackEvidenceCitation",
    "ContextPackEvidenceReference",
    "ContextPackMode",
    "ContextPackNormalizedRequest",
    "ContextPackRecordCitation",
    "ContextPackReproducibility",
    "ContextPackSection",
    "ContextPackTokenBudget",
    "ContextPackTokenCount",
    "ContextPackUncertainty",
    "context_pack_citation_from_wire",
    "context_pack_citation_to_wire",
)


@pytest.mark.parametrize("name", CONTEXT_PACK_GENERATED_NAMES)
def test_context_pack_generated_names_are_exported(name: str) -> None:
    assert name in generated.__all__
    assert name in v1.__all__


def test_context_pack_definitions_are_published_by_the_registry() -> None:
    registry = json.loads((SCHEMA_DIR / "application-v1.schema.json").read_text(encoding="utf-8"))
    source = json.loads((SCHEMA_DIR / "context-pack.schema.json").read_text(encoding="utf-8"))
    for name in source["$defs"]:
        assert registry["$defs"][name]["$ref"] == (
            f"{BASE_URI}context-pack.schema.json#/$defs/{name}"
        )


def test_context_pack_definitions_reach_the_generated_typescript() -> None:
    typescript = (REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts").read_text(
        encoding="utf-8"
    )
    source = json.loads((SCHEMA_DIR / "context-pack.schema.json").read_text(encoding="utf-8"))
    for name in source["$defs"]:
        assert f" {name} " in typescript, f"generated TypeScript is missing {name}"


def test_context_pack_schema_is_part_of_the_packaged_resource_set() -> None:
    """The wheel force-includes this whole directory (`pyproject.toml`), and
    `tests/contracts/test_resources.py` exercises the packaged accessors over everything in
    it, so what matters here is that the document is *in* the set and parses."""
    packaged = {path.name for path in SCHEMA_DIR.glob("*.schema.json")}
    assert "context-pack.schema.json" in packaged
    document = json.loads((SCHEMA_DIR / "context-pack.schema.json").read_text(encoding="utf-8"))
    assert document["$id"] == f"{BASE_URI}context-pack.schema.json"
    assert "ContextPackSection" in document["$defs"]


CONTRACT_MODULE_IMPORTS: dict[str, frozenset[str]] = {
    "semantics_knowledge": frozenset(
        {
            "__future__",
            "re",
            "collections.abc",
            "datetime",
            "hashlib",
            "typing",
            "omnivia_core.contracts.v1.canonical_json",
            "omnivia_core.contracts.v1.compatibility",
            "omnivia_core.contracts.v1.generated",
            "omnivia_core.contracts.v1.semantics",
            "omnivia_core.contracts.v1.semantics_evidence",
        }
    ),
    "canonical_json": frozenset(
        {
            "__future__",
            "json",
            "collections.abc",
            "math",
            "typing",
            "omnivia_core.contracts.v1.compatibility",
        }
    ),
}


@pytest.mark.parametrize("module_name,expected", sorted(CONTRACT_MODULE_IMPORTS.items()))
def test_context_pack_modules_import_only_the_standard_library_and_this_package(
    module_name: str, expected: frozenset[str]
) -> None:
    """A frozen import set, so a runtime, storage, transport, or provider dependency is a
    visible diff rather than something a reader has to notice."""
    source = (
        REPO_ROOT / "src" / "omnivia_core" / "contracts" / "v1" / f"{module_name}.py"
    ).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "absolute imports only"
            assert node.module is not None
            imported.add(node.module)
    assert imported == expected
