"""The pure stage of `engineering.context.build` (§12.3a-12.6).

`build_pack` takes a frozen `BuildContext` and immutable authorised inputs and
returns the pack. It reads no clock, connection or ambient state: the same
inputs give canonical-identical bytes and the same checksum. The handler
captures everything the context holds exactly once, before calling it.

Token counts use Core's pinned `context-pack.tokenizer.v1` pattern count. That
is a deterministic count, not a model tokenizer, and the pack says so.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.storage.context_pack import (
    CONTEXT_PACK_TOKENIZER_ID,
    CONTEXT_PACK_TOKENIZER_VERSION,
    _token_count,
)

RENDERER_VERSION: Final = "eng-render-2"
BUILDER_VERSION: Final = "eng-build-2"
BYTE_ONLY_RENDERER_VERSION: Final = "eng-render-3"
BYTE_ONLY_BUILDER_VERSION: Final = "eng-build-3"
CONFLICT_RENDERER_VERSION: Final = "eng-render-4"
CONFLICT_BUILDER_VERSION: Final = "eng-build-4"
BYTE_ONLY_COUNTING_MODE: Final = "byte_only.v1"
WORKING_CONTEXT_SHARE_DIVISOR: Final = 4

#: Dropped last-first when the rendering exceeds a budget. Accepted knowledge
#: and the uncertainty notice are mandatory and never dropped (§12.5).
DROP_ORDER: Final[tuple[str, ...]] = (
    "working_context",
    "history",
    "candidate_findings",
)
_PARTITION_RANK: Final = {"accepted_knowledge": 0, "candidate_findings": 1}

TOKENIZER_NOTE: Final = (
    "pattern count under the named tokenizer; not a model tokenizer and no "
    "model-tokenizer accuracy is claimed"
)
CONFLICT_NOTE: Final = (
    "These cited claims materially conflict and remain unresolved; do not treat "
    "any one as the sole conclusion."
)
OMITTED_CONFLICT_NOTE: Final = (
    "Conflicting cited claims were omitted; no conclusion from the group is safe."
)
OVERLAP_NOTE: Final = (
    "These cited claims have an unresolved potential overlap; no material-conflict "
    "assessment is available, so do not treat either as the sole conclusion."
)
OMITTED_OVERLAP_NOTE: Final = (
    "Cited claims with unresolved potential overlap were omitted; no conclusion "
    "from the group is safe."
)


class MandatoryContextTooLarge(Exception):
    """The notice plus accepted knowledge alone exceed the effective budget."""


@dataclass(frozen=True, slots=True)
class BuildContext:
    """Every fact the build depends on, captured once by the handler."""

    resolved_at_us: int
    workspace_id: str
    principal_id: str
    query: str
    profile: str
    mode: str
    targets: tuple[Mapping[str, Any], ...]
    source_coverage: tuple[Mapping[str, Any], ...]
    requested_budget: Mapping[str, int] | None
    effective_tokens: int
    effective_bytes: int
    projection_version: int
    applicability_evaluator: str
    counting_mode: str | None = None
    effective_hydrations: int = 8
    effective_evidence_bytes: int = 262_144
    effective_authorized_candidates: int = 2_000
    hydrations: int = 0
    source_bytes_read: int = 0
    selection_profile: str | None = None
    authorized_frontier_digest: str | None = None
    authorized_candidate_count: int | None = None


@dataclass(frozen=True, slots=True)
class PackRecord:
    """One authorised record already reduced to what the pack may render."""

    record_id: str
    version: str
    partition: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    record_id: str
    version: str


@dataclass(frozen=True, slots=True)
class ConflictGroup:
    records: tuple[ConflictRecord, ...]
    status: str = "unresolved"
    omitted: bool = False


@dataclass(frozen=True, slots=True)
class _NormalizedConflictGroup:
    records: tuple[tuple[str, str], ...]
    status: str
    omitted: bool


@dataclass(frozen=True, slots=True)
class WorkingItem:
    checkpoint_id: str
    sequence: int
    objective: str
    unresolved: tuple[str, ...]


def _normalize_uncertainties(notice: str, uncertainties: list[str]) -> list[str]:
    """Notice first, then supplied uncertainties, deduped on first occurrence."""

    seen: set[str] = set()
    normalized: list[str] = []
    for item in (notice, *uncertainties):
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    return normalized


def _normalize_conflict_groups(
    records: tuple[PackRecord, ...], conflict_groups: tuple[ConflictGroup, ...]
) -> tuple[_NormalizedConflictGroup, ...]:
    """Merge duplicate/overlapping groups and return deterministic components."""

    record_keys = [(record.record_id, record.version) for record in records]
    if len(set(record_keys)) != len(record_keys):
        raise ValueError("pack record identities must be unique")
    available = set(record_keys)
    parent = {key: key for group in conflict_groups for key in (
        (record.record_id, record.version) for record in group.records
    )}

    def find(key: tuple[str, str]) -> tuple[str, str]:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        parent[second] = first

    initially_omitted: set[tuple[str, str]] = set()
    material_conflict_keys: set[tuple[str, str]] = set()
    for group in conflict_groups:
        if group.status not in {"unresolved", "unresolved_overlap"}:
            raise ValueError("the conflict group status is not renderable")
        keys = tuple(
            sorted({(record.record_id, record.version) for record in group.records})
        )
        if len(keys) < 2:
            raise ValueError("a conflict group must name two or more records")
        if not group.omitted and not set(keys) <= available:
            raise ValueError("a rendered conflict group must name selected records")
        if group.omitted:
            initially_omitted.update(keys)
        if group.status == "unresolved":
            material_conflict_keys.update(keys)
        for key in keys[1:]:
            union(keys[0], key)

    grouped: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for key in parent:
        grouped.setdefault(find(key), set()).add(key)
    order = {key: index for index, key in enumerate(record_keys)}

    def component_order(key: tuple[str, str]) -> tuple[int, int, str, str]:
        if key in order:
            return (0, order[key], "", "")
        return (1, 0, key[0], key[1])

    components = [
        _NormalizedConflictGroup(
            records=tuple(sorted(keys, key=component_order)),
            status=(
                "unresolved" if keys & material_conflict_keys else "unresolved_overlap"
            ),
            omitted=bool(keys & initially_omitted),
        )
        for keys in grouped.values()
        if len(keys) >= 2
    ]
    components.sort(
        key=lambda component: min(component_order(key) for key in component.records)
    )
    return tuple(components)


def _arrange_records(
    records: tuple[PackRecord, ...],
    components: tuple[_NormalizedConflictGroup, ...],
) -> tuple[PackRecord, ...]:
    baseline = sorted(records, key=lambda record: _PARTITION_RANK[record.partition])
    component_by_key = {
        key: component.records
        for component in components
        if not component.omitted
        for key in component.records
    }
    records_by_key = {
        (record.record_id, record.version): record for record in baseline
    }
    baseline_order = {
        (record.record_id, record.version): index
        for index, record in enumerate(baseline)
    }
    arranged: list[PackRecord] = []
    emitted: set[tuple[tuple[str, str], ...]] = set()
    for record in baseline:
        key = (record.record_id, record.version)
        component = component_by_key.get(key)
        if component is None:
            arranged.append(record)
            continue
        if component in emitted:
            continue
        emitted.add(component)
        arranged.extend(
            records_by_key[item]
            for item in sorted(component, key=baseline_order.__getitem__)
        )
    return tuple(arranged)


def _render_conflict(conflict: Mapping[str, Any]) -> str:
    citations = " ".join(f"[{citation}]" for citation in conflict["citation_ids"])
    return (
        f"[conflict {conflict['wire']['status']}] "
        f"{conflict['wire']['note']} {citations}"
    ).strip()


def _render(
    notices: list[str],
    sections: list[dict[str, Any]],
    labels: Mapping[str, str],
    conflicts: list[dict[str, Any]],
) -> str:
    # Every mandatory uncertainty is rendered first, one block per notice;
    # every heading, label, separator and citation label below is part of
    # the counted text.
    parts = ["[uncertainty] " + notice for notice in notices]
    parts.extend(_render_conflict(conflict) for conflict in conflicts if conflict["omitted"])
    for section in sections:
        parts.extend(
            _render_conflict(conflict)
            for conflict in conflicts
            if not conflict["omitted"]
            and conflict["anchor_section_id"] == section["section_id"]
        )
        label = f"[{section['partition']}]"
        if section["partition"] == "working_context":
            label += f" [uncited checkpoint {labels[section['section_id']]}]"
        cites = " ".join(f"[{c}]" for c in section["citation_ids"])
        parts.append(f"{label} {section['content']} {cites}".strip())
    return "\n\n".join(parts)


def _build_render_items(
    records: tuple[PackRecord, ...],
    working: tuple[WorkingItem, ...],
    conflict_groups: tuple[ConflictGroup, ...],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    list[dict[str, Any]],
]:
    components = _normalize_conflict_groups(records, conflict_groups)
    omitted_record_keys = {
        key
        for component in components
        if component.omitted
        for key in component.records
    }
    ordered = _arrange_records(
        tuple(
            record
            for record in records
            if (record.record_id, record.version) not in omitted_record_keys
        ),
        components,
    )
    ordered_index = {
        (record.record_id, record.version): index
        for index, record in enumerate(ordered)
    }
    components = tuple(
        _NormalizedConflictGroup(
            records=(
                component.records
                if component.omitted
                else tuple(
                    sorted(component.records, key=ordered_index.__getitem__)
                )
            ),
            status=component.status,
            omitted=component.omitted,
        )
        for component in components
    )
    sections: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    citation_by_record: dict[tuple[str, str], str] = {}
    section_by_record: dict[tuple[str, str], str] = {}
    for ordinal, record in enumerate(ordered, 1):
        section_id = f"sec-{ordinal}"
        citation_id = f"cite-{ordinal}"
        key = (record.record_id, record.version)
        section_by_record[key] = section_id
        citation_by_record[key] = citation_id
        sections.append(
            {
                "section_id": section_id,
                "kind": "decision_summary",
                "partition": record.partition,
                "content": f"{record.title}. {record.body}".strip(),
                "citation_ids": [citation_id],
            }
        )
        citations.append(
            {
                "citation_id": citation_id,
                "record_ref": {
                    "record_id": record.record_id,
                    "version": record.version,
                },
            }
        )
    conflict_only_keys = tuple(
        key
        for component in components
        for key in component.records
        if key not in citation_by_record
    )
    for key in dict.fromkeys(conflict_only_keys):
        citation_id = f"cite-{len(citations) + 1}"
        citation_by_record[key] = citation_id
        citations.append(
            {
                "citation_id": citation_id,
                "record_ref": {"record_id": key[0], "version": key[1]},
            }
        )
    labels: dict[str, str] = {}
    for ordinal, item in enumerate(working, len(sections) + 1):
        section_id = f"sec-{ordinal}"
        labels[section_id] = f"{item.checkpoint_id}#{item.sequence}"
        sections.append(
            {
                "section_id": section_id,
                "kind": "working_context",
                "partition": "working_context",
                "content": (
                    f"{item.objective} Unresolved: " + "; ".join(item.unresolved)
                ).strip(),
                "citation_ids": [],
            }
        )
    conflicts = [
        {
            "record_keys": component.records,
            "section_ids": tuple(
                section_by_record[key]
                for key in component.records
                if key in section_by_record
            ),
            "anchor_section_id": (
                None
                if component.omitted
                else section_by_record[component.records[0]]
            ),
            "citation_ids": tuple(
                citation_by_record[key] for key in component.records
            ),
            "omitted": component.omitted,
            "wire": {
                "records": [
                    {"record_id": record_id, "version": version}
                    for record_id, version in component.records
                ],
                "status": component.status,
                "note": (
                    OMITTED_CONFLICT_NOTE
                    if component.omitted and component.status == "unresolved"
                    else OMITTED_OVERLAP_NOTE
                    if component.omitted
                    else CONFLICT_NOTE
                    if component.status == "unresolved"
                    else OVERLAP_NOTE
                ),
            },
        }
        for component in components
    ]
    return sections, citations, labels, conflicts


def _drop_one_budget_item(
    sections: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    omissions: list[dict[str, Any]],
) -> bool:
    conflict_by_section = {
        section_id: conflict
        for conflict in conflicts
        if not conflict["omitted"]
        for section_id in conflict["section_ids"]
    }
    droppable = [
        index
        for index, section in enumerate(sections)
        if section["partition"] in DROP_ORDER
    ]
    target_conflict = (
        None
        if not droppable
        else conflict_by_section.get(sections[droppable[-1]]["section_id"])
    )
    if target_conflict is None and droppable:
        dropped = sections.pop(droppable[-1])
        omissions.append({"field": dropped["section_id"], "reason": "budget"})
        return True
    if target_conflict is None:
        full_conflicts = [
            conflict
            for conflict in conflicts
            if not conflict["omitted"]
            and any(
                section["section_id"] in conflict["section_ids"]
                for section in sections
            )
        ]
        if not full_conflicts:
            return False
        target_conflict = full_conflicts[-1]
    removed = set(target_conflict["section_ids"])
    sections[:] = [
        section for section in sections if section["section_id"] not in removed
    ]
    target_conflict["omitted"] = True
    target_conflict["wire"]["note"] = (
        OMITTED_CONFLICT_NOTE
        if target_conflict["wire"]["status"] == "unresolved"
        else OMITTED_OVERLAP_NOTE
    )
    omissions.append({"field": "sections", "reason": "conflict_group_budget"})
    return True


def _enforce_working_context_share(
    *,
    notices: list[str],
    sections: list[dict[str, Any]],
    labels: Mapping[str, str],
    conflicts: list[dict[str, Any]],
    omissions: list[dict[str, Any]],
    effective_bytes: int,
    effective_tokens: int | None,
) -> None:
    """Keep model-facing working context within its accepted 25% share.

    The request contract has no expanded-working-context control, so every accepted
    request uses the default quarter share. Measuring the full rendering with and
    without working sections counts their headings, checkpoint labels and separators;
    no payload-only estimate stands in for what the model actually receives.
    """

    byte_limit = effective_bytes // WORKING_CONTEXT_SHARE_DIVISOR
    token_limit = (
        None
        if effective_tokens is None
        else effective_tokens // WORKING_CONTEXT_SHARE_DIVISOR
    )
    while True:
        without_working = [
            section
            for section in sections
            if section["partition"] != "working_context"
        ]
        if len(without_working) == len(sections):
            return
        full_text = _render(notices, sections, labels, conflicts)
        base_text = _render(notices, without_working, labels, conflicts)
        working_bytes = len(full_text.encode("utf-8")) - len(base_text.encode("utf-8"))
        working_tokens = (
            None
            if token_limit is None
            else _token_count(full_text) - _token_count(base_text)
        )
        if working_bytes <= byte_limit and (
            token_limit is None or working_tokens is not None and working_tokens <= token_limit
        ):
            return
        dropped_index = next(
            index
            for index in range(len(sections) - 1, -1, -1)
            if sections[index]["partition"] == "working_context"
        )
        dropped = sections.pop(dropped_index)
        omissions.append(
            {"field": dropped["section_id"], "reason": "working_context_share"}
        )


def build_pack(
    ctx: BuildContext,
    records: tuple[PackRecord, ...],
    working: tuple[WorkingItem, ...],
    *,
    notice: str,
    uncertainties: list[str],
    omissions: list[dict[str, Any]],
    conflict_groups: tuple[ConflictGroup, ...] = (),
) -> dict[str, Any]:
    sections, citations, labels, conflicts = _build_render_items(
        records, working, conflict_groups
    )

    # Copy caller-owned mutable inputs now: later caller-side mutation must
    # never invalidate the checksum already computed over this pack.
    uncertainties = _normalize_uncertainties(notice, uncertainties)
    omissions = [dict(o) for o in omissions]
    if any(conflict["omitted"] for conflict in conflicts):
        omissions.append(
            {"field": "sections", "reason": "conflict_group_selection"}
        )
    _enforce_working_context_share(
        notices=uncertainties,
        sections=sections,
        labels=labels,
        conflicts=conflicts,
        omissions=omissions,
        effective_bytes=ctx.effective_bytes,
        effective_tokens=ctx.effective_tokens,
    )
    while True:
        text = _render(uncertainties, sections, labels, conflicts)
        token_count = _token_count(text)
        byte_count = len(text.encode("utf-8"))
        if token_count <= ctx.effective_tokens and byte_count <= ctx.effective_bytes:
            break
        if not _drop_one_budget_item(sections, conflicts, omissions):
            raise MandatoryContextTooLarge
    kept = {c for s in sections for c in s["citation_ids"]} | {
        citation
        for conflict in conflicts
        for citation in conflict["citation_ids"]
    }
    citations = [c for c in citations if c["citation_id"] in kept]

    reproducibility: dict[str, Any] = {
        "builder_version": (
            CONFLICT_BUILDER_VERSION if conflicts else BUILDER_VERSION
        ),
        "renderer_version": (
            CONFLICT_RENDERER_VERSION if conflicts else RENDERER_VERSION
        ),
        "tokenizer_id": CONTEXT_PACK_TOKENIZER_ID,
        "tokenizer_version": CONTEXT_PACK_TOKENIZER_VERSION,
        "tokenizer_note": TOKENIZER_NOTE,
        "projection_version": ctx.projection_version,
        "artifact_canonicalization": "rfc8785",
        "resolution_instant_us": ctx.resolved_at_us,
    }
    if ctx.selection_profile is not None:
        reproducibility["selection_profile"] = ctx.selection_profile
    if ctx.authorized_frontier_digest is not None:
        reproducibility["authorized_frontier_digest"] = ctx.authorized_frontier_digest
    if ctx.authorized_candidate_count is not None:
        reproducibility["authorized_candidate_count"] = ctx.authorized_candidate_count
    normalized_request: dict[str, Any] = {"query": ctx.query, "profile": ctx.profile}
    if ctx.source_coverage:
        normalized_request["applicability_mode"] = ctx.mode
        reproducibility["applicability_evaluator"] = ctx.applicability_evaluator
        reproducibility["source_coverage"] = [dict(c) for c in ctx.source_coverage]

    # A rendered section or exact cited conflict warning is substantive content.
    # Both can be `matched` after current_safe proved every referenced record;
    # generic authorization withholding carries no refs and makes no such claim.
    status = (
        "matched"
        if ctx.source_coverage
        and (
            any(section["citation_ids"] for section in sections)
            or bool(conflicts)
        )
        else "not_evaluated"
    )
    budget: dict[str, Any] = {
        "effective": {
            "model_tokens": ctx.effective_tokens,
            "model_bytes": ctx.effective_bytes,
        },
        "rendered_tokens": token_count,
        "rendered_bytes": byte_count,
        "source_bytes_read": ctx.source_bytes_read,
        "hydrations": ctx.hydrations,
    }
    if ctx.selection_profile is not None:
        budget["effective"].update(
            {
                "hydrations": ctx.effective_hydrations,
                "evidence_bytes": ctx.effective_evidence_bytes,
                "authorized_candidates": ctx.effective_authorized_candidates,
            }
        )
    if ctx.requested_budget is not None:
        budget["requested"] = dict(ctx.requested_budget)

    pack: dict[str, Any] = {
        "format_version": "engineering_context.v1",
        "normalized_request": normalized_request,
        "targets": [dict(t) for t in ctx.targets],
        "profile": ctx.profile,
        "sections": sections,
        "citations": citations,
        "conflicts": [dict(conflict["wire"]) for conflict in conflicts],
        "uncertainties": uncertainties,
        "omissions": omissions,
        "rendering": {
            "text": text,
            "renderer_version": (
                CONFLICT_RENDERER_VERSION if conflicts else RENDERER_VERSION
            ),
            "token_count": token_count,
            "byte_count": byte_count,
        },
        "budget": budget,
        "applicability": [{"snapshot": dict(t), "status": status} for t in ctx.targets],
        "authorization_context": {
            "workspace_id": ctx.workspace_id,
            "principal_id": ctx.principal_id,
        },
        "reproducibility": reproducibility,
        "fresh_authorization_required": True,
    }
    pack_id = (
        "sha256:" + hashlib.sha256(to_canonical_json(pack).encode("utf-8")).hexdigest()
    )
    pack["pack_id"] = pack_id
    reproducibility["artifact_checksum"] = pack_id
    return pack


def build_pack_byte_only(
    ctx: BuildContext,
    records: tuple[PackRecord, ...],
    working: tuple[WorkingItem, ...],
    *,
    notice: str,
    uncertainties: list[str],
    omissions: list[dict[str, Any]],
    conflict_groups: tuple[ConflictGroup, ...] = (),
) -> dict[str, Any]:
    """Build the explicit byte-only v2 representation.

    The complete rendering is measured directly as UTF-8.  This path never calls
    the legacy pattern tokenizer and never emits a token estimate.
    """

    if ctx.counting_mode != BYTE_ONLY_COUNTING_MODE:
        raise ValueError("byte-only builder requires byte_only.v1")

    sections, citations, labels, conflicts = _build_render_items(
        records, working, conflict_groups
    )

    uncertainties = _normalize_uncertainties(notice, uncertainties)
    omissions = [dict(omission) for omission in omissions]
    if any(conflict["omitted"] for conflict in conflicts):
        omissions.append(
            {"field": "sections", "reason": "conflict_group_selection"}
        )
    _enforce_working_context_share(
        notices=uncertainties,
        sections=sections,
        labels=labels,
        conflicts=conflicts,
        omissions=omissions,
        effective_bytes=ctx.effective_bytes,
        effective_tokens=None,
    )
    while True:
        text = _render(uncertainties, sections, labels, conflicts)
        byte_count = len(text.encode("utf-8"))
        if byte_count <= ctx.effective_bytes:
            break
        if not _drop_one_budget_item(sections, conflicts, omissions):
            raise MandatoryContextTooLarge

    kept = {citation for section in sections for citation in section["citation_ids"]} | {
        citation
        for conflict in conflicts
        for citation in conflict["citation_ids"]
    }
    citations = [
        citation
        for citation in citations
        if citation["citation_id"] in kept
    ]

    reproducibility: dict[str, Any] = {
        "builder_version": (
            CONFLICT_BUILDER_VERSION if conflicts else BYTE_ONLY_BUILDER_VERSION
        ),
        "renderer_version": (
            CONFLICT_RENDERER_VERSION if conflicts else BYTE_ONLY_RENDERER_VERSION
        ),
        "counting_mode": BYTE_ONLY_COUNTING_MODE,
        "projection_version": ctx.projection_version,
        "artifact_canonicalization": "rfc8785",
        "resolution_instant_us": ctx.resolved_at_us,
    }
    if ctx.selection_profile is not None:
        reproducibility["selection_profile"] = ctx.selection_profile
    if ctx.authorized_frontier_digest is not None:
        reproducibility["authorized_frontier_digest"] = ctx.authorized_frontier_digest
    if ctx.authorized_candidate_count is not None:
        reproducibility["authorized_candidate_count"] = ctx.authorized_candidate_count
    normalized_request: dict[str, Any] = {
        "query": ctx.query,
        "profile": ctx.profile,
        "counting_mode": BYTE_ONLY_COUNTING_MODE,
    }
    if ctx.source_coverage:
        normalized_request["applicability_mode"] = ctx.mode
        reproducibility["applicability_evaluator"] = ctx.applicability_evaluator
        reproducibility["source_coverage"] = [
            dict(coverage) for coverage in ctx.source_coverage
        ]

    status = (
        "matched"
        if ctx.source_coverage
        and (
            any(section["citation_ids"] for section in sections)
            or bool(conflicts)
        )
        else "not_evaluated"
    )
    budget: dict[str, Any] = {
        "effective": {
            "model_bytes": ctx.effective_bytes,
            "hydrations": ctx.effective_hydrations,
            "evidence_bytes": ctx.effective_evidence_bytes,
        },
        "rendered_bytes": byte_count,
        "source_bytes_read": ctx.source_bytes_read,
        "hydrations": ctx.hydrations,
    }
    if ctx.selection_profile is not None:
        budget["effective"]["authorized_candidates"] = (
            ctx.effective_authorized_candidates
        )
    if ctx.requested_budget is not None:
        budget["requested"] = dict(ctx.requested_budget)

    pack: dict[str, Any] = {
        "format_version": "engineering_context.v2",
        "normalized_request": normalized_request,
        "targets": [dict(target) for target in ctx.targets],
        "profile": ctx.profile,
        "sections": sections,
        "citations": citations,
        "conflicts": [dict(conflict["wire"]) for conflict in conflicts],
        "uncertainties": uncertainties,
        "omissions": omissions,
        "rendering": {
            "text": text,
            "renderer_version": (
                CONFLICT_RENDERER_VERSION
                if conflicts
                else BYTE_ONLY_RENDERER_VERSION
            ),
            "byte_count": byte_count,
        },
        "budget": budget,
        "applicability": [
            {"snapshot": dict(target), "status": status} for target in ctx.targets
        ],
        "authorization_context": {
            "workspace_id": ctx.workspace_id,
            "principal_id": ctx.principal_id,
        },
        "reproducibility": reproducibility,
        "fresh_authorization_required": True,
    }
    pack_id = (
        "sha256:"
        + hashlib.sha256(to_canonical_json(pack).encode("utf-8")).hexdigest()
    )
    pack["pack_id"] = pack_id
    reproducibility["artifact_checksum"] = pack_id
    return pack
