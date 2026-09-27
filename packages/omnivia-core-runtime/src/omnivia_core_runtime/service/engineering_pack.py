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
class WorkingItem:
    checkpoint_id: str
    sequence: int
    objective: str
    unresolved: tuple[str, ...]


def _render(
    notice: str, sections: list[dict[str, Any]], labels: Mapping[str, str]
) -> str:
    # The notice is mandatory and first; every heading, label, separator and
    # citation label below is part of the counted text.
    parts = ["[uncertainty] " + notice]
    for section in sections:
        label = f"[{section['partition']}]"
        if section["partition"] == "working_context":
            label += f" [uncited checkpoint {labels[section['section_id']]}]"
        cites = " ".join(f"[{c}]" for c in section["citation_ids"])
        parts.append(f"{label} {section['content']} {cites}".strip())
    return "\n\n".join(parts)


def _enforce_working_context_share(
    *,
    notice: str,
    sections: list[dict[str, Any]],
    labels: Mapping[str, str],
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
        full_text = _render(notice, sections, labels)
        base_text = _render(notice, without_working, labels)
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
) -> dict[str, Any]:
    # Stable: groups by partition without disturbing the frozen authorized
    # selection order (relevance/priority) within a partition.
    ordered = sorted(records, key=lambda r: _PARTITION_RANK[r.partition])
    sections: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    for ordinal, record in enumerate(ordered, 1):
        sections.append(
            {
                "section_id": f"sec-{ordinal}",
                "kind": "decision_summary",
                "partition": record.partition,
                "content": f"{record.title}. {record.body}".strip(),
                "citation_ids": [f"cite-{ordinal}"],
            }
        )
        citations.append(
            {
                "citation_id": f"cite-{ordinal}",
                "record_ref": {
                    "record_id": record.record_id,
                    "version": record.version,
                },
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

    # Copy caller-owned mutable inputs now: later caller-side mutation must
    # never invalidate the checksum already computed over this pack.
    uncertainties = list(uncertainties)
    omissions = [dict(o) for o in omissions]
    _enforce_working_context_share(
        notice=notice,
        sections=sections,
        labels=labels,
        omissions=omissions,
        effective_bytes=ctx.effective_bytes,
        effective_tokens=ctx.effective_tokens,
    )
    while True:
        text = _render(notice, sections, labels)
        token_count = _token_count(text)
        byte_count = len(text.encode("utf-8"))
        if token_count <= ctx.effective_tokens and byte_count <= ctx.effective_bytes:
            break
        droppable = [i for i, s in enumerate(sections) if s["partition"] in DROP_ORDER]
        if not droppable:
            raise MandatoryContextTooLarge
        dropped = sections.pop(droppable[-1])
        omissions.append({"field": dropped["section_id"], "reason": "budget"})
    kept = {c for s in sections for c in s["citation_ids"]}
    citations = [c for c in citations if c["citation_id"] in kept]

    reproducibility: dict[str, Any] = {
        "builder_version": BUILDER_VERSION,
        "renderer_version": RENDERER_VERSION,
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

    # `matched` only when current_safe proved at least one record that
    # actually survived into the rendered pack; a pack left with no cited
    # sections (every candidate dropped for budget, or none selected) makes
    # no applicability claim.
    status = "matched" if ctx.source_coverage and citations else "not_evaluated"
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
        "conflicts": [],
        "uncertainties": uncertainties,
        "omissions": omissions,
        "rendering": {
            "text": text,
            "renderer_version": RENDERER_VERSION,
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
) -> dict[str, Any]:
    """Build the explicit byte-only v2 representation.

    The complete rendering is measured directly as UTF-8.  This path never calls
    the legacy pattern tokenizer and never emits a token estimate.
    """

    if ctx.counting_mode != BYTE_ONLY_COUNTING_MODE:
        raise ValueError("byte-only builder requires byte_only.v1")

    ordered = sorted(records, key=lambda record: _PARTITION_RANK[record.partition])
    sections: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    for ordinal, record in enumerate(ordered, 1):
        sections.append(
            {
                "section_id": f"sec-{ordinal}",
                "kind": "decision_summary",
                "partition": record.partition,
                "content": f"{record.title}. {record.body}".strip(),
                "citation_ids": [f"cite-{ordinal}"],
            }
        )
        citations.append(
            {
                "citation_id": f"cite-{ordinal}",
                "record_ref": {
                    "record_id": record.record_id,
                    "version": record.version,
                },
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

    uncertainties = list(uncertainties)
    omissions = [dict(omission) for omission in omissions]
    _enforce_working_context_share(
        notice=notice,
        sections=sections,
        labels=labels,
        omissions=omissions,
        effective_bytes=ctx.effective_bytes,
        effective_tokens=None,
    )
    while True:
        text = _render(notice, sections, labels)
        byte_count = len(text.encode("utf-8"))
        if byte_count <= ctx.effective_bytes:
            break
        droppable = [
            index
            for index, section in enumerate(sections)
            if section["partition"] in DROP_ORDER
        ]
        if not droppable:
            raise MandatoryContextTooLarge
        dropped = sections.pop(droppable[-1])
        omissions.append({"field": dropped["section_id"], "reason": "budget"})

    kept = {citation for section in sections for citation in section["citation_ids"]}
    citations = [
        citation
        for citation in citations
        if citation["citation_id"] in kept
    ]

    reproducibility: dict[str, Any] = {
        "builder_version": BYTE_ONLY_BUILDER_VERSION,
        "renderer_version": BYTE_ONLY_RENDERER_VERSION,
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

    status = "matched" if ctx.source_coverage and citations else "not_evaluated"
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
    if ctx.requested_budget is not None:
        budget["requested"] = dict(ctx.requested_budget)

    pack: dict[str, Any] = {
        "format_version": "engineering_context.v2",
        "normalized_request": normalized_request,
        "targets": [dict(target) for target in ctx.targets],
        "profile": ctx.profile,
        "sections": sections,
        "citations": citations,
        "conflicts": [],
        "uncertainties": uncertainties,
        "omissions": omissions,
        "rendering": {
            "text": text,
            "renderer_version": BYTE_ONLY_RENDERER_VERSION,
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
