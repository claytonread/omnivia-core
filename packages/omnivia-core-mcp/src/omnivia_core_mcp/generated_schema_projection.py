# Generated file. Do not edit.
#
# Source of truth:
#   contracts/application/v1/schemas (Application Contract v1, ADR-038)
# Generator:
#   scripts/generate-mcp-exposure-schemas.py
#
# Regenerate: python scripts/generate-mcp-exposure-schemas.py
# Verify:     python scripts/generate-mcp-exposure-schemas.py --check

"""Self-contained JSON Schemas for the MCP exposure manifest.

One entry per schema reference the curated manifest advertises, keyed by the
canonical Application Contract v1 reference the operation catalogue names. Each
value is a closed JSON Schema 2020-12 document: the referenced definition
verbatim, plus its complete transitive ``$defs`` closure, with every canonical
absolute reference rewritten to a local ``#/$defs/...`` one. Nothing here needs
network resolution, and nothing here is hand-written.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["SCHEMAS"]

#: Canonical schema reference -> the self-contained document advertised for it.
SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    "https://contracts.omnivia.dev/application/v1/context-pack.schema.json#/$defs/ContextPackBuildInput": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ContextPackBuildInput",
        "description": "Input for `context_pack.build`. Workspace-scoped: the workspace, principal, scopes, and purpose are the request envelope's; this payload never carries a second, independent copy of any of them, and selecting content never grants new authority beyond what the envelope already carries. Deliberately minimal: no view selector, no point-in-time selector, no pagination, and no persistence, expiry, retention, snapshot, or job control. The v1 operation resolves the current canonical view synchronously and persists nothing, so none of those controls has a meaning here, and a payload that smuggles one in is rejected rather than ignored.",
        "type": "object",
        "properties": {
            "query": {
                "$ref": "#/$defs/memory__MemoryQuery",
                "description": "Original caller query this pack is built for. The server normalizes it; the normalized form appears only on the result's `reproducibility.normalized_request`.",
            },
            "mode": {
                "$ref": "#/$defs/context_pack__ContextPackMode",
                "description": "How to build the pack. v1 recognizes only `deterministic_view`.",
            },
            "token_budget": {
                "$ref": "#/$defs/context_pack__ContextPackTokenBudget",
                "description": "Bounded, strictly positive maximum token budget for the built pack.",
            },
            "domain_scope": {
                "$ref": "#/$defs/memory__RecordDomainScope",
                "description": "Restrict governed-record selection to this domain scope, when set.",
            },
            "record_type": {
                "$ref": "#/$defs/memory__GovernedRecordType",
                "description": "Restrict governed-record selection to this record type, when set.",
            },
        },
        "required": [
            "query",
            "mode",
            "token_budget",
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "context_pack__ContextPackMode": {
                "title": "ContextPackMode",
                "description": "Open, dot-namespaced code naming how a Context Pack was produced. Wire-open by shape so a compatible minor release can add vocabulary, but trust-sensitive: v1 recognizes exactly one value, `deterministic_view` (a regenerated, non-persisted, deterministic view), and semantic validation fails closed on every other value rather than guessing at it. `immutable_snapshot` is deliberately not a v1 mode -- persistence is an operation posture this read does not have -- and `returned_artifact` was never a wire mode at all.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "context_pack__ContextPackTokenBudget": {
                "title": "ContextPackTokenBudget",
                "description": "A bounded, strictly positive token budget a caller asks a pack to be built against. Zero is excluded rather than merely discouraged: a pack built against no budget at all can carry no content, so a zero budget states a request no build could usefully answer.",
                "type": "integer",
                "minimum": 1,
                "maximum": 10000000,
            },
            "memory__GovernedRecordType": {
                "title": "GovernedRecordType",
                "description": "Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact` or `memory.entity` or `memory.relation`. Open by design so a compatible minor release can add record types without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__MemoryQuery": {
                "title": "MemoryQuery",
                "description": "A caller-supplied, normalized search query for `memory.search`. Normalization (case-folding, whitespace, tokenization) is caller-side; this document defines no normalization algorithm.",
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
            },
            "memory__RecordDomainScope": {
                "title": "RecordDomainScope",
                "description": "Open, bounded, non-empty, dot-namespaced record classification stating what domain a governed record belongs to, such as `personal.preferences` or `project.roadmap`. Distinct from the caller-authorization `Scope` vocabulary (e.g. `memory:read`): a domain scope never grants or checks a permission, it only classifies what the record is about. Open by design so a compatible minor release can add classifications without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
        },
    },
    "https://contracts.omnivia.dev/application/v1/context-pack.schema.json#/$defs/ContextPackBuildResult": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ContextPackBuildResult",
        "description": "Result of `context_pack.build`: the original query, the model-facing sections, the selected L0 evidence, current canonical L2 records, supporting history and L3 context models, the exact citations every section and selected item rests on, the conflicts and uncertainties the pack surfaces rather than resolving, the policy and budget omissions, token accounting, and the complete reproducibility record. Selecting and citing this content never grants new authority: `fresh_authorization_required` is always true, and possessing `pack_id` grants nothing on its own -- it is a content digest anyone can recompute, not a capability.",
        "type": "object",
        "properties": {
            "pack_id": {
                "$ref": "#/$defs/context_pack__ContextPackDigest",
                "description": "Content-addressed identity of this pack, exactly equal to `reproducibility.artifact_checksum`. Two builds that produce the same content produce the same identity, and a changed pack can never keep an old one.",
            },
            "mode": {
                "$ref": "#/$defs/context_pack__ContextPackMode",
                "description": "The mode this pack was built in, bound exactly to the request's mode.",
            },
            "query": {
                "$ref": "#/$defs/memory__MemoryQuery",
                "description": "The original caller query this pack was built for, bound exactly to the request's query.",
            },
            "sections": {
                "type": "array",
                "description": "The model-facing sections of this pack, ordered deterministically by `section_id`. May be empty for a valid pack that found nothing to say; every section that is present is non-empty and cited.",
                "items": {
                    "$ref": "#/$defs/context_pack__ContextPackSection",
                },
                "maxItems": 256,
            },
            "evidence": {
                "type": "array",
                "description": "Selected L0 evidence artifacts, ordered deterministically by identifier then content checksum. Every artifact is held to the complete resolution-time closure: `canonical_resolution_time` is an inclusive upper bound on every act and observation the artifact carries, not only on the instants this partition's own rule reads -- `temporal.event_at`, `observed_at`, `ingested_at` and `recorded_at`, `source.retrieved_at`, every `provenance_history[].occurred_at`, and every `provenance_history[].evidence[].source.retrieved_at`. Equality passes and only a strictly later instant is refused, and the refusal is a refusal rather than a repair: provenance is append-only, so an out-of-range instant is never dropped or truncated to make the artifact selectable. Its validity must contain the resolution instant on a half-open `[valid_from, valid_until)` window -- `temporal.valid_from` no later than it, inclusive of equality, and `temporal.valid_until` strictly later than it; a `valid_until` exactly at the resolution instant is refused, not accepted. `temporal.superseded_at` must be absent or strictly after the resolution instant: supersession is exclusive where the closure is inclusive, because an artifact replaced *at* the instant a pack resolved was already not the live one, so equality is rejected here rather than accepted.",
                "items": {
                    "$ref": "#/$defs/evidence__EvidenceArtifact",
                },
                "maxItems": 500,
            },
            "records": {
                "type": "array",
                "description": "Selected current, canonical L2 governed records, ordered deterministically by record identifier then version. Every record is held to the complete resolution-time closure: `canonical_resolution_time` is an inclusive upper bound on every act and observation nested anywhere in its provenance, not only on the instants this partition's own rule reads -- `provenance.temporal.event_at`, `observed_at`, `ingested_at` and `recorded_at`, every `provenance.sources[].retrieved_at`, every `provenance.history[].occurred_at`, every `provenance.history[].evidence[].source.retrieved_at`, `provenance.assertion.asserted_at`, every `provenance.assertion.evidence[].source.retrieved_at`, and `provenance.extraction.extracted_at`. Equality passes and only a strictly later instant is refused, and the refusal is a refusal rather than a repair. Its validity must contain the resolution instant on a half-open `[valid_from, valid_until)` window -- `provenance.temporal.valid_from` no later than it, inclusive of equality, and `provenance.temporal.valid_until` strictly later than it, so a `valid_until` exactly at the resolution instant is refused; a version whose validity begins only afterwards was not yet in force, and one whose validity ends at or before it was no longer the answer. The version must be current and unsuperseded at that instant: `currentness` exactly `current`, and `provenance.temporal.superseded_at` absent outright, irrespective of timestamp. Not merely absent at or before the resolution instant: a current version records no supersession at all, so a `superseded_at` strictly after the resolution instant is refused exactly as one at or before it is. A version that states when it was replaced belongs to `history`, whichever side of the resolution instant that statement falls on. `provenance.assertion.proposed_valid_from`/`proposed_valid_until` are deliberately not bounded by any of this -- a proposed effective date is a claim about the future rather than an act that had to have happened, and a record valid now may propose taking effect later.",
                "items": {
                    "$ref": "#/$defs/memory__GovernedRecord",
                },
                "maxItems": 500,
            },
            "history": {
                "type": "array",
                "description": "Selected historical canonical L2 governed record versions -- versions that were canonical knowledge and had already been superseded at the canonical-resolution time -- ordered deterministically by record identifier then version. May be empty. Every version is held to the same complete resolution-time closure `records` states, over exactly the same nested provenance paths, inclusive at equality and refused rather than repaired past it. `provenance.temporal.superseded_at` is required and must be at or before the resolution instant, with equality *accepted*: a version replaced exactly at the instant a pack resolved was already history by it, and one superseded only afterwards was still canonical then and is not this partition's to carry. That is the mirror image of the `evidence` rule, where equality is rejected, and the two differ because they are asking opposite questions about the same boundary. Validity containment is deliberately *not* required here: a historical version's validity window may have closed long before the resolution instant -- that is what makes it historical -- so demanding containment would empty the partition of exactly the versions it exists to carry. Note also that `recorded_at <= superseded_at <= resolution` already follows from the intrinsic record rules composed with the supersession bound above, so a future `ingested_at`/`recorded_at` on a historical version is refused by those before the nested closure is ever consulted.",
                "items": {
                    "$ref": "#/$defs/memory__GovernedRecord",
                },
                "maxItems": 500,
            },
            "context_models": {
                "type": "array",
                "description": "Selected current, canonical L3 context-model governed records, ordered deterministically by record identifier then version. May be empty. Held to exactly the same current rules `records` states, at L3 rather than L2: the same complete resolution-time closure over the same nested provenance paths, inclusive at equality and refused rather than repaired past it; the same half-open validity containment of the resolution instant -- `valid_from` inclusive, `valid_until` exclusive; and the same requirement to be current and unsuperseded at that instant, with `provenance.temporal.superseded_at` absent outright, irrespective of timestamp -- a value strictly after the resolution instant is refused exactly as one at or before it is. Only the governance layer differs -- `layer` exactly `l3` -- and a context model is otherwise no more selectable than an L2 record would be under the same temporal facts.",
                "items": {
                    "$ref": "#/$defs/memory__GovernedRecord",
                },
                "maxItems": 500,
            },
            "citations": {
                "type": "array",
                "description": "Exact citations binding this pack's sections and selected content to immutable evidence or governed record versions, ordered deterministically by `citation_id`. May be empty only when this pack selected nothing and states no section.",
                "items": {
                    "$ref": "#/$defs/context_pack__ContextPackCitation",
                },
                "maxItems": 2000,
            },
            "conflicts": {
                "type": "array",
                "description": "Conflicts among cited content this pack surfaces rather than silently resolving, in deterministic order. May be empty.",
                "items": {
                    "$ref": "#/$defs/context_pack__ContextPackConflict",
                },
                "maxItems": 256,
            },
            "uncertainties": {
                "type": "array",
                "description": "Uncertainties this pack surfaces rather than silently resolving, in deterministic order. May be empty.",
                "items": {
                    "$ref": "#/$defs/context_pack__ContextPackUncertainty",
                },
                "maxItems": 256,
            },
            "omissions": {
                "type": "array",
                "description": "Policy or budget reasons content the caller might otherwise expect was left out of this pack, in deterministic order. May be empty.",
                "items": {
                    "$ref": "#/$defs/common__Omission",
                },
                "maxItems": 256,
            },
            "budget": {
                "$ref": "#/$defs/context_pack__ContextPackBudget",
                "description": "Token budget accounting for this pack.",
            },
            "reproducibility": {
                "$ref": "#/$defs/context_pack__ContextPackReproducibility",
                "description": "Everything a second build needs to reproduce this pack byte for byte, including the checksum that makes it content-addressed.",
            },
            "fresh_authorization_required": {
                "type": "boolean",
                "description": "Always true: following any citation in this pack always requires fresh authorization against the cited evidence or record. Possessing this pack, or `pack_id`, grants no access on its own.",
            },
        },
        "required": [
            "pack_id",
            "mode",
            "query",
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
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "common__CapabilityId": {
                "title": "CapabilityId",
                "description": "Stable namespaced capability identifier such as `memory.read`. At least one dot is required so capability names always carry a namespace.",
                "type": "string",
                "minLength": 3,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*(?:\\.[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*)+$(?![\\s\\S])",
            },
            "common__ContractVersion": {
                "title": "ContractVersion",
                "description": "A `major.minor` contract version. Major changes are breaking; minor changes are additive and forward compatible.",
                "type": "string",
                "pattern": "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$(?![\\s\\S])",
                "maxLength": 32,
            },
            "common__GrantedAuthority": {
                "title": "GrantedAuthority",
                "description": "Server-produced, validated authority actually applied to a request. This is the only authority statement a client may trust.",
                "type": "object",
                "properties": {
                    "principal_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Validated principal the operation executed as.",
                    },
                    "roles": {
                        "type": "array",
                        "description": "Validated roles held by the principal.",
                        "items": {
                            "$ref": "#/$defs/common__Identifier",
                        },
                        "maxItems": 64,
                    },
                    "capabilities": {
                        "type": "array",
                        "description": "Capability references actually granted for this request.",
                        "items": {
                            "$ref": "#/$defs/compatibility__CapabilityRef",
                        },
                        "maxItems": 256,
                    },
                },
                "required": [
                    "principal_id",
                    "roles",
                    "capabilities",
                ],
                "unevaluatedProperties": False,
            },
            "common__Identifier": {
                "title": "Identifier",
                "description": "Generic bounded, non-empty identifier used for clients, principals, roles, and deprecations.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "common__JsonObject": {
                "title": "JsonObject",
                "description": "An opaque JSON object. The envelope carries domain payloads without inspecting them, which is a statement about the envelope rather than about the payload: an operation's `input` and `result` are each bound to their own definition by `operations.schema.json`'s `x-omnivia-operation-catalogue` (`input_schema_ref` and `result_schema_ref`), and validating a payload against that binding is a separate step from decoding the envelope carrying it.",
                "type": "object",
            },
            "common__Omission": {
                "title": "Omission",
                "description": "A statement that something the caller asked for was deliberately not returned.",
                "type": "object",
                "properties": {
                    "code": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open omission code.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional JSON Pointer into the result identifying what was omitted.",
                        "maxLength": 1024,
                    },
                    "message": {
                        "type": "string",
                        "description": "Human-readable explanation. Not a stable interface.",
                        "maxLength": 2048,
                    },
                },
                "required": [
                    "code",
                ],
                "unevaluatedProperties": False,
            },
            "common__OpaqueToken": {
                "title": "OpaqueToken",
                "description": "A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never parse it. The pattern's trailing negative lookahead is an end-of-input assertion, not a widening of the character domain: a bare `$` matches before a final line terminator in some conforming regex engines, so a token spelled with a trailing newline would be schema-valid while the semantic validators -- which match the whole string -- refuse it. The lookahead pins the anchor to absolute end of input, so strict schema and semantic validation accept exactly the same tokens.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__OpenCode": {
                "title": "OpenCode",
                "description": "An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible minor releases can add vocabulary; consumers must preserve values they do not recognize.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "common__ProjectionFreshness": {
                "title": "ProjectionFreshness",
                "description": "Staleness statement for reads served from a projection rather than the write model. Every projection this read was served from must be named in both `projection_versions` and `projection_watermarks`: the two maps are one statement about the same set of projections, so their key sets are required to be identical and neither may be empty. A read served from no named projection cannot state its own staleness, and a projection that states a version but no watermark (or the reverse) leaves the caller unable to tell how far behind the write model it actually is.",
                "type": "object",
                "properties": {
                    "as_of": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Point in time the projection reflects.",
                    },
                    "projection_versions": {
                        "type": "object",
                        "description": "Open map of projection name to the opaque projection version this read was actually served from. Projection names are an open vocabulary, so new projections may appear in compatible minor releases; at least one must be named, and the key set must equal `projection_watermarks`'.",
                        "minProperties": 1,
                        "propertyNames": {
                            "$ref": "#/$defs/common__OpenCode",
                        },
                        "additionalProperties": {
                            "$ref": "#/$defs/common__ProjectionVersion",
                        },
                    },
                    "projection_watermarks": {
                        "type": "object",
                        "description": "Open map of projection name to the opaque write-model version each projection has consumed up to -- how far the projection has caught up, as opposed to which version served this read. Keyed by exactly the same projection names as `projection_versions`; at least one must be named.",
                        "minProperties": 1,
                        "propertyNames": {
                            "$ref": "#/$defs/common__OpenCode",
                        },
                        "additionalProperties": {
                            "$ref": "#/$defs/common__ProjectionVersion",
                        },
                    },
                    "stale": {
                        "type": "boolean",
                        "description": "True when the server knows the projection lags the write model.",
                    },
                },
                "required": [
                    "as_of",
                    "projection_versions",
                    "projection_watermarks",
                    "stale",
                ],
                "unevaluatedProperties": False,
            },
            "common__ProjectionVersion": {
                "title": "ProjectionVersion",
                "description": "An opaque per-projection version marker used to reason about read staleness.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__Purpose": {
                "title": "Purpose",
                "description": "An open purpose-limitation token stating why the caller is making this request.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "common__Scope": {
                "title": "Scope",
                "description": "An open scope token such as `memory:read` requested by the caller. Scopes narrow a request; they never widen granted authority.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:[.:][a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "common__Timestamp": {
                "title": "Timestamp",
                "description": "An RFC 3339 timestamp in UTC with a literal `Z` offset.",
                "type": "string",
                "format": "date-time",
                "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$(?![\\s\\S])",
                "maxLength": 40,
            },
            "common__WorkspaceId": {
                "title": "WorkspaceId",
                "description": "Bounded, non-empty identifier of the workspace a request is scoped to.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "compatibility__CapabilityRef": {
                "title": "CapabilityRef",
                "description": "A concrete capability at a concrete version.",
                "type": "object",
                "properties": {
                    "id": {
                        "$ref": "#/$defs/common__CapabilityId",
                        "description": "Stable namespaced capability identifier.",
                    },
                    "version": {
                        "$ref": "#/$defs/common__ContractVersion",
                        "description": "Capability contract version.",
                    },
                },
                "required": [
                    "id",
                    "version",
                ],
                "unevaluatedProperties": False,
            },
            "context_pack__ContextPackAuthorizationContext": {
                "title": "ContextPackAuthorizationContext",
                "description": "The complete authority context one Context Pack was produced under, recorded so the build can be reproduced and audited. Historical reproducibility context only, and never a live grant: possessing it authorizes nothing, and following any citation still requires fresh authorization against the cited evidence or record. Recorded structurally rather than as an opaque fingerprint so a reviewer can actually check which principal, roles, capabilities, scopes, purpose, and policy versions were in force, instead of comparing two hashes and learning only that they differ.",
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "$ref": "#/$defs/common__WorkspaceId",
                        "description": "The workspace this pack was produced for.",
                    },
                    "authority": {
                        "$ref": "#/$defs/common__GrantedAuthority",
                        "description": "The validated principal, roles, and capabilities the build actually executed under. A historical record of that authority, not a restatement of it that a later reader may act on.",
                    },
                    "scopes": {
                        "type": "array",
                        "description": "The scopes in force for the build, in deterministic ascending order and free of duplicates. Never empty: a build that names no scope states nothing checkable about what narrowed it. Scopes narrow a request; recording them never widens what this artifact permits.",
                        "items": {
                            "$ref": "#/$defs/common__Scope",
                        },
                        "minItems": 1,
                        "maxItems": 64,
                        "uniqueItems": True,
                    },
                    "purpose": {
                        "$ref": "#/$defs/common__Purpose",
                        "description": "The purpose-limitation token the build was performed under.",
                    },
                    "policy_versions": {
                        "type": "object",
                        "description": "Open map of policy or ACL name to the opaque policy version applied while producing this pack. At least one policy must be named: a pack that states no policy version states nothing checkable about what filtered it.",
                        "minProperties": 1,
                        "propertyNames": {
                            "$ref": "#/$defs/common__OpenCode",
                        },
                        "additionalProperties": {
                            "$ref": "#/$defs/common__OpaqueToken",
                        },
                    },
                    "pre_ranking_authorization_enforced": {
                        "type": "boolean",
                        "description": "Always true: ACL and sensitivity authorization were applied to the candidate set before ranking and selection, not merely afterwards. An attestation about how the build ran, never itself a grant, and never an implication that filtering after ranking would have sufficed.",
                    },
                    "authorized_candidate_set_checksum": {
                        "$ref": "#/$defs/context_pack__ContextPackDigest",
                        "description": "Digest of the authorized candidate set that ranking and selection actually ran over, so a reproduction can prove it started from the same authorized material rather than a wider one. Independently recomputable rather than merely stated: it is exactly the digest `ContextPackAuthorizedCandidateSetManifest` defines, over the complete authorized frontier frozen before the first ranking, reranking, selection, or budget decision. A verifier checks it by recomputing that digest from a manifest supplied out of band and comparing; reading this value back out of the pack and comparing it with itself verifies nothing.",
                    },
                },
                "required": [
                    "workspace_id",
                    "authority",
                    "scopes",
                    "purpose",
                    "policy_versions",
                    "pre_ranking_authorization_enforced",
                    "authorized_candidate_set_checksum",
                ],
                "unevaluatedProperties": False,
            },
            "context_pack__ContextPackBudget": {
                "title": "ContextPackBudget",
                "description": "Token budget accounting for one Context Pack: the positive budget it was built against, and the non-negative amount its sections actually consumed.",
                "type": "object",
                "properties": {
                    "token_budget": {
                        "$ref": "#/$defs/context_pack__ContextPackTokenBudget",
                        "description": "The token budget this pack was built against, exactly as requested.",
                    },
                    "tokens_used": {
                        "$ref": "#/$defs/context_pack__ContextPackTokenCount",
                        "description": "Tokens actually consumed, exactly the sum of every section's `token_count`.",
                    },
                },
                "required": [
                    "token_budget",
                    "tokens_used",
                ],
                "unevaluatedProperties": False,
            },
            "context_pack__ContextPackCitation": {
                "title": "ContextPackCitation",
                "description": "One exact citation in a pack: either an evidence citation or a governed-record citation, never both and never neither. The two branches are distinct object shapes rather than one shape with two optional pointers, so what a citation points at is settled structurally by the wire document instead of being left to a semantic agreement check.",
                "oneOf": [
                    {
                        "$ref": "#/$defs/context_pack__ContextPackEvidenceCitation",
                    },
                    {
                        "$ref": "#/$defs/context_pack__ContextPackRecordCitation",
                    },
                ],
            },
            "context_pack__ContextPackConflict": {
                "title": "ContextPackConflict",
                "description": "A stated conflict between two or more citations this pack returned, which the pack surfaces rather than resolving on the caller's behalf. Stated in citation identifiers rather than record references so evidence and governed records are addressed by the one reference system this pack already publishes, instead of a second, competing one that could name something the pack never cited.",
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Human-readable statement of the conflict. Not a stable interface.",
                        "minLength": 1,
                        "maxLength": 4096,
                    },
                    "conflicting_citation_ids": {
                        "type": "array",
                        "description": "The citations that conflict with one another, by `citation_id`, in deterministic ascending order and free of duplicates. At least two, since a conflict needs two sides, and every identifier must resolve to a citation this pack actually returned.",
                        "items": {
                            "$ref": "#/$defs/common__Identifier",
                        },
                        "minItems": 2,
                        "maxItems": 64,
                        "uniqueItems": True,
                    },
                },
                "required": [
                    "description",
                    "conflicting_citation_ids",
                ],
                "unevaluatedProperties": False,
            },
            "context_pack__ContextPackDigest": {
                "title": "ContextPackDigest",
                "description": "A SHA-256 content digest, spelled `sha256:` followed by exactly 64 lowercase hexadecimal characters. Deliberately narrower than the general `EvidenceChecksum`: this is not an opaque server token a client round-trips but a value an independent implementation must be able to recompute and compare byte for byte, so exactly one algorithm, one length, and one letter case are admitted.",
                "type": "string",
                "minLength": 71,
                "maxLength": 71,
                "pattern": "^sha256:[0-9a-f]{64}$(?![\\s\\S])",
            },
            "context_pack__ContextPackEvidenceCitation": {
                "title": "ContextPackEvidenceCitation",
                "description": "One citation binding a pack section to an exact L0 evidence artifact, optionally at a precise location inside it. Possessing this citation grants no access on its own: following it always requires fresh authorization against the cited evidence.",
                "type": "object",
                "properties": {
                    "citation_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of this citation within this pack, unique across `citations` and referenced by section, conflict, and uncertainty citation-id lists.",
                    },
                    "evidence_reference": {
                        "$ref": "#/$defs/context_pack__ContextPackEvidenceReference",
                        "description": "The exact evidence artifact and content checksum this citation points at.",
                    },
                    "content_pointer": {
                        "type": "string",
                        "description": "Optional bounded, opaque locator within the cited artifact's content, such as a JSON Pointer. v1 states its length bound and nothing about its syntax: resolving a locator is target-specific and is not guessed at by this provider-neutral layer.",
                        "maxLength": 2048,
                    },
                    "source_span": {
                        "$ref": "#/$defs/records__SourceSpan",
                        "description": "Optional addressable position within the cited artifact, reusing the same structural and semantic rules `records.SourceSpan` already carries rather than restating them as an unvalidated string.",
                    },
                    "excerpt": {
                        "type": "string",
                        "description": "Optional short excerpt substantiating what this citation supports. Not a stable interface.",
                        "maxLength": 4096,
                    },
                },
                "required": [
                    "citation_id",
                    "evidence_reference",
                ],
                "unevaluatedProperties": False,
            },
            "context_pack__ContextPackEvidenceReference": {
                "title": "ContextPackEvidenceReference",
                "description": "A precise pointer to one exact L0 evidence artifact: which artifact, and the content checksum that artifact carried. Both are required, so the pointer names a specific immutable content state rather than whatever the identifier resolves to later. Distinct from `records.EvidenceReference`, which points at a source a record drew on rather than at a captured L0 artifact.",
                "type": "object",
                "properties": {
                    "evidence_id": {
                        "$ref": "#/$defs/evidence__EvidenceId",
                        "description": "Identifier of the referenced L0 evidence artifact.",
                    },
                    "content_checksum": {
                        "$ref": "#/$defs/evidence__EvidenceChecksum",
                        "description": "The exact content checksum the referenced artifact carried, so this reference names one immutable content state rather than an identifier whose content may since have been recaptured.",
                    },
                },
                "required": [
                    "evidence_id",
                    "content_checksum",
                ],
                "unevaluatedProperties": False,
            },
            "context_pack__ContextPackMode": {
                "title": "ContextPackMode",
                "description": "Open, dot-namespaced code naming how a Context Pack was produced. Wire-open by shape so a compatible minor release can add vocabulary, but trust-sensitive: v1 recognizes exactly one value, `deterministic_view` (a regenerated, non-persisted, deterministic view), and semantic validation fails closed on every other value rather than guessing at it. `immutable_snapshot` is deliberately not a v1 mode -- persistence is an operation posture this read does not have -- and `returned_artifact` was never a wire mode at all.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "context_pack__ContextPackNormalizedRequest": {
                "title": "ContextPackNormalizedRequest",
                "description": "The exact normalized request one Context Pack was built from: the server-produced normalized query and the version of the normalization that produced it, the mode, the resolved record view, the token budget, and any selection filters. The single normalized form of a request -- the original caller query stays on the result's own `query` field, and nothing else restates it. Query normalization itself is server-owned and versioned: this contract requires the normalized query to be non-empty and pins which normalization produced it, and deliberately specifies no normalization algorithm of its own.",
                "type": "object",
                "properties": {
                    "normalized_query": {
                        "type": "string",
                        "description": "The normalized form of the caller's query that this build actually ran. Never empty: a build that normalized a query to nothing has no request left to reproduce.",
                        "minLength": 1,
                        "maxLength": 4096,
                    },
                    "mode": {
                        "$ref": "#/$defs/context_pack__ContextPackMode",
                        "description": "The mode this build ran in, bound exactly to the validated request's mode.",
                    },
                    "view": {
                        "$ref": "#/$defs/memory__GovernedRecordView",
                        "description": "The governed-record view the build resolved to. In v1 this is always `current_canonical`: a Context Pack selects current canonical knowledge plus the history and context models that support it, and the request carries no view selector that could widen that.",
                    },
                    "token_budget": {
                        "$ref": "#/$defs/context_pack__ContextPackTokenBudget",
                        "description": "The token budget this build ran against, bound exactly to the validated request's budget.",
                    },
                    "normalization_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the query normalization that produced `normalized_query`. Without it the normalized query is unreproducible, since a later normalization of the same caller query may differ.",
                    },
                    "domain_scope": {
                        "$ref": "#/$defs/memory__RecordDomainScope",
                        "description": "The domain-scope filter the build applied, present exactly when the request carried one.",
                    },
                    "record_type": {
                        "$ref": "#/$defs/memory__GovernedRecordType",
                        "description": "The record-type filter the build applied, present exactly when the request carried one.",
                    },
                },
                "required": [
                    "normalized_query",
                    "mode",
                    "view",
                    "token_budget",
                    "normalization_version",
                ],
                "unevaluatedProperties": False,
            },
            "context_pack__ContextPackRecordCitation": {
                "title": "ContextPackRecordCitation",
                "description": "One citation binding a pack section to an exact governed record version, optionally at a precise location inside it. Possessing this citation grants no access on its own: following it always requires fresh authorization against the cited record.",
                "type": "object",
                "properties": {
                    "citation_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of this citation within this pack, unique across `citations` and referenced by section, conflict, and uncertainty citation-id lists.",
                    },
                    "record_reference": {
                        "$ref": "#/$defs/records__RecordVersionReference",
                        "description": "The exact governed record version this citation points at.",
                    },
                    "content_pointer": {
                        "type": "string",
                        "description": "Optional bounded, opaque locator within the cited record's opaque content, such as a JSON Pointer. v1 states its length bound and nothing about its syntax: resolving a locator is target-specific and is not guessed at by this provider-neutral layer.",
                        "maxLength": 2048,
                    },
                    "source_span": {
                        "$ref": "#/$defs/records__SourceSpan",
                        "description": "Optional addressable position within the cited record, reusing the same structural and semantic rules `records.SourceSpan` already carries rather than restating them as an unvalidated string.",
                    },
                    "excerpt": {
                        "type": "string",
                        "description": "Optional short excerpt substantiating what this citation supports. Not a stable interface.",
                        "maxLength": 4096,
                    },
                },
                "required": [
                    "citation_id",
                    "record_reference",
                ],
                "unevaluatedProperties": False,
            },
            "context_pack__ContextPackReproducibility": {
                "title": "ContextPackReproducibility",
                "description": "Everything a second build needs to reproduce one Context Pack byte for byte: the pack format version, the builder, the normalized request, the authority context the build ran under, the exact evidence and record versions selected, the projection the read was served from, the retrieval/ranking/reranking/selection/tokenizer/summarizer/model versions applied, the instant the canonical knowledge was resolved at, and the canonicalization and checksum that make the result content-addressed. With every one of these unchanged, rebuilding must reproduce the identical pack. Carries no audit reference: the response envelope owns audit linkage, and folding a per-request audit identifier into a content-addressed artifact would make two identical builds hash differently.",
                "type": "object",
                "properties": {
                    "pack_format_version": {
                        "$ref": "#/$defs/common__ContractVersion",
                        "description": "The Context Pack artifact format this pack is written in. Frozen at `1.0` for v1: the checksum rule below is defined against exactly this format, so a reader must know which format it is verifying before it verifies anything. Format `1.0` also pins the numeric admission profile the canonicalization runs under -- lossless binary64. Every number in an admitted pack must be a finite IEEE 754 binary64 value, and an integer is admitted only when it converts to binary64 without loss, so `2**53 + 1` is refused rather than silently signed under the name of `2**53`; non-finite values (NaN, the infinities) have no JSON form and are refused outright. This belongs to the format rather than to the checksum because it decides *which documents have a canonical form at all*: two implementations that agreed on the hash but disagreed on whether a given number was admissible would not agree on which packs exist. A future format may widen or narrow that profile, which is exactly why the format is named inside the artifact and read before anything is verified.",
                    },
                    "builder_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the pack builder that assembled this artifact.",
                    },
                    "normalized_request": {
                        "$ref": "#/$defs/context_pack__ContextPackNormalizedRequest",
                        "description": "The exact normalized request this pack was built from.",
                    },
                    "authorization_context": {
                        "$ref": "#/$defs/context_pack__ContextPackAuthorizationContext",
                        "description": "The complete authority context the build ran under. Historical reproducibility context, never a live grant.",
                    },
                    "evidence_versions": {
                        "type": "array",
                        "description": "Exactly the L0 evidence identities this pack selected, in deterministic ascending order by identifier then checksum, with no duplicate, addition, or omission. Not a superset of what was selected and not a summary of it: the set itself, so a reproduction can be checked rather than believed.",
                        "items": {
                            "$ref": "#/$defs/context_pack__ContextPackEvidenceReference",
                        },
                        "maxItems": 500,
                    },
                    "record_versions": {
                        "type": "array",
                        "description": "Exactly the union of the governed-record identities this pack selected across `records`, `history`, and `context_models`, in deterministic ascending order by record identifier then version, with no duplicate, addition, or omission.",
                        "items": {
                            "$ref": "#/$defs/records__RecordVersionReference",
                        },
                        "maxItems": 1500,
                    },
                    "freshness": {
                        "$ref": "#/$defs/common__ProjectionFreshness",
                        "description": "The projection versions and watermarks this pack was actually served from, under the same strict rule every other projection-served read in this contract states.",
                    },
                    "retrieval_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the retrieval configuration that produced the candidate set.",
                    },
                    "ranking_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the ranking configuration applied to the authorized candidate set.",
                    },
                    "reranking_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the reranking configuration applied after ranking.",
                    },
                    "selection_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the selection configuration that chose what fit the budget.",
                    },
                    "tokenizer_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the tokenizer every `token_count` in this pack was measured with.",
                    },
                    "tokenizer_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of that tokenizer. Token counts are only reproducible against an exact tokenizer identity and version.",
                    },
                    "summarizer_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the summarizer applied while producing this pack, or the literal `disabled` when none was used. Required either way: an absent field would leave a reader unable to tell a build that summarized nothing from one whose summarizer was simply never recorded.",
                    },
                    "model_versions": {
                        "type": "object",
                        "description": "Open map of model role to the exact model version used in that role. Required but allowed to be empty, which is how a build that used no model at all states so explicitly rather than by omission.",
                        "propertyNames": {
                            "$ref": "#/$defs/common__OpenCode",
                        },
                        "additionalProperties": {
                            "$ref": "#/$defs/common__Identifier",
                        },
                    },
                    "canonical_resolution_time": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "The instant the canonical knowledge in this pack was resolved at. Every selected item is judged current, historical, valid, or superseded against exactly this instant, and it is additionally an inclusive upper bound on every event and provenance instant a selected item carries: equality passes, strictly later is refused. For a selected `evidence.EvidenceArtifact` that covers `temporal.event_at`, `observed_at`, `ingested_at`, and `recorded_at`, `source.retrieved_at`, every `provenance_history[].occurred_at`, and every `provenance_history[].evidence[].source.retrieved_at`. For a selected `memory.GovernedRecord` in `records`, `history`, or `context_models` it covers `provenance.temporal.event_at`, `observed_at`, `ingested_at`, and `recorded_at`, every `provenance.sources[].retrieved_at`, every `provenance.history[].occurred_at`, every `provenance.history[].evidence[].source.retrieved_at`, `provenance.assertion.asserted_at`, every `provenance.assertion.evidence[].source.retrieved_at`, and `provenance.extraction.extracted_at`. It deliberately does not bound `provenance.assertion.proposed_valid_from` or `proposed_valid_until`: those are proposed effective dates, and an assertion about the future is a claim rather than an act that had not happened. This bound applies to selection into a pack only; the generic record and evidence rules are unchanged, and an out-of-range instant is a refusal to select, never a repair -- provenance is append-only, so nothing is dropped or truncated to make an item selectable.",
                    },
                    "generated_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this pack was generated. Equal to `canonical_resolution_time`: a deterministic build is logically complete at the instant it resolved at, and letting wall-clock generation time drift from it would make two otherwise identical builds hash differently.",
                    },
                    "artifact_canonicalization": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the canonicalization the checksum below was computed over. Frozen at `rfc8785` in v1: a checksum is only checkable against a stated, exactly specified canonical form, and an unrecognized value fails closed rather than being verified under a guessed one. Pack format 1.0 applies RFC 8785 byte serialization to an admitted I-JSON data model: every number must be a finite binary64 before canonicalization, and any number token written in integer form -- and any direct host-language integral value -- is admitted only when converting it to binary64 and back to the mathematical integer is exact. Decimal and exponent tokens are read as finite binary64 under ordinary JCS rules, with no requirement of an exact decimal rational representation. So `9007199254740992` and `1152921504606846976` are admitted while `9007199254740993` and `1152921504606846977` are refused, `1e400` is refused as non-finite, and `0.1` and `1e+21` are admitted. This is an admission rule, not a safe-integer range: exact larger integers such as powers of two stay valid. It refuses silent rounding of an integer identity or count without changing the RFC 8785 bytes any admitted document serializes to.",
                    },
                    "artifact_checksum": {
                        "$ref": "#/$defs/context_pack__ContextPackDigest",
                        "description": "SHA-256 of the RFC 8785 canonical UTF-8 bytes of this complete result with exactly two members removed: the result's own `pack_id` and this field. Nothing else is excluded -- not the generation time, the authority context, the freshness statement, the policy or configuration versions, the budget, the sections, the citations, or any selected content -- so any change to any of them changes the digest. What is hashed is the *admitted v1 data model's* RFC 8785 canonical UTF-8 bytes, not the bytes as they happened to arrive: the document is first admitted under the numeric profile `pack_format_version` pins and the rest of the v1 data model (object member names are strings and no member name is duplicated, strings are valid Unicode scalar sequences with no lone surrogate), and only then canonicalized and hashed. So received whitespace, member order, number spelling, and optional string escaping do not reach the digest -- RFC 8785 defines them away -- while a document that is not admissible at all has no digest rather than a digest of some repaired version of itself. Member names are ordered by unsigned UTF-16 code unit and numbers rendered by ECMAScript `Number::toString`, both as RFC 8785 requires.",
                    },
                },
                "required": [
                    "pack_format_version",
                    "builder_version",
                    "normalized_request",
                    "authorization_context",
                    "evidence_versions",
                    "record_versions",
                    "freshness",
                    "retrieval_version",
                    "ranking_version",
                    "reranking_version",
                    "selection_version",
                    "tokenizer_id",
                    "tokenizer_version",
                    "summarizer_version",
                    "model_versions",
                    "canonical_resolution_time",
                    "generated_at",
                    "artifact_canonicalization",
                    "artifact_checksum",
                ],
                "unevaluatedProperties": False,
            },
            "context_pack__ContextPackSection": {
                "title": "ContextPackSection",
                "description": "One model-facing section of a Context Pack: its identity, what kind of section it is, its content, the citations that content rests on, and the tokens that content occupies. Every section is substantiated: `citation_ids` is never empty, so no part of a pack's model-facing content is unattributable.",
                "type": "object",
                "properties": {
                    "section_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of this section within this pack, unique across `sections`. Section identifiers and citation identifiers are independent namespaces; the same string may appear in both.",
                    },
                    "kind": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming what kind of section this is, such as `summary` or `evidence_digest`. Open by design; an unrecognized kind must be preserved, not coerced.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional human-readable heading for this section. Not a stable interface, and not counted by `token_count`.",
                        "minLength": 1,
                        "maxLength": 256,
                    },
                    "content": {
                        "type": "string",
                        "description": "The exact model-facing content of this section. Never empty: a section that contributes no content contributes nothing a caller could act on.",
                        "minLength": 1,
                        "maxLength": 16384,
                    },
                    "citation_ids": {
                        "type": "array",
                        "description": "The citations this section's content rests on, by `citation_id`, in deterministic ascending order and free of duplicates. Never empty, and every identifier must resolve to a citation this pack actually returned.",
                        "items": {
                            "$ref": "#/$defs/common__Identifier",
                        },
                        "minItems": 1,
                        "maxItems": 256,
                        "uniqueItems": True,
                    },
                    "token_count": {
                        "$ref": "#/$defs/context_pack__ContextPackTokenCount",
                        "description": "Tokens the exact `content` string occupies under the tokenizer named by `reproducibility.tokenizer_id` at `reproducibility.tokenizer_version`. Covers `content` and nothing else: `title` and any excerpt carried by a cited citation are deliberately excluded, so a caller can reconcile this count against the string it actually sends to a model.",
                    },
                },
                "required": [
                    "section_id",
                    "kind",
                    "content",
                    "citation_ids",
                    "token_count",
                ],
                "unevaluatedProperties": False,
            },
            "context_pack__ContextPackTokenBudget": {
                "title": "ContextPackTokenBudget",
                "description": "A bounded, strictly positive token budget a caller asks a pack to be built against. Zero is excluded rather than merely discouraged: a pack built against no budget at all can carry no content, so a zero budget states a request no build could usefully answer.",
                "type": "integer",
                "minimum": 1,
                "maximum": 10000000,
            },
            "context_pack__ContextPackTokenCount": {
                "title": "ContextPackTokenCount",
                "description": "A bounded, non-negative count of tokens actually observed: the tokens one section's model-facing content occupies, or the total a whole pack consumed. Distinct from `ContextPackTokenBudget`, which is what a caller asked for: zero is a meaningful observation (an empty pack consumed nothing) but never a meaningful request.",
                "type": "integer",
                "minimum": 0,
                "maximum": 10000000,
            },
            "context_pack__ContextPackUncertainty": {
                "title": "ContextPackUncertainty",
                "description": "A stated uncertainty this pack surfaces rather than silently resolving or hiding, anchored to the citations it concerns.",
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Human-readable statement of the uncertainty. Not a stable interface.",
                        "minLength": 1,
                        "maxLength": 4096,
                    },
                    "related_citation_ids": {
                        "type": "array",
                        "description": "The citations this uncertainty concerns, by `citation_id`, in deterministic ascending order and free of duplicates. At least one: an uncertainty anchored to nothing this pack returned cannot be acted on or checked, and every identifier must resolve to a citation this pack actually returned.",
                        "items": {
                            "$ref": "#/$defs/common__Identifier",
                        },
                        "minItems": 1,
                        "maxItems": 64,
                        "uniqueItems": True,
                    },
                },
                "required": [
                    "description",
                    "related_citation_ids",
                ],
                "unevaluatedProperties": False,
            },
            "evidence__EvidenceArtifact": {
                "title": "EvidenceArtifact",
                "description": "One complete, append-preserving L0 evidence artifact: stable identity, workspace, exact source/native locator, applicable temporal instants, content checksum and media type, opaque metadata, permission/sensitivity labels, tombstone status, parser/ingestion status, and append-only provenance history. Carries no `GovernanceLayer`, `GovernanceState`, `RecordCurrentness`, or `authority_level` field: an evidence artifact is raw L0 material, never governed knowledge, and this shape must never be mistaken for a `GovernedRecord`.",
                "type": "object",
                "properties": {
                    "evidence_id": {
                        "$ref": "#/$defs/evidence__EvidenceId",
                        "description": "Stable identifier of this evidence artifact.",
                    },
                    "workspace_id": {
                        "$ref": "#/$defs/common__WorkspaceId",
                        "description": "Workspace this evidence artifact belongs to.",
                    },
                    "source": {
                        "$ref": "#/$defs/records__SourceReference",
                        "description": "Exact source, native identifier, and locator this evidence was captured from.",
                    },
                    "temporal": {
                        "$ref": "#/$defs/records__RecordTemporalMetadata",
                        "description": "The distinct instants applicable to this evidence: when observed, when ingested, and when this artifact was recorded, without collapsing them into one. `valid_from`/`valid_until`/`superseded_at` are rarely applicable to append-only L0 evidence and are typically absent.",
                    },
                    "content_checksum": {
                        "$ref": "#/$defs/evidence__EvidenceChecksum",
                        "description": "Checksum of this evidence artifact's content, proving the content has not been altered since capture.",
                    },
                    "media_type": {
                        "$ref": "#/$defs/evidence__MediaType",
                        "description": "Media type of this evidence artifact's content.",
                    },
                    "metadata": {
                        "$ref": "#/$defs/common__JsonObject",
                        "description": "Opaque metadata captured alongside this evidence artifact.",
                    },
                    "permission_labels": {
                        "type": "array",
                        "description": "Open codes naming the access/permission labels attached to this evidence artifact, such as `restricted` or `team_only`. May be empty when no label applies.",
                        "items": {
                            "$ref": "#/$defs/common__OpenCode",
                        },
                        "maxItems": 64,
                    },
                    "sensitivity": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming this evidence artifact's sensitivity classification, such as `public` or `confidential`.",
                    },
                    "tombstoned": {
                        "type": "boolean",
                        "description": "True when this evidence artifact has been tombstoned. A tombstoned artifact's append-only provenance history is never erased; tombstoning is itself recorded as a provenance entry.",
                    },
                    "parser_status": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the status of parsing this evidence artifact's content, such as `parsed` or `parse_failed`.",
                    },
                    "ingestion_status": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the status of ingesting this evidence artifact, such as `ingested` or `quarantined`.",
                    },
                    "provenance_history": {
                        "type": "array",
                        "description": "Append-only history of actions taken on this evidence artifact. Never truncated or rewritten; a correction is a new entry, not an edit to a prior one. Never empty: even the first capture of this artifact is itself a provenance entry, so an artifact with no recorded history entry would be an unaudited state.",
                        "items": {
                            "$ref": "#/$defs/records__ProvenanceEntry",
                        },
                        "minItems": 1,
                        "maxItems": 1024,
                    },
                    "import_run_id": {
                        "$ref": "#/$defs/common__OpaqueToken",
                        "description": "Names the import job/run that produced this evidence artifact, when it was produced by one. Typed as `OpaqueToken` because it is drawn from exactly the same opaque token domain as `JobIdentity.job_id` and `ImportCompletionResult.import_run_id`: the completion contract promises this backlink names the run that created this evidence, and spelled in any narrower vocabulary it could not hold every job id the contract admits -- a run such as `job/opaque-token` could complete and then have no writable backlink at all.",
                    },
                },
                "required": [
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
                ],
                "unevaluatedProperties": False,
            },
            "evidence__EvidenceChecksum": {
                "title": "EvidenceChecksum",
                "description": "A content checksum, spelled `algorithm:hex-digest` (such as `sha256:9f86d0...`) so the digest is never ambiguous about which algorithm produced it. Provider-neutral: this contract does not mandate a specific algorithm.",
                "type": "string",
                "minLength": 3,
                "maxLength": 256,
                "pattern": "^[a-z][a-z0-9_]*:[A-Za-z0-9+/=_-]+$(?![\\s\\S])",
            },
            "evidence__EvidenceId": {
                "title": "EvidenceId",
                "description": "Stable identifier of one L0 evidence artifact, constant across its append-only provenance history. Distinct from `RecordId`: an evidence artifact is never itself a governed record.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "evidence__MediaType": {
                "title": "MediaType",
                "description": "An IANA-style `type/subtype` media type string, such as `text/plain` or `application/json`.",
                "type": "string",
                "minLength": 3,
                "maxLength": 255,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$(?![\\s\\S])",
            },
            "memory__GovernedRecord": {
                "title": "GovernedRecord",
                "description": "A provider-neutral governed record: which workspace it belongs to, what kind of record it is, its domain scope and authority level, its full L0-L4 governance, temporal, evidence, and provenance envelope, and its opaque JSON content. Carries no reference to, and is not a substitute for, any repo-local `Memory`, `MemoryFact`, or `SourceRef` domain class.",
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "$ref": "#/$defs/common__WorkspaceId",
                        "description": "Workspace this record belongs to.",
                    },
                    "record_type": {
                        "$ref": "#/$defs/memory__GovernedRecordType",
                        "description": "What kind of governed record this is.",
                    },
                    "domain_scope": {
                        "$ref": "#/$defs/memory__RecordDomainScope",
                        "description": "Non-empty domain/record classification this record is filed under. Every governed record carries exactly one; a caller may propose one through `memory.create`, but the server is always the final authority on what is actually stored here. Distinct from caller-authorization `Scope`.",
                    },
                    "authority_level": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the authority level this record's governance decision currently carries, such as `proposed` or `reviewed` or `canonical`. Server-owned: no `memory.create` input field lets a caller assert this directly.",
                    },
                    "reviewer": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the reviewer or policy that produced this record's current governance decision, when one has been recorded. Absent when no reviewer/policy decision applies yet, such as a freshly proposed record.",
                    },
                    "provenance": {
                        "$ref": "#/$defs/records__RecordProvenance",
                        "description": "Identity, governance layer/state/currentness, temporal metadata, history, and evidence for this record version.",
                    },
                    "content": {
                        "$ref": "#/$defs/common__JsonObject",
                        "description": "Opaque governed content this record carries.",
                    },
                },
                "required": [
                    "workspace_id",
                    "record_type",
                    "domain_scope",
                    "authority_level",
                    "provenance",
                    "content",
                ],
                "unevaluatedProperties": False,
            },
            "memory__GovernedRecordType": {
                "title": "GovernedRecordType",
                "description": "Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact` or `memory.entity` or `memory.relation`. Open by design so a compatible minor release can add record types without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__GovernedRecordView": {
                "title": "GovernedRecordView",
                "description": "Open, dot-namespaced code selecting which slice of a governed record's versions a read considers: `current_canonical` (the single active accepted version, the default when this field is absent), `candidates` (proposed/candidate versions not yet accepted), or `history` (every version, including superseded ones). Open by design so a compatible minor release can add views without breaking existing decoders. Default resolution when absent is a semantic concern (see `omnivia_core.contracts.v1.semantics`), not a wire-shape one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__MemoryQuery": {
                "title": "MemoryQuery",
                "description": "A caller-supplied, normalized search query for `memory.search`. Normalization (case-folding, whitespace, tokenization) is caller-side; this document defines no normalization algorithm.",
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
            },
            "memory__RecordDomainScope": {
                "title": "RecordDomainScope",
                "description": "Open, bounded, non-empty, dot-namespaced record classification stating what domain a governed record belongs to, such as `personal.preferences` or `project.roadmap`. Distinct from the caller-authorization `Scope` vocabulary (e.g. `memory:read`): a domain scope never grants or checks a permission, it only classifies what the record is about. Open by design so a compatible minor release can add classifications without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__CandidateAssertion": {
                "title": "CandidateAssertion",
                "description": "Who is asserting a governed record's claim, when, and on what evidence, plus the validity window they propose for it. This is caller-supplied provenance for the claim -- carried into `memory.create` and `record.supersede` inputs, and preserved on the resulting record's `RecordProvenance` -- not the server-owned governance decision: it never carries authority level, reviewer/policy identity, or any other field a least-authority-escalating mutation input is forbidden from carrying. Defined here rather than in `memory.schema.json` so `RecordProvenance` can preserve it without `records.schema.json` depending on a document that already depends on it.",
                "type": "object",
                "properties": {
                    "actor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Principal or system asserting this candidate.",
                    },
                    "actor_kind": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.",
                    },
                    "actor_role": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the role the actor asserted this candidate under.",
                    },
                    "asserted_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the actor asserted this candidate.",
                    },
                    "proposed_valid_from": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Start of the validity window the caller proposes for this candidate, when known. The server remains the final authority on the validity window actually stored.",
                    },
                    "proposed_valid_until": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "End of the validity window the caller proposes for this candidate, when bounded. The server remains the final authority on the validity window actually stored.",
                    },
                    "evidence": {
                        "type": "array",
                        "description": "Concrete evidence substantiating this assertion. May be empty only when the enclosing input's `evidence_disposition` explicitly excuses it; enforcing that agreement is a semantic-validation concern, not a wire-shape one.",
                        "items": {
                            "$ref": "#/$defs/records__EvidenceReference",
                        },
                        "maxItems": 256,
                    },
                },
                "required": [
                    "actor_id",
                    "actor_kind",
                    "actor_role",
                    "asserted_at",
                    "evidence",
                ],
                "unevaluatedProperties": False,
            },
            "records__CandidateExtractionMetadata": {
                "title": "CandidateExtractionMetadata",
                "description": "Optional provenance about the automated extractor that produced a governed record's claim, when one did. Absent entirely for a claim a human asserted directly. Defined here rather than in `memory.schema.json` so `RecordProvenance` can preserve it without `records.schema.json` depending on a document that already depends on it.",
                "type": "object",
                "properties": {
                    "extractor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the extractor that produced this candidate.",
                    },
                    "extractor_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the extractor that produced this candidate, when known.",
                    },
                    "model_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the model the extractor used, when known.",
                    },
                    "prompt_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the prompt the extractor used, when known.",
                    },
                    "extracted_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the extractor produced this candidate.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "The extractor's self-reported confidence in this candidate, on a 0-1 scale, when known.",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "reconciliation_state": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming this candidate's reconciliation/deduplication state against prior extractions, such as `novel` or `duplicate` or `merged`, when the extractor determined one. Open by design; an unrecognized value must be preserved, not coerced to a known one, and must never widen this candidate's authority.",
                    },
                },
                "required": [
                    "extractor_id",
                    "extracted_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__EvidenceDisposition": {
                "title": "EvidenceDisposition",
                "description": "Open, dot-namespaced code stating whether concrete evidence is actually available for a record, such as `available` or `unavailable` or `redacted`. Open by design; an unrecognized value must be preserved, not coerced to a known one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__EvidenceReference": {
                "title": "EvidenceReference",
                "description": "A concrete piece of evidence supporting one claim in a record.",
                "type": "object",
                "properties": {
                    "source": {
                        "$ref": "#/$defs/records__SourceReference",
                        "description": "The source this evidence was drawn from.",
                    },
                    "span": {
                        "$ref": "#/$defs/records__SourceSpan",
                        "description": "Addressable position within the source this evidence was drawn from, when known.",
                    },
                    "excerpt": {
                        "type": "string",
                        "description": "Optional short excerpt from the source substantiating the claim. Not a stable interface.",
                        "maxLength": 4096,
                    },
                },
                "required": [
                    "source",
                ],
                "unevaluatedProperties": False,
            },
            "records__GovernanceLayer": {
                "title": "GovernanceLayer",
                "description": "Open, dot-namespaced code naming the knowledge-governance layer a record belongs to: `l0` (raw evidence), `l1` (candidate observations), `l2` (governed records / canonical knowledge), `l3` (context models), or `l4` (organisational model). Distinct from workspace scope, which is a caller-facing tenancy boundary, not a knowledge-governance layer. Open by design so a compatible minor release can add layers without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__GovernanceState": {
                "title": "GovernanceState",
                "description": "Open, dot-namespaced code naming a record's position in its own governance workflow, such as `proposed` or `candidate` or `accepted` or `rejected`. Distinct from `GovernanceLayer` (which namespace a record belongs to) and `RecordCurrentness` (whether this version is the active one): a record can be `accepted` and still later superseded, or `proposed` and never adopted. Open by design so a compatible minor release can add states without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__ProvenanceEntry": {
                "title": "ProvenanceEntry",
                "description": "One step in a record's history: who or what did what, when, and -- for a governance transition -- the explicit rationale it was taken under.",
                "type": "object",
                "properties": {
                    "actor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Principal or system that performed this action.",
                    },
                    "actor_kind": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.",
                    },
                    "action": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming what happened, such as `created` or `modified` or `superseded`.",
                    },
                    "occurred_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this action occurred.",
                    },
                    "reason_code": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming why this action was taken, carried over verbatim from the `GovernanceRationale.reason_code` the transition was requested under. Absent on ordinary non-governance history, which never carries a rationale; a governance-transition event must carry it, and requiring that is a semantic-validation concern, not a wire-shape one.",
                    },
                    "reason_comment": {
                        "type": "string",
                        "description": "Bounded human-readable elaboration, carried over verbatim from the requesting `GovernanceRationale.comment`. Absent exactly when that comment was absent. Not a stable interface.",
                        "maxLength": 2048,
                    },
                    "evidence": {
                        "type": "array",
                        "description": "Evidence supporting this action, when applicable. Bounded at the same 256 items `CandidateAssertion.evidence` is, and deliberately so: a `record.supersede` transition appends exactly one event whose evidence must equal the replacement claim's complete assertion evidence, so a lower bound here would make an otherwise valid replacement impossible to record.",
                        "items": {
                            "$ref": "#/$defs/records__EvidenceReference",
                        },
                        "maxItems": 256,
                    },
                },
                "required": [
                    "actor_id",
                    "actor_kind",
                    "action",
                    "occurred_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordCurrentness": {
                "title": "RecordCurrentness",
                "description": "Open, dot-namespaced code naming whether a record version is the active one, such as `current` or `superseded` or `retracted`. Open by design; an unrecognized value must be preserved, not coerced to a known one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__RecordId": {
                "title": "RecordId",
                "description": "Stable identifier of a governed record, constant across every version of that record.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "records__RecordIdentity": {
                "title": "RecordIdentity",
                "description": "The identity, version, governance layer, governance state, and currentness of one record version.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier stable across every version of this record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "Opaque version of this specific revision.",
                    },
                    "layer": {
                        "$ref": "#/$defs/records__GovernanceLayer",
                        "description": "Governance layer this record belongs to.",
                    },
                    "governance_state": {
                        "$ref": "#/$defs/records__GovernanceState",
                        "description": "This record version's position in its own governance workflow, independent of `layer` and `currentness`.",
                    },
                    "currentness": {
                        "$ref": "#/$defs/records__RecordCurrentness",
                        "description": "Whether this version is the active one.",
                    },
                    "supersedes": {
                        "$ref": "#/$defs/records__SupersessionReference",
                        "description": "The earlier record version this version replaces, when this version is itself the newer one.",
                    },
                    "superseded_by": {
                        "$ref": "#/$defs/records__SupersessionReference",
                        "description": "The newer record version that replaced this one, present when `currentness` marks this version as superseded.",
                    },
                },
                "required": [
                    "record_id",
                    "version",
                    "layer",
                    "governance_state",
                    "currentness",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordProvenance": {
                "title": "RecordProvenance",
                "description": "The full provenance envelope for one record version: identity, temporal metadata, its authoring history, the sources it draws on, and the caller-supplied assertion/extraction lineage the claim in this version came from. `assertion`/`extraction` are structurally optional so a record written before they existed still decodes, but a governance transition that replaces or carries forward a claim must bind them; enforcing that is a semantic-validation concern, not a wire-shape one.",
                "type": "object",
                "properties": {
                    "identity": {
                        "$ref": "#/$defs/records__RecordIdentity",
                        "description": "Identity, version, governance layer, and currentness of this record.",
                    },
                    "temporal": {
                        "$ref": "#/$defs/records__RecordTemporalMetadata",
                        "description": "Observed, ingested, recorded, and valid time for this record.",
                    },
                    "history": {
                        "type": "array",
                        "description": "Ordered, append-only history of actions that produced this record version. Deliberately carries no `maxItems`: history is never erased or rewritten, and every governance transition appends exactly one event, so any finite inline cap would eventually make a previously valid record impossible to transition -- and raising the cap only postpones that contradiction. Bounding a response's size is a transport/operation concern, handled outside this inline provenance invariant, never by dropping, compacting, or summarising audit history.",
                        "items": {
                            "$ref": "#/$defs/records__ProvenanceEntry",
                        },
                    },
                    "evidence_disposition": {
                        "$ref": "#/$defs/records__EvidenceDisposition",
                        "description": "Whether concrete evidence is actually available for this record. `sources` may be empty only when this disposition explicitly states evidence is unavailable; enforcing that agreement is a semantic-validation concern, not a wire-shape one.",
                    },
                    "sources": {
                        "type": "array",
                        "description": "Sources this record draws on, independent of any single history entry's evidence. May be empty only when `evidence_disposition` explicitly states evidence is unavailable.",
                        "items": {
                            "$ref": "#/$defs/records__SourceReference",
                        },
                        "maxItems": 256,
                    },
                    "assertion": {
                        "$ref": "#/$defs/records__CandidateAssertion",
                        "description": "Who asserted the claim this version carries, when, on what evidence, and the validity window they proposed. Preserved verbatim from the `memory.create` or `record.supersede` input that supplied the claim, so candidate/replacement lineage survives every governance transition.",
                    },
                    "extraction": {
                        "$ref": "#/$defs/records__CandidateExtractionMetadata",
                        "description": "Provenance of the automated extractor that produced the claim this version carries, when one did. Absent for a claim a human asserted directly.",
                    },
                },
                "required": [
                    "identity",
                    "temporal",
                    "history",
                    "evidence_disposition",
                    "sources",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordTemporalMetadata": {
                "title": "RecordTemporalMetadata",
                "description": "The distinct instants a governed record's lifecycle turns on: when the underlying fact occurred in the world, when it was observed, when the system ingested it, when this version was persisted, the window it is asserted valid for, and when it was superseded.",
                "type": "object",
                "properties": {
                    "event_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the underlying fact occurred in the world (source/event time), when distinguishable from `observed_at`.",
                    },
                    "observed_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the underlying fact was observed to be true in the world, when known.",
                    },
                    "ingested_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the system first ingested the fact behind this record.",
                    },
                    "recorded_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this specific version was persisted.",
                    },
                    "valid_from": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Start of the window this record is asserted valid for, when bounded.",
                    },
                    "valid_until": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "End of the window this record is asserted valid for, when bounded.",
                    },
                    "superseded_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this record version was superseded by a newer version, present only once superseded.",
                    },
                },
                "required": [
                    "ingested_at",
                    "recorded_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordVersion": {
                "title": "RecordVersion",
                "description": "Opaque, server-issued version marker of one specific revision of a record. Clients must round-trip it verbatim and must never parse it.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "records__RecordVersionReference": {
                "title": "RecordVersionReference",
                "description": "A precise, non-directional pointer to one exact record version: both `record_id` and `version` are always required. Distinct from `SupersessionReference`, which is direction-bearing (its meaning comes from which `RecordIdentity` field carries it) and whose `version` is optional. Used wherever a payload must name a specific existing record version -- a graph traversal start point or edge endpoint, a context pack citation or source-version list -- without asserting any supersession relationship.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier of the referenced record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "The exact referenced version.",
                    },
                },
                "required": [
                    "record_id",
                    "version",
                ],
                "unevaluatedProperties": False,
            },
            "records__SourceKind": {
                "title": "SourceKind",
                "description": "Open, dot-namespaced code naming the kind of thing a source reference points at, such as `document` or `conversation` or `api_response`.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__SourceReference": {
                "title": "SourceReference",
                "description": "A pointer to the external or internal thing a record's claim came from.",
                "type": "object",
                "properties": {
                    "kind": {
                        "$ref": "#/$defs/records__SourceKind",
                        "description": "What kind of thing this reference points at.",
                    },
                    "source_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the source within its own system of record.",
                    },
                    "locator": {
                        "type": "string",
                        "description": "Optional locator within the source, such as a path, offset, or message id.",
                        "maxLength": 2048,
                    },
                    "retrieved_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the source was read to produce the record it supports.",
                    },
                },
                "required": [
                    "kind",
                    "source_id",
                ],
                "unevaluatedProperties": False,
            },
            "records__SourceSpan": {
                "title": "SourceSpan",
                "description": "An addressable position within a source: a pointer plus an optional character span, so evidence can be pinpointed within a source rather than only referencing the source as a whole.",
                "type": "object",
                "properties": {
                    "pointer": {
                        "type": "string",
                        "description": "Locator within the source, such as a JSON Pointer, XPath, byte offset path, or line reference.",
                        "maxLength": 2048,
                    },
                    "start_offset": {
                        "type": "integer",
                        "description": "Start of the span, in characters from the start of the pointed-at unit, when known.",
                        "minimum": 0,
                    },
                    "end_offset": {
                        "type": "integer",
                        "description": "End of the span, in characters from the start of the pointed-at unit, when known.",
                        "minimum": 0,
                    },
                },
                "required": [
                    "pointer",
                ],
                "unevaluatedProperties": False,
            },
            "records__SupersessionReference": {
                "title": "SupersessionReference",
                "description": "A direction-neutral pointer from one record version to another related record version. The direction of the relationship comes entirely from which field on `RecordIdentity` carries it (`supersedes` vs `superseded_by`); this DTO itself states only which record and version, and why.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier of the related record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "The specific related version, when known.",
                    },
                    "reason": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming why this supersession relationship exists.",
                    },
                },
                "required": [
                    "record_id",
                ],
                "unevaluatedProperties": False,
            },
        },
    },
    "https://contracts.omnivia.dev/application/v1/evidence.schema.json#/$defs/EvidenceSearchInput": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "EvidenceSearchInput",
        "description": "Input for `evidence.search`. Workspace-scoped: the workspace is the request envelope's selected workspace; this payload never carries a second, independent workspace identifier.",
        "type": "object",
        "properties": {
            "query": {
                "$ref": "#/$defs/evidence__EvidenceQuery",
                "description": "Normalized search query.",
            },
            "sensitivity": {
                "$ref": "#/$defs/common__OpenCode",
                "description": "Restrict results to this sensitivity classification, when set.",
            },
            "include_tombstoned": {
                "type": "boolean",
                "description": "Whether tombstoned evidence artifacts may be included. Absent means the server's default (excluded).",
            },
            "limit": {
                "$ref": "#/$defs/common__PageLimit",
                "description": "Bounded maximum number of evidence artifacts to return in this page.",
            },
            "page": {
                "$ref": "#/$defs/common__PageMetadata",
                "description": "Continuation position from a prior page, when paging.",
            },
        },
        "required": [
            "query",
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "common__OpaqueToken": {
                "title": "OpaqueToken",
                "description": "A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never parse it. The pattern's trailing negative lookahead is an end-of-input assertion, not a widening of the character domain: a bare `$` matches before a final line terminator in some conforming regex engines, so a token spelled with a trailing newline would be schema-valid while the semantic validators -- which match the whole string -- refuse it. The lookahead pins the anchor to absolute end of input, so strict schema and semantic validation accept exactly the same tokens.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__OpenCode": {
                "title": "OpenCode",
                "description": "An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible minor releases can add vocabulary; consumers must preserve values they do not recognize.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "common__PageLimit": {
                "title": "PageLimit",
                "description": "A bounded positive page size a caller requests for a paginated read.",
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
            },
            "common__PageMetadata": {
                "title": "PageMetadata",
                "description": "A pagination position. Direction-neutral: the same shape is read differently on a request than on a result, and neither reading is the other's default. On a request, an absent `page` asks for the first page, and a present `page` must actually name a continuation token -- `{}` states nothing to continue from and is invalid. On a result, `page` is always present and states the position this read reached: a continuation token means more remains, and `{}` means the read is exhausted. Exhaustion is therefore stated, never implied by an absent field -- one spelling on every paginated result, so a caller never has to know which result type it is holding to know what 'no next page' looks like. Token issuance, encoding, expiry, and the bindings a token proves are deliberately out of scope here; a token is opaque, and a reader that needs to prove what one was bound to takes that binding as separate trusted input rather than parsing the token.",
                "type": "object",
                "properties": {
                    "continuation_token": {
                        "$ref": "#/$defs/common__OpaqueToken",
                        "description": "Opaque cursor. On a request, the position to continue from; on a result, the position the next page starts at. Absent on a result means the read is exhausted, which is why an exhausted result still carries `page` as `{}` rather than dropping the field.",
                    },
                },
                "required": [],
                "unevaluatedProperties": False,
            },
            "evidence__EvidenceQuery": {
                "title": "EvidenceQuery",
                "description": "A caller-supplied, normalized search query for `evidence.search`. Normalization (case-folding, whitespace, tokenization) is caller-side; this document defines no normalization algorithm.",
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
            },
        },
    },
    "https://contracts.omnivia.dev/application/v1/evidence.schema.json#/$defs/EvidenceSearchResult": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "EvidenceSearchResult",
        "description": "Result of `evidence.search`: complete, append-preserving L0 evidence artifacts with exact provenance. Never substitutes a `GovernedRecord` for an evidence artifact.",
        "type": "object",
        "properties": {
            "evidence": {
                "type": "array",
                "description": "Evidence artifacts in this page.",
                "items": {
                    "$ref": "#/$defs/evidence__EvidenceArtifact",
                },
                "maxItems": 500,
            },
            "page": {
                "$ref": "#/$defs/common__PageMetadata",
                "description": "Continuation position for the next page, absent on the last page.",
            },
        },
        "required": [
            "evidence",
            "page",
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "common__Identifier": {
                "title": "Identifier",
                "description": "Generic bounded, non-empty identifier used for clients, principals, roles, and deprecations.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "common__JsonObject": {
                "title": "JsonObject",
                "description": "An opaque JSON object. The envelope carries domain payloads without inspecting them, which is a statement about the envelope rather than about the payload: an operation's `input` and `result` are each bound to their own definition by `operations.schema.json`'s `x-omnivia-operation-catalogue` (`input_schema_ref` and `result_schema_ref`), and validating a payload against that binding is a separate step from decoding the envelope carrying it.",
                "type": "object",
            },
            "common__OpaqueToken": {
                "title": "OpaqueToken",
                "description": "A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never parse it. The pattern's trailing negative lookahead is an end-of-input assertion, not a widening of the character domain: a bare `$` matches before a final line terminator in some conforming regex engines, so a token spelled with a trailing newline would be schema-valid while the semantic validators -- which match the whole string -- refuse it. The lookahead pins the anchor to absolute end of input, so strict schema and semantic validation accept exactly the same tokens.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__OpenCode": {
                "title": "OpenCode",
                "description": "An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible minor releases can add vocabulary; consumers must preserve values they do not recognize.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "common__PageMetadata": {
                "title": "PageMetadata",
                "description": "A pagination position. Direction-neutral: the same shape is read differently on a request than on a result, and neither reading is the other's default. On a request, an absent `page` asks for the first page, and a present `page` must actually name a continuation token -- `{}` states nothing to continue from and is invalid. On a result, `page` is always present and states the position this read reached: a continuation token means more remains, and `{}` means the read is exhausted. Exhaustion is therefore stated, never implied by an absent field -- one spelling on every paginated result, so a caller never has to know which result type it is holding to know what 'no next page' looks like. Token issuance, encoding, expiry, and the bindings a token proves are deliberately out of scope here; a token is opaque, and a reader that needs to prove what one was bound to takes that binding as separate trusted input rather than parsing the token.",
                "type": "object",
                "properties": {
                    "continuation_token": {
                        "$ref": "#/$defs/common__OpaqueToken",
                        "description": "Opaque cursor. On a request, the position to continue from; on a result, the position the next page starts at. Absent on a result means the read is exhausted, which is why an exhausted result still carries `page` as `{}` rather than dropping the field.",
                    },
                },
                "required": [],
                "unevaluatedProperties": False,
            },
            "common__Timestamp": {
                "title": "Timestamp",
                "description": "An RFC 3339 timestamp in UTC with a literal `Z` offset.",
                "type": "string",
                "format": "date-time",
                "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$(?![\\s\\S])",
                "maxLength": 40,
            },
            "common__WorkspaceId": {
                "title": "WorkspaceId",
                "description": "Bounded, non-empty identifier of the workspace a request is scoped to.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "evidence__EvidenceArtifact": {
                "title": "EvidenceArtifact",
                "description": "One complete, append-preserving L0 evidence artifact: stable identity, workspace, exact source/native locator, applicable temporal instants, content checksum and media type, opaque metadata, permission/sensitivity labels, tombstone status, parser/ingestion status, and append-only provenance history. Carries no `GovernanceLayer`, `GovernanceState`, `RecordCurrentness`, or `authority_level` field: an evidence artifact is raw L0 material, never governed knowledge, and this shape must never be mistaken for a `GovernedRecord`.",
                "type": "object",
                "properties": {
                    "evidence_id": {
                        "$ref": "#/$defs/evidence__EvidenceId",
                        "description": "Stable identifier of this evidence artifact.",
                    },
                    "workspace_id": {
                        "$ref": "#/$defs/common__WorkspaceId",
                        "description": "Workspace this evidence artifact belongs to.",
                    },
                    "source": {
                        "$ref": "#/$defs/records__SourceReference",
                        "description": "Exact source, native identifier, and locator this evidence was captured from.",
                    },
                    "temporal": {
                        "$ref": "#/$defs/records__RecordTemporalMetadata",
                        "description": "The distinct instants applicable to this evidence: when observed, when ingested, and when this artifact was recorded, without collapsing them into one. `valid_from`/`valid_until`/`superseded_at` are rarely applicable to append-only L0 evidence and are typically absent.",
                    },
                    "content_checksum": {
                        "$ref": "#/$defs/evidence__EvidenceChecksum",
                        "description": "Checksum of this evidence artifact's content, proving the content has not been altered since capture.",
                    },
                    "media_type": {
                        "$ref": "#/$defs/evidence__MediaType",
                        "description": "Media type of this evidence artifact's content.",
                    },
                    "metadata": {
                        "$ref": "#/$defs/common__JsonObject",
                        "description": "Opaque metadata captured alongside this evidence artifact.",
                    },
                    "permission_labels": {
                        "type": "array",
                        "description": "Open codes naming the access/permission labels attached to this evidence artifact, such as `restricted` or `team_only`. May be empty when no label applies.",
                        "items": {
                            "$ref": "#/$defs/common__OpenCode",
                        },
                        "maxItems": 64,
                    },
                    "sensitivity": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming this evidence artifact's sensitivity classification, such as `public` or `confidential`.",
                    },
                    "tombstoned": {
                        "type": "boolean",
                        "description": "True when this evidence artifact has been tombstoned. A tombstoned artifact's append-only provenance history is never erased; tombstoning is itself recorded as a provenance entry.",
                    },
                    "parser_status": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the status of parsing this evidence artifact's content, such as `parsed` or `parse_failed`.",
                    },
                    "ingestion_status": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the status of ingesting this evidence artifact, such as `ingested` or `quarantined`.",
                    },
                    "provenance_history": {
                        "type": "array",
                        "description": "Append-only history of actions taken on this evidence artifact. Never truncated or rewritten; a correction is a new entry, not an edit to a prior one. Never empty: even the first capture of this artifact is itself a provenance entry, so an artifact with no recorded history entry would be an unaudited state.",
                        "items": {
                            "$ref": "#/$defs/records__ProvenanceEntry",
                        },
                        "minItems": 1,
                        "maxItems": 1024,
                    },
                    "import_run_id": {
                        "$ref": "#/$defs/common__OpaqueToken",
                        "description": "Names the import job/run that produced this evidence artifact, when it was produced by one. Typed as `OpaqueToken` because it is drawn from exactly the same opaque token domain as `JobIdentity.job_id` and `ImportCompletionResult.import_run_id`: the completion contract promises this backlink names the run that created this evidence, and spelled in any narrower vocabulary it could not hold every job id the contract admits -- a run such as `job/opaque-token` could complete and then have no writable backlink at all.",
                    },
                },
                "required": [
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
                ],
                "unevaluatedProperties": False,
            },
            "evidence__EvidenceChecksum": {
                "title": "EvidenceChecksum",
                "description": "A content checksum, spelled `algorithm:hex-digest` (such as `sha256:9f86d0...`) so the digest is never ambiguous about which algorithm produced it. Provider-neutral: this contract does not mandate a specific algorithm.",
                "type": "string",
                "minLength": 3,
                "maxLength": 256,
                "pattern": "^[a-z][a-z0-9_]*:[A-Za-z0-9+/=_-]+$(?![\\s\\S])",
            },
            "evidence__EvidenceId": {
                "title": "EvidenceId",
                "description": "Stable identifier of one L0 evidence artifact, constant across its append-only provenance history. Distinct from `RecordId`: an evidence artifact is never itself a governed record.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "evidence__MediaType": {
                "title": "MediaType",
                "description": "An IANA-style `type/subtype` media type string, such as `text/plain` or `application/json`.",
                "type": "string",
                "minLength": 3,
                "maxLength": 255,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$(?![\\s\\S])",
            },
            "records__EvidenceReference": {
                "title": "EvidenceReference",
                "description": "A concrete piece of evidence supporting one claim in a record.",
                "type": "object",
                "properties": {
                    "source": {
                        "$ref": "#/$defs/records__SourceReference",
                        "description": "The source this evidence was drawn from.",
                    },
                    "span": {
                        "$ref": "#/$defs/records__SourceSpan",
                        "description": "Addressable position within the source this evidence was drawn from, when known.",
                    },
                    "excerpt": {
                        "type": "string",
                        "description": "Optional short excerpt from the source substantiating the claim. Not a stable interface.",
                        "maxLength": 4096,
                    },
                },
                "required": [
                    "source",
                ],
                "unevaluatedProperties": False,
            },
            "records__ProvenanceEntry": {
                "title": "ProvenanceEntry",
                "description": "One step in a record's history: who or what did what, when, and -- for a governance transition -- the explicit rationale it was taken under.",
                "type": "object",
                "properties": {
                    "actor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Principal or system that performed this action.",
                    },
                    "actor_kind": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.",
                    },
                    "action": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming what happened, such as `created` or `modified` or `superseded`.",
                    },
                    "occurred_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this action occurred.",
                    },
                    "reason_code": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming why this action was taken, carried over verbatim from the `GovernanceRationale.reason_code` the transition was requested under. Absent on ordinary non-governance history, which never carries a rationale; a governance-transition event must carry it, and requiring that is a semantic-validation concern, not a wire-shape one.",
                    },
                    "reason_comment": {
                        "type": "string",
                        "description": "Bounded human-readable elaboration, carried over verbatim from the requesting `GovernanceRationale.comment`. Absent exactly when that comment was absent. Not a stable interface.",
                        "maxLength": 2048,
                    },
                    "evidence": {
                        "type": "array",
                        "description": "Evidence supporting this action, when applicable. Bounded at the same 256 items `CandidateAssertion.evidence` is, and deliberately so: a `record.supersede` transition appends exactly one event whose evidence must equal the replacement claim's complete assertion evidence, so a lower bound here would make an otherwise valid replacement impossible to record.",
                        "items": {
                            "$ref": "#/$defs/records__EvidenceReference",
                        },
                        "maxItems": 256,
                    },
                },
                "required": [
                    "actor_id",
                    "actor_kind",
                    "action",
                    "occurred_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordTemporalMetadata": {
                "title": "RecordTemporalMetadata",
                "description": "The distinct instants a governed record's lifecycle turns on: when the underlying fact occurred in the world, when it was observed, when the system ingested it, when this version was persisted, the window it is asserted valid for, and when it was superseded.",
                "type": "object",
                "properties": {
                    "event_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the underlying fact occurred in the world (source/event time), when distinguishable from `observed_at`.",
                    },
                    "observed_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the underlying fact was observed to be true in the world, when known.",
                    },
                    "ingested_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the system first ingested the fact behind this record.",
                    },
                    "recorded_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this specific version was persisted.",
                    },
                    "valid_from": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Start of the window this record is asserted valid for, when bounded.",
                    },
                    "valid_until": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "End of the window this record is asserted valid for, when bounded.",
                    },
                    "superseded_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this record version was superseded by a newer version, present only once superseded.",
                    },
                },
                "required": [
                    "ingested_at",
                    "recorded_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__SourceKind": {
                "title": "SourceKind",
                "description": "Open, dot-namespaced code naming the kind of thing a source reference points at, such as `document` or `conversation` or `api_response`.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__SourceReference": {
                "title": "SourceReference",
                "description": "A pointer to the external or internal thing a record's claim came from.",
                "type": "object",
                "properties": {
                    "kind": {
                        "$ref": "#/$defs/records__SourceKind",
                        "description": "What kind of thing this reference points at.",
                    },
                    "source_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the source within its own system of record.",
                    },
                    "locator": {
                        "type": "string",
                        "description": "Optional locator within the source, such as a path, offset, or message id.",
                        "maxLength": 2048,
                    },
                    "retrieved_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the source was read to produce the record it supports.",
                    },
                },
                "required": [
                    "kind",
                    "source_id",
                ],
                "unevaluatedProperties": False,
            },
            "records__SourceSpan": {
                "title": "SourceSpan",
                "description": "An addressable position within a source: a pointer plus an optional character span, so evidence can be pinpointed within a source rather than only referencing the source as a whole.",
                "type": "object",
                "properties": {
                    "pointer": {
                        "type": "string",
                        "description": "Locator within the source, such as a JSON Pointer, XPath, byte offset path, or line reference.",
                        "maxLength": 2048,
                    },
                    "start_offset": {
                        "type": "integer",
                        "description": "Start of the span, in characters from the start of the pointed-at unit, when known.",
                        "minimum": 0,
                    },
                    "end_offset": {
                        "type": "integer",
                        "description": "End of the span, in characters from the start of the pointed-at unit, when known.",
                        "minimum": 0,
                    },
                },
                "required": [
                    "pointer",
                ],
                "unevaluatedProperties": False,
            },
        },
    },
    "https://contracts.omnivia.dev/application/v1/graph.schema.json#/$defs/GraphTraversalInput": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "GraphTraversalInput",
        "description": "Input for `graph.traverse`. Workspace-scoped: the workspace is the request envelope's selected workspace; this payload never carries a second, independent workspace identifier. Absent `view` defaults to `current_canonical`; only an explicit `view` selector may request `candidates` or `history`. Absent `direction` defaults to `outbound`.",
        "type": "object",
        "properties": {
            "start": {
                "type": "array",
                "description": "One or more starting record versions to traverse from. Every one of them is returned at depth 0 on a first page, and depth 0 is exactly this set: no other node ever carries it.",
                "items": {
                    "$ref": "#/$defs/records__RecordVersionReference",
                },
                "minItems": 1,
                "maxItems": 64,
            },
            "direction": {
                "$ref": "#/$defs/graph__GraphDirection",
                "description": "Which direction to follow relations in. Absent means `outbound`.",
            },
            "relation_types": {
                "type": "array",
                "description": "Restrict traversal to these relation types, when set. Absent means every relation type; present means a bounded, non-empty set of distinct types, since an empty filter would ask for nothing and a repeated type states the same restriction twice.",
                "items": {
                    "$ref": "#/$defs/graph__GraphRelationType",
                },
                "minItems": 1,
                "maxItems": 64,
                "uniqueItems": True,
            },
            "domain_scope": {
                "$ref": "#/$defs/memory__RecordDomainScope",
                "description": "Restrict traversal to this domain scope, when set.",
            },
            "view": {
                "$ref": "#/$defs/memory__GovernedRecordView",
                "description": "Which slice of records' versions to consider. Absent defaults to `current_canonical`; requesting `candidates` or `history` requires this field to be set explicitly.",
            },
            "as_of": {
                "$ref": "#/$defs/common__Timestamp",
                "description": "Caller-requested point in time for a reproducible historical traversal, when set. Distinct from the response's own `canonical_resolution_time`: this is what the caller asked for, not what the server actually used.",
            },
            "depth_limit": {
                "$ref": "#/$defs/graph__GraphDepthLimit",
                "description": "Bounded maximum traversal depth requested. Absent means the server's default depth.",
            },
            "node_limit": {
                "$ref": "#/$defs/common__PageLimit",
                "description": "Bounded maximum number of nodes requested. Absent means the server's default node limit. When set it must be at least the number of `start` seeds: a first page owes every seed at depth 0 and may return no more nodes than the limit, so a smaller one asks for a result no traversal could return.",
            },
            "edge_limit": {
                "$ref": "#/$defs/common__PageLimit",
                "description": "Bounded maximum number of edges requested. Absent means the server's default edge limit.",
            },
            "page": {
                "$ref": "#/$defs/common__PageMetadata",
                "description": "Continuation position from a prior page, when paging a traversal whose ordering can be deterministically continued. Present states that this is a continuation page: its seeds were already returned by an earlier page, so they are not returned again at any depth and every node it carries sits at depth 1 or deeper.",
            },
        },
        "required": [
            "start",
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "common__OpaqueToken": {
                "title": "OpaqueToken",
                "description": "A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never parse it. The pattern's trailing negative lookahead is an end-of-input assertion, not a widening of the character domain: a bare `$` matches before a final line terminator in some conforming regex engines, so a token spelled with a trailing newline would be schema-valid while the semantic validators -- which match the whole string -- refuse it. The lookahead pins the anchor to absolute end of input, so strict schema and semantic validation accept exactly the same tokens.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__PageLimit": {
                "title": "PageLimit",
                "description": "A bounded positive page size a caller requests for a paginated read.",
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
            },
            "common__PageMetadata": {
                "title": "PageMetadata",
                "description": "A pagination position. Direction-neutral: the same shape is read differently on a request than on a result, and neither reading is the other's default. On a request, an absent `page` asks for the first page, and a present `page` must actually name a continuation token -- `{}` states nothing to continue from and is invalid. On a result, `page` is always present and states the position this read reached: a continuation token means more remains, and `{}` means the read is exhausted. Exhaustion is therefore stated, never implied by an absent field -- one spelling on every paginated result, so a caller never has to know which result type it is holding to know what 'no next page' looks like. Token issuance, encoding, expiry, and the bindings a token proves are deliberately out of scope here; a token is opaque, and a reader that needs to prove what one was bound to takes that binding as separate trusted input rather than parsing the token.",
                "type": "object",
                "properties": {
                    "continuation_token": {
                        "$ref": "#/$defs/common__OpaqueToken",
                        "description": "Opaque cursor. On a request, the position to continue from; on a result, the position the next page starts at. Absent on a result means the read is exhausted, which is why an exhausted result still carries `page` as `{}` rather than dropping the field.",
                    },
                },
                "required": [],
                "unevaluatedProperties": False,
            },
            "common__Timestamp": {
                "title": "Timestamp",
                "description": "An RFC 3339 timestamp in UTC with a literal `Z` offset.",
                "type": "string",
                "format": "date-time",
                "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$(?![\\s\\S])",
                "maxLength": 40,
            },
            "graph__GraphDepthLimit": {
                "title": "GraphDepthLimit",
                "description": "A bounded traversal depth a caller may request, or the server states it actually applied. Zero means the seeds themselves with no traversal beyond them; absent on input means the server's default depth of 1.",
                "type": "integer",
                "minimum": 0,
                "maximum": 8,
            },
            "graph__GraphDirection": {
                "title": "GraphDirection",
                "description": "Open, dot-namespaced code naming which direction a traversal follows relations in: `outbound`, `inbound`, or `both`. Wire-open by shape, but trust-sensitive: only the known values are accepted by semantic validation, and an unrecognized value fails closed rather than being guessed at.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "graph__GraphRelationType": {
                "title": "GraphRelationType",
                "description": "Open, dot-namespaced code naming a kind of relation between governed records, such as `relates_to` or `derived_from`. Open by design so a compatible minor release can add relation types without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__GovernedRecordView": {
                "title": "GovernedRecordView",
                "description": "Open, dot-namespaced code selecting which slice of a governed record's versions a read considers: `current_canonical` (the single active accepted version, the default when this field is absent), `candidates` (proposed/candidate versions not yet accepted), or `history` (every version, including superseded ones). Open by design so a compatible minor release can add views without breaking existing decoders. Default resolution when absent is a semantic concern (see `omnivia_core.contracts.v1.semantics`), not a wire-shape one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__RecordDomainScope": {
                "title": "RecordDomainScope",
                "description": "Open, bounded, non-empty, dot-namespaced record classification stating what domain a governed record belongs to, such as `personal.preferences` or `project.roadmap`. Distinct from the caller-authorization `Scope` vocabulary (e.g. `memory:read`): a domain scope never grants or checks a permission, it only classifies what the record is about. Open by design so a compatible minor release can add classifications without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__RecordId": {
                "title": "RecordId",
                "description": "Stable identifier of a governed record, constant across every version of that record.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "records__RecordVersion": {
                "title": "RecordVersion",
                "description": "Opaque, server-issued version marker of one specific revision of a record. Clients must round-trip it verbatim and must never parse it.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "records__RecordVersionReference": {
                "title": "RecordVersionReference",
                "description": "A precise, non-directional pointer to one exact record version: both `record_id` and `version` are always required. Distinct from `SupersessionReference`, which is direction-bearing (its meaning comes from which `RecordIdentity` field carries it) and whose `version` is optional. Used wherever a payload must name a specific existing record version -- a graph traversal start point or edge endpoint, a context pack citation or source-version list -- without asserting any supersession relationship.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier of the referenced record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "The exact referenced version.",
                    },
                },
                "required": [
                    "record_id",
                    "version",
                ],
                "unevaluatedProperties": False,
            },
        },
    },
    "https://contracts.omnivia.dev/application/v1/graph.schema.json#/$defs/GraphTraversalResult": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "GraphTraversalResult",
        "description": "Result of `graph.traverse`: the traversed nodes and edges, the traversal limits actually applied (which may be tighter than requested but never looser), the projection metadata/watermark this traversal was served from, and deterministic ordering evidence. Boundaries are stated, never implied: an edge whose source or target this traversal did not reach carries the endpoint absent plus a `boundary_reason` that must actually hold here -- `page_boundary` only when `page` offers a continuation token and `nodes` reached `applied_node_limit` exactly, `depth_boundary` only when the endpoint that *is* present is a returned node sitting at `applied_depth_limit`. Projection loss is never canonical-data loss: an absent endpoint says this page stopped, not that the relation lost an end.",
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "description": "Nodes reached by this traversal, in the order named by `ordering_basis`: ascending by `(depth, reference.record_id, reference.version)`.",
                "items": {
                    "$ref": "#/$defs/graph__GraphNode",
                },
                "maxItems": 1000,
            },
            "edges": {
                "type": "array",
                "description": "Edges reached by this traversal, in the order named by `ordering_basis`: ascending by the complete tuple `(source.record_id, source.version, relation_type, target.record_id, target.version, relation_reference.record_id, relation_reference.version)`. All seven fields participate, so two edges sharing the same endpoints but naming different relations, or the same relation type recorded by different relation record versions, still have one reproducible order. An absent endpoint contributes the empty string in both of its positions, which sorts before any present reference.",
                "items": {
                    "$ref": "#/$defs/graph__GraphEdge",
                },
                "maxItems": 1000,
            },
            "applied_depth_limit": {
                "$ref": "#/$defs/graph__GraphDepthLimit",
                "description": "The traversal depth actually applied.",
            },
            "applied_node_limit": {
                "$ref": "#/$defs/common__PageLimit",
                "description": "The node limit actually applied. May be tighter than a requested `node_limit`, but on a first page never below the number of requested `start` seeds -- including when the request named no `node_limit` and this limit is the server's own choice, since that page still owes every seed.",
            },
            "applied_edge_limit": {
                "$ref": "#/$defs/common__PageLimit",
                "description": "The edge limit actually applied.",
            },
            "freshness": {
                "$ref": "#/$defs/common__ProjectionFreshness",
                "description": "The projection version(s)/watermark this traversal was actually served from.",
            },
            "ordering_basis": {
                "$ref": "#/$defs/graph__GraphOrderingBasis",
                "description": "The deterministic key `nodes` and `edges` are ordered by.",
            },
            "page": {
                "$ref": "#/$defs/common__PageMetadata",
                "description": "Position this traversal reached. Always present: a continuation token when the traversal's ordering can be deterministically continued and more remains, and `{}` when it is exhausted or cannot be paged. Exhaustion is stated rather than implied by an absent field, so a caller reads the same shape here as on every other paginated result in this contract.",
            },
        },
        "required": [
            "nodes",
            "edges",
            "applied_depth_limit",
            "applied_node_limit",
            "applied_edge_limit",
            "freshness",
            "ordering_basis",
            "page",
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "common__Identifier": {
                "title": "Identifier",
                "description": "Generic bounded, non-empty identifier used for clients, principals, roles, and deprecations.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "common__JsonObject": {
                "title": "JsonObject",
                "description": "An opaque JSON object. The envelope carries domain payloads without inspecting them, which is a statement about the envelope rather than about the payload: an operation's `input` and `result` are each bound to their own definition by `operations.schema.json`'s `x-omnivia-operation-catalogue` (`input_schema_ref` and `result_schema_ref`), and validating a payload against that binding is a separate step from decoding the envelope carrying it.",
                "type": "object",
            },
            "common__OpaqueToken": {
                "title": "OpaqueToken",
                "description": "A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never parse it. The pattern's trailing negative lookahead is an end-of-input assertion, not a widening of the character domain: a bare `$` matches before a final line terminator in some conforming regex engines, so a token spelled with a trailing newline would be schema-valid while the semantic validators -- which match the whole string -- refuse it. The lookahead pins the anchor to absolute end of input, so strict schema and semantic validation accept exactly the same tokens.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__OpenCode": {
                "title": "OpenCode",
                "description": "An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible minor releases can add vocabulary; consumers must preserve values they do not recognize.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "common__PageLimit": {
                "title": "PageLimit",
                "description": "A bounded positive page size a caller requests for a paginated read.",
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
            },
            "common__PageMetadata": {
                "title": "PageMetadata",
                "description": "A pagination position. Direction-neutral: the same shape is read differently on a request than on a result, and neither reading is the other's default. On a request, an absent `page` asks for the first page, and a present `page` must actually name a continuation token -- `{}` states nothing to continue from and is invalid. On a result, `page` is always present and states the position this read reached: a continuation token means more remains, and `{}` means the read is exhausted. Exhaustion is therefore stated, never implied by an absent field -- one spelling on every paginated result, so a caller never has to know which result type it is holding to know what 'no next page' looks like. Token issuance, encoding, expiry, and the bindings a token proves are deliberately out of scope here; a token is opaque, and a reader that needs to prove what one was bound to takes that binding as separate trusted input rather than parsing the token.",
                "type": "object",
                "properties": {
                    "continuation_token": {
                        "$ref": "#/$defs/common__OpaqueToken",
                        "description": "Opaque cursor. On a request, the position to continue from; on a result, the position the next page starts at. Absent on a result means the read is exhausted, which is why an exhausted result still carries `page` as `{}` rather than dropping the field.",
                    },
                },
                "required": [],
                "unevaluatedProperties": False,
            },
            "common__ProjectionFreshness": {
                "title": "ProjectionFreshness",
                "description": "Staleness statement for reads served from a projection rather than the write model. Every projection this read was served from must be named in both `projection_versions` and `projection_watermarks`: the two maps are one statement about the same set of projections, so their key sets are required to be identical and neither may be empty. A read served from no named projection cannot state its own staleness, and a projection that states a version but no watermark (or the reverse) leaves the caller unable to tell how far behind the write model it actually is.",
                "type": "object",
                "properties": {
                    "as_of": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Point in time the projection reflects.",
                    },
                    "projection_versions": {
                        "type": "object",
                        "description": "Open map of projection name to the opaque projection version this read was actually served from. Projection names are an open vocabulary, so new projections may appear in compatible minor releases; at least one must be named, and the key set must equal `projection_watermarks`'.",
                        "minProperties": 1,
                        "propertyNames": {
                            "$ref": "#/$defs/common__OpenCode",
                        },
                        "additionalProperties": {
                            "$ref": "#/$defs/common__ProjectionVersion",
                        },
                    },
                    "projection_watermarks": {
                        "type": "object",
                        "description": "Open map of projection name to the opaque write-model version each projection has consumed up to -- how far the projection has caught up, as opposed to which version served this read. Keyed by exactly the same projection names as `projection_versions`; at least one must be named.",
                        "minProperties": 1,
                        "propertyNames": {
                            "$ref": "#/$defs/common__OpenCode",
                        },
                        "additionalProperties": {
                            "$ref": "#/$defs/common__ProjectionVersion",
                        },
                    },
                    "stale": {
                        "type": "boolean",
                        "description": "True when the server knows the projection lags the write model.",
                    },
                },
                "required": [
                    "as_of",
                    "projection_versions",
                    "projection_watermarks",
                    "stale",
                ],
                "unevaluatedProperties": False,
            },
            "common__ProjectionVersion": {
                "title": "ProjectionVersion",
                "description": "An opaque per-projection version marker used to reason about read staleness.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__Timestamp": {
                "title": "Timestamp",
                "description": "An RFC 3339 timestamp in UTC with a literal `Z` offset.",
                "type": "string",
                "format": "date-time",
                "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$(?![\\s\\S])",
                "maxLength": 40,
            },
            "common__WorkspaceId": {
                "title": "WorkspaceId",
                "description": "Bounded, non-empty identifier of the workspace a request is scoped to.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "graph__GraphBoundaryReason": {
                "title": "GraphBoundaryReason",
                "description": "Open, dot-namespaced code justifying why one endpoint of an edge is absent from a traversal result: `page_boundary` when the traversal stopped at the node limit and offers a continuation token, or `depth_boundary` when the present endpoint sits exactly at the applied depth limit. Wire-open by shape, but trust-sensitive: an absent endpoint is a claim that the projection stopped, not that the relation lost an end, so only the recognized values are accepted by semantic validation and an unrecognized reason fails closed rather than being guessed at.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "graph__GraphDepthLimit": {
                "title": "GraphDepthLimit",
                "description": "A bounded traversal depth a caller may request, or the server states it actually applied. Zero means the seeds themselves with no traversal beyond them; absent on input means the server's default depth of 1.",
                "type": "integer",
                "minimum": 0,
                "maximum": 8,
            },
            "graph__GraphEdge": {
                "title": "GraphEdge",
                "description": "One edge in a traversal result: the relation type, its source and target governed-record versions, the relation's own full governed record, and the precise reference identifying that relation record. Never carries a competing identity, provenance, lifecycle, authority, or governance state of its own beyond that wrapped record: `relation_reference` must identify `record.provenance.identity` exactly, so the relation record is referenced, never re-identified. `source` and `target` are structurally optional so a result can represent a justified page/depth boundary where one end of a relation was not reached; at least one must be present, and exactly one may be absent only together with a coherent `boundary_reason`. Both endpoints present means a fully materialized edge and forbids `boundary_reason`; both absent is never representable, since an edge that names no returned node states nothing this result can be trusted about.",
                "type": "object",
                "properties": {
                    "relation_type": {
                        "$ref": "#/$defs/graph__GraphRelationType",
                        "description": "Kind of relation this edge represents.",
                    },
                    "source": {
                        "$ref": "#/$defs/records__RecordVersionReference",
                        "description": "The record version this edge originates from. Absent only at a justified page/depth boundary, together with a coherent `boundary_reason`; absence means this traversal did not reach that end, never that the relation has no source.",
                    },
                    "target": {
                        "$ref": "#/$defs/records__RecordVersionReference",
                        "description": "The record version this edge points to. Absent only at a justified page/depth boundary, together with a coherent `boundary_reason`; absence means this traversal did not reach that end, never that the relation has no target.",
                    },
                    "record": {
                        "$ref": "#/$defs/memory__GovernedRecord",
                        "description": "The relation's own full governed record, retaining its own full evidence and provenance.",
                    },
                    "relation_reference": {
                        "$ref": "#/$defs/records__RecordVersionReference",
                        "description": "Precise reference to the canonical governed record version of the relation itself -- the record `record` wraps. Must identify `record.provenance.identity` exactly; it is a pointer to that record's identity, never a second identity the edge owns.",
                    },
                    "boundary_reason": {
                        "$ref": "#/$defs/graph__GraphBoundaryReason",
                        "description": "Why exactly one endpoint is absent. Required when exactly one of `source`/`target` is absent, forbidden when both are present, and never sufficient on its own: the stated reason must actually hold against this result's page metadata and applied limits.",
                    },
                },
                "required": [
                    "relation_type",
                    "record",
                    "relation_reference",
                ],
                "unevaluatedProperties": False,
            },
            "graph__GraphNode": {
                "title": "GraphNode",
                "description": "One node in a traversal result: a precise reference to the canonical governed record version it represents, the full governed record it wraps, and the depth at which this traversal reached it. Never carries a competing identity, provenance, lifecycle, authority, or governance state of its own -- `reference` and `record` are the only sources of truth, and they must agree.",
                "type": "object",
                "properties": {
                    "reference": {
                        "$ref": "#/$defs/records__RecordVersionReference",
                        "description": "Precise reference to the canonical governed record version this node represents.",
                    },
                    "record": {
                        "$ref": "#/$defs/memory__GovernedRecord",
                        "description": "The full governed record this node wraps.",
                    },
                    "depth": {
                        "$ref": "#/$defs/graph__GraphDepthLimit",
                        "description": "The traversal depth at which this node was reached, measured from the requested seeds: 0 exactly for a node the request named in `start`, and never for any other node.",
                    },
                },
                "required": [
                    "reference",
                    "record",
                    "depth",
                ],
                "unevaluatedProperties": False,
            },
            "graph__GraphOrderingBasis": {
                "title": "GraphOrderingBasis",
                "description": "Open, dot-namespaced code naming the deterministic key a traversal result was ordered by, such as `record_id_asc`, so identical inputs against an unchanged projection reproduce identical node/edge ordering.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "graph__GraphRelationType": {
                "title": "GraphRelationType",
                "description": "Open, dot-namespaced code naming a kind of relation between governed records, such as `relates_to` or `derived_from`. Open by design so a compatible minor release can add relation types without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__GovernedRecord": {
                "title": "GovernedRecord",
                "description": "A provider-neutral governed record: which workspace it belongs to, what kind of record it is, its domain scope and authority level, its full L0-L4 governance, temporal, evidence, and provenance envelope, and its opaque JSON content. Carries no reference to, and is not a substitute for, any repo-local `Memory`, `MemoryFact`, or `SourceRef` domain class.",
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "$ref": "#/$defs/common__WorkspaceId",
                        "description": "Workspace this record belongs to.",
                    },
                    "record_type": {
                        "$ref": "#/$defs/memory__GovernedRecordType",
                        "description": "What kind of governed record this is.",
                    },
                    "domain_scope": {
                        "$ref": "#/$defs/memory__RecordDomainScope",
                        "description": "Non-empty domain/record classification this record is filed under. Every governed record carries exactly one; a caller may propose one through `memory.create`, but the server is always the final authority on what is actually stored here. Distinct from caller-authorization `Scope`.",
                    },
                    "authority_level": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the authority level this record's governance decision currently carries, such as `proposed` or `reviewed` or `canonical`. Server-owned: no `memory.create` input field lets a caller assert this directly.",
                    },
                    "reviewer": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the reviewer or policy that produced this record's current governance decision, when one has been recorded. Absent when no reviewer/policy decision applies yet, such as a freshly proposed record.",
                    },
                    "provenance": {
                        "$ref": "#/$defs/records__RecordProvenance",
                        "description": "Identity, governance layer/state/currentness, temporal metadata, history, and evidence for this record version.",
                    },
                    "content": {
                        "$ref": "#/$defs/common__JsonObject",
                        "description": "Opaque governed content this record carries.",
                    },
                },
                "required": [
                    "workspace_id",
                    "record_type",
                    "domain_scope",
                    "authority_level",
                    "provenance",
                    "content",
                ],
                "unevaluatedProperties": False,
            },
            "memory__GovernedRecordType": {
                "title": "GovernedRecordType",
                "description": "Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact` or `memory.entity` or `memory.relation`. Open by design so a compatible minor release can add record types without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__RecordDomainScope": {
                "title": "RecordDomainScope",
                "description": "Open, bounded, non-empty, dot-namespaced record classification stating what domain a governed record belongs to, such as `personal.preferences` or `project.roadmap`. Distinct from the caller-authorization `Scope` vocabulary (e.g. `memory:read`): a domain scope never grants or checks a permission, it only classifies what the record is about. Open by design so a compatible minor release can add classifications without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__CandidateAssertion": {
                "title": "CandidateAssertion",
                "description": "Who is asserting a governed record's claim, when, and on what evidence, plus the validity window they propose for it. This is caller-supplied provenance for the claim -- carried into `memory.create` and `record.supersede` inputs, and preserved on the resulting record's `RecordProvenance` -- not the server-owned governance decision: it never carries authority level, reviewer/policy identity, or any other field a least-authority-escalating mutation input is forbidden from carrying. Defined here rather than in `memory.schema.json` so `RecordProvenance` can preserve it without `records.schema.json` depending on a document that already depends on it.",
                "type": "object",
                "properties": {
                    "actor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Principal or system asserting this candidate.",
                    },
                    "actor_kind": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.",
                    },
                    "actor_role": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the role the actor asserted this candidate under.",
                    },
                    "asserted_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the actor asserted this candidate.",
                    },
                    "proposed_valid_from": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Start of the validity window the caller proposes for this candidate, when known. The server remains the final authority on the validity window actually stored.",
                    },
                    "proposed_valid_until": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "End of the validity window the caller proposes for this candidate, when bounded. The server remains the final authority on the validity window actually stored.",
                    },
                    "evidence": {
                        "type": "array",
                        "description": "Concrete evidence substantiating this assertion. May be empty only when the enclosing input's `evidence_disposition` explicitly excuses it; enforcing that agreement is a semantic-validation concern, not a wire-shape one.",
                        "items": {
                            "$ref": "#/$defs/records__EvidenceReference",
                        },
                        "maxItems": 256,
                    },
                },
                "required": [
                    "actor_id",
                    "actor_kind",
                    "actor_role",
                    "asserted_at",
                    "evidence",
                ],
                "unevaluatedProperties": False,
            },
            "records__CandidateExtractionMetadata": {
                "title": "CandidateExtractionMetadata",
                "description": "Optional provenance about the automated extractor that produced a governed record's claim, when one did. Absent entirely for a claim a human asserted directly. Defined here rather than in `memory.schema.json` so `RecordProvenance` can preserve it without `records.schema.json` depending on a document that already depends on it.",
                "type": "object",
                "properties": {
                    "extractor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the extractor that produced this candidate.",
                    },
                    "extractor_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the extractor that produced this candidate, when known.",
                    },
                    "model_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the model the extractor used, when known.",
                    },
                    "prompt_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the prompt the extractor used, when known.",
                    },
                    "extracted_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the extractor produced this candidate.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "The extractor's self-reported confidence in this candidate, on a 0-1 scale, when known.",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "reconciliation_state": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming this candidate's reconciliation/deduplication state against prior extractions, such as `novel` or `duplicate` or `merged`, when the extractor determined one. Open by design; an unrecognized value must be preserved, not coerced to a known one, and must never widen this candidate's authority.",
                    },
                },
                "required": [
                    "extractor_id",
                    "extracted_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__EvidenceDisposition": {
                "title": "EvidenceDisposition",
                "description": "Open, dot-namespaced code stating whether concrete evidence is actually available for a record, such as `available` or `unavailable` or `redacted`. Open by design; an unrecognized value must be preserved, not coerced to a known one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__EvidenceReference": {
                "title": "EvidenceReference",
                "description": "A concrete piece of evidence supporting one claim in a record.",
                "type": "object",
                "properties": {
                    "source": {
                        "$ref": "#/$defs/records__SourceReference",
                        "description": "The source this evidence was drawn from.",
                    },
                    "span": {
                        "$ref": "#/$defs/records__SourceSpan",
                        "description": "Addressable position within the source this evidence was drawn from, when known.",
                    },
                    "excerpt": {
                        "type": "string",
                        "description": "Optional short excerpt from the source substantiating the claim. Not a stable interface.",
                        "maxLength": 4096,
                    },
                },
                "required": [
                    "source",
                ],
                "unevaluatedProperties": False,
            },
            "records__GovernanceLayer": {
                "title": "GovernanceLayer",
                "description": "Open, dot-namespaced code naming the knowledge-governance layer a record belongs to: `l0` (raw evidence), `l1` (candidate observations), `l2` (governed records / canonical knowledge), `l3` (context models), or `l4` (organisational model). Distinct from workspace scope, which is a caller-facing tenancy boundary, not a knowledge-governance layer. Open by design so a compatible minor release can add layers without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__GovernanceState": {
                "title": "GovernanceState",
                "description": "Open, dot-namespaced code naming a record's position in its own governance workflow, such as `proposed` or `candidate` or `accepted` or `rejected`. Distinct from `GovernanceLayer` (which namespace a record belongs to) and `RecordCurrentness` (whether this version is the active one): a record can be `accepted` and still later superseded, or `proposed` and never adopted. Open by design so a compatible minor release can add states without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__ProvenanceEntry": {
                "title": "ProvenanceEntry",
                "description": "One step in a record's history: who or what did what, when, and -- for a governance transition -- the explicit rationale it was taken under.",
                "type": "object",
                "properties": {
                    "actor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Principal or system that performed this action.",
                    },
                    "actor_kind": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.",
                    },
                    "action": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming what happened, such as `created` or `modified` or `superseded`.",
                    },
                    "occurred_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this action occurred.",
                    },
                    "reason_code": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming why this action was taken, carried over verbatim from the `GovernanceRationale.reason_code` the transition was requested under. Absent on ordinary non-governance history, which never carries a rationale; a governance-transition event must carry it, and requiring that is a semantic-validation concern, not a wire-shape one.",
                    },
                    "reason_comment": {
                        "type": "string",
                        "description": "Bounded human-readable elaboration, carried over verbatim from the requesting `GovernanceRationale.comment`. Absent exactly when that comment was absent. Not a stable interface.",
                        "maxLength": 2048,
                    },
                    "evidence": {
                        "type": "array",
                        "description": "Evidence supporting this action, when applicable. Bounded at the same 256 items `CandidateAssertion.evidence` is, and deliberately so: a `record.supersede` transition appends exactly one event whose evidence must equal the replacement claim's complete assertion evidence, so a lower bound here would make an otherwise valid replacement impossible to record.",
                        "items": {
                            "$ref": "#/$defs/records__EvidenceReference",
                        },
                        "maxItems": 256,
                    },
                },
                "required": [
                    "actor_id",
                    "actor_kind",
                    "action",
                    "occurred_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordCurrentness": {
                "title": "RecordCurrentness",
                "description": "Open, dot-namespaced code naming whether a record version is the active one, such as `current` or `superseded` or `retracted`. Open by design; an unrecognized value must be preserved, not coerced to a known one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__RecordId": {
                "title": "RecordId",
                "description": "Stable identifier of a governed record, constant across every version of that record.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "records__RecordIdentity": {
                "title": "RecordIdentity",
                "description": "The identity, version, governance layer, governance state, and currentness of one record version.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier stable across every version of this record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "Opaque version of this specific revision.",
                    },
                    "layer": {
                        "$ref": "#/$defs/records__GovernanceLayer",
                        "description": "Governance layer this record belongs to.",
                    },
                    "governance_state": {
                        "$ref": "#/$defs/records__GovernanceState",
                        "description": "This record version's position in its own governance workflow, independent of `layer` and `currentness`.",
                    },
                    "currentness": {
                        "$ref": "#/$defs/records__RecordCurrentness",
                        "description": "Whether this version is the active one.",
                    },
                    "supersedes": {
                        "$ref": "#/$defs/records__SupersessionReference",
                        "description": "The earlier record version this version replaces, when this version is itself the newer one.",
                    },
                    "superseded_by": {
                        "$ref": "#/$defs/records__SupersessionReference",
                        "description": "The newer record version that replaced this one, present when `currentness` marks this version as superseded.",
                    },
                },
                "required": [
                    "record_id",
                    "version",
                    "layer",
                    "governance_state",
                    "currentness",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordProvenance": {
                "title": "RecordProvenance",
                "description": "The full provenance envelope for one record version: identity, temporal metadata, its authoring history, the sources it draws on, and the caller-supplied assertion/extraction lineage the claim in this version came from. `assertion`/`extraction` are structurally optional so a record written before they existed still decodes, but a governance transition that replaces or carries forward a claim must bind them; enforcing that is a semantic-validation concern, not a wire-shape one.",
                "type": "object",
                "properties": {
                    "identity": {
                        "$ref": "#/$defs/records__RecordIdentity",
                        "description": "Identity, version, governance layer, and currentness of this record.",
                    },
                    "temporal": {
                        "$ref": "#/$defs/records__RecordTemporalMetadata",
                        "description": "Observed, ingested, recorded, and valid time for this record.",
                    },
                    "history": {
                        "type": "array",
                        "description": "Ordered, append-only history of actions that produced this record version. Deliberately carries no `maxItems`: history is never erased or rewritten, and every governance transition appends exactly one event, so any finite inline cap would eventually make a previously valid record impossible to transition -- and raising the cap only postpones that contradiction. Bounding a response's size is a transport/operation concern, handled outside this inline provenance invariant, never by dropping, compacting, or summarising audit history.",
                        "items": {
                            "$ref": "#/$defs/records__ProvenanceEntry",
                        },
                    },
                    "evidence_disposition": {
                        "$ref": "#/$defs/records__EvidenceDisposition",
                        "description": "Whether concrete evidence is actually available for this record. `sources` may be empty only when this disposition explicitly states evidence is unavailable; enforcing that agreement is a semantic-validation concern, not a wire-shape one.",
                    },
                    "sources": {
                        "type": "array",
                        "description": "Sources this record draws on, independent of any single history entry's evidence. May be empty only when `evidence_disposition` explicitly states evidence is unavailable.",
                        "items": {
                            "$ref": "#/$defs/records__SourceReference",
                        },
                        "maxItems": 256,
                    },
                    "assertion": {
                        "$ref": "#/$defs/records__CandidateAssertion",
                        "description": "Who asserted the claim this version carries, when, on what evidence, and the validity window they proposed. Preserved verbatim from the `memory.create` or `record.supersede` input that supplied the claim, so candidate/replacement lineage survives every governance transition.",
                    },
                    "extraction": {
                        "$ref": "#/$defs/records__CandidateExtractionMetadata",
                        "description": "Provenance of the automated extractor that produced the claim this version carries, when one did. Absent for a claim a human asserted directly.",
                    },
                },
                "required": [
                    "identity",
                    "temporal",
                    "history",
                    "evidence_disposition",
                    "sources",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordTemporalMetadata": {
                "title": "RecordTemporalMetadata",
                "description": "The distinct instants a governed record's lifecycle turns on: when the underlying fact occurred in the world, when it was observed, when the system ingested it, when this version was persisted, the window it is asserted valid for, and when it was superseded.",
                "type": "object",
                "properties": {
                    "event_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the underlying fact occurred in the world (source/event time), when distinguishable from `observed_at`.",
                    },
                    "observed_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the underlying fact was observed to be true in the world, when known.",
                    },
                    "ingested_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the system first ingested the fact behind this record.",
                    },
                    "recorded_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this specific version was persisted.",
                    },
                    "valid_from": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Start of the window this record is asserted valid for, when bounded.",
                    },
                    "valid_until": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "End of the window this record is asserted valid for, when bounded.",
                    },
                    "superseded_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this record version was superseded by a newer version, present only once superseded.",
                    },
                },
                "required": [
                    "ingested_at",
                    "recorded_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordVersion": {
                "title": "RecordVersion",
                "description": "Opaque, server-issued version marker of one specific revision of a record. Clients must round-trip it verbatim and must never parse it.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "records__RecordVersionReference": {
                "title": "RecordVersionReference",
                "description": "A precise, non-directional pointer to one exact record version: both `record_id` and `version` are always required. Distinct from `SupersessionReference`, which is direction-bearing (its meaning comes from which `RecordIdentity` field carries it) and whose `version` is optional. Used wherever a payload must name a specific existing record version -- a graph traversal start point or edge endpoint, a context pack citation or source-version list -- without asserting any supersession relationship.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier of the referenced record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "The exact referenced version.",
                    },
                },
                "required": [
                    "record_id",
                    "version",
                ],
                "unevaluatedProperties": False,
            },
            "records__SourceKind": {
                "title": "SourceKind",
                "description": "Open, dot-namespaced code naming the kind of thing a source reference points at, such as `document` or `conversation` or `api_response`.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__SourceReference": {
                "title": "SourceReference",
                "description": "A pointer to the external or internal thing a record's claim came from.",
                "type": "object",
                "properties": {
                    "kind": {
                        "$ref": "#/$defs/records__SourceKind",
                        "description": "What kind of thing this reference points at.",
                    },
                    "source_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the source within its own system of record.",
                    },
                    "locator": {
                        "type": "string",
                        "description": "Optional locator within the source, such as a path, offset, or message id.",
                        "maxLength": 2048,
                    },
                    "retrieved_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the source was read to produce the record it supports.",
                    },
                },
                "required": [
                    "kind",
                    "source_id",
                ],
                "unevaluatedProperties": False,
            },
            "records__SourceSpan": {
                "title": "SourceSpan",
                "description": "An addressable position within a source: a pointer plus an optional character span, so evidence can be pinpointed within a source rather than only referencing the source as a whole.",
                "type": "object",
                "properties": {
                    "pointer": {
                        "type": "string",
                        "description": "Locator within the source, such as a JSON Pointer, XPath, byte offset path, or line reference.",
                        "maxLength": 2048,
                    },
                    "start_offset": {
                        "type": "integer",
                        "description": "Start of the span, in characters from the start of the pointed-at unit, when known.",
                        "minimum": 0,
                    },
                    "end_offset": {
                        "type": "integer",
                        "description": "End of the span, in characters from the start of the pointed-at unit, when known.",
                        "minimum": 0,
                    },
                },
                "required": [
                    "pointer",
                ],
                "unevaluatedProperties": False,
            },
            "records__SupersessionReference": {
                "title": "SupersessionReference",
                "description": "A direction-neutral pointer from one record version to another related record version. The direction of the relationship comes entirely from which field on `RecordIdentity` carries it (`supersedes` vs `superseded_by`); this DTO itself states only which record and version, and why.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier of the related record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "The specific related version, when known.",
                    },
                    "reason": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming why this supersession relationship exists.",
                    },
                },
                "required": [
                    "record_id",
                ],
                "unevaluatedProperties": False,
            },
        },
    },
    "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/KnowledgeSearchInput": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "KnowledgeSearchInput",
        "description": "Input for `knowledge.search`. Workspace-scoped: the workspace is the request envelope's selected workspace; this payload never carries a second, independent workspace identifier. Absent `view` defaults to `current_canonical`; only an explicit `view` selector may request `candidates` or `history`, so a caller can never receive candidate, rejected, superseded, or otherwise non-canonical governed knowledge by omission.",
        "type": "object",
        "properties": {
            "query": {
                "$ref": "#/$defs/memory__MemoryQuery",
                "description": "Normalized search query.",
            },
            "order": {
                "$ref": "#/$defs/memory__MemorySearchOrder",
                "description": "Requested result order. Absent means the server's default order.",
            },
            "view": {
                "$ref": "#/$defs/memory__GovernedRecordView",
                "description": "Which slice of records' versions to consider. Absent defaults to `current_canonical`; requesting `candidates` or `history` requires this field to be set explicitly.",
            },
            "record_type": {
                "$ref": "#/$defs/memory__GovernedRecordType",
                "description": "Restrict results to this record type, when set.",
            },
            "domain_scope": {
                "$ref": "#/$defs/memory__RecordDomainScope",
                "description": "Restrict results to this domain scope, when set.",
            },
            "limit": {
                "$ref": "#/$defs/common__PageLimit",
                "description": "Bounded maximum number of records to return in this page.",
            },
            "page": {
                "$ref": "#/$defs/common__PageMetadata",
                "description": "Continuation position from a prior page, when paging.",
            },
        },
        "required": [
            "query",
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "common__OpaqueToken": {
                "title": "OpaqueToken",
                "description": "A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never parse it. The pattern's trailing negative lookahead is an end-of-input assertion, not a widening of the character domain: a bare `$` matches before a final line terminator in some conforming regex engines, so a token spelled with a trailing newline would be schema-valid while the semantic validators -- which match the whole string -- refuse it. The lookahead pins the anchor to absolute end of input, so strict schema and semantic validation accept exactly the same tokens.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__PageLimit": {
                "title": "PageLimit",
                "description": "A bounded positive page size a caller requests for a paginated read.",
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
            },
            "common__PageMetadata": {
                "title": "PageMetadata",
                "description": "A pagination position. Direction-neutral: the same shape is read differently on a request than on a result, and neither reading is the other's default. On a request, an absent `page` asks for the first page, and a present `page` must actually name a continuation token -- `{}` states nothing to continue from and is invalid. On a result, `page` is always present and states the position this read reached: a continuation token means more remains, and `{}` means the read is exhausted. Exhaustion is therefore stated, never implied by an absent field -- one spelling on every paginated result, so a caller never has to know which result type it is holding to know what 'no next page' looks like. Token issuance, encoding, expiry, and the bindings a token proves are deliberately out of scope here; a token is opaque, and a reader that needs to prove what one was bound to takes that binding as separate trusted input rather than parsing the token.",
                "type": "object",
                "properties": {
                    "continuation_token": {
                        "$ref": "#/$defs/common__OpaqueToken",
                        "description": "Opaque cursor. On a request, the position to continue from; on a result, the position the next page starts at. Absent on a result means the read is exhausted, which is why an exhausted result still carries `page` as `{}` rather than dropping the field.",
                    },
                },
                "required": [],
                "unevaluatedProperties": False,
            },
            "memory__GovernedRecordType": {
                "title": "GovernedRecordType",
                "description": "Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact` or `memory.entity` or `memory.relation`. Open by design so a compatible minor release can add record types without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__GovernedRecordView": {
                "title": "GovernedRecordView",
                "description": "Open, dot-namespaced code selecting which slice of a governed record's versions a read considers: `current_canonical` (the single active accepted version, the default when this field is absent), `candidates` (proposed/candidate versions not yet accepted), or `history` (every version, including superseded ones). Open by design so a compatible minor release can add views without breaking existing decoders. Default resolution when absent is a semantic concern (see `omnivia_core.contracts.v1.semantics`), not a wire-shape one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__MemoryQuery": {
                "title": "MemoryQuery",
                "description": "A caller-supplied, normalized search query for `memory.search`. Normalization (case-folding, whitespace, tokenization) is caller-side; this document defines no normalization algorithm.",
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
            },
            "memory__MemorySearchOrder": {
                "title": "MemorySearchOrder",
                "description": "Open, dot-namespaced code naming how `memory.search` results are ordered, such as `relevance` or `recency`. Open by design so a compatible minor release can add orders without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__RecordDomainScope": {
                "title": "RecordDomainScope",
                "description": "Open, bounded, non-empty, dot-namespaced record classification stating what domain a governed record belongs to, such as `personal.preferences` or `project.roadmap`. Distinct from the caller-authorization `Scope` vocabulary (e.g. `memory:read`): a domain scope never grants or checks a permission, it only classifies what the record is about. Open by design so a compatible minor release can add classifications without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
        },
    },
    "https://contracts.omnivia.dev/application/v1/knowledge.schema.json#/$defs/KnowledgeSearchResult": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "KnowledgeSearchResult",
        "description": "Result of `knowledge.search`. When the request's `view` was absent or `current_canonical`, every returned record must be the exact accepted, current, canonical version; no candidate, rejected, superseded, or non-canonical-layer record may appear.",
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "description": "Governed records in this page, ordered per the request's `order`.",
                "items": {
                    "$ref": "#/$defs/memory__GovernedRecord",
                },
                "maxItems": 500,
            },
            "page": {
                "$ref": "#/$defs/common__PageMetadata",
                "description": "Continuation position for the next page, absent on the last page.",
            },
        },
        "required": [
            "records",
            "page",
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "common__Identifier": {
                "title": "Identifier",
                "description": "Generic bounded, non-empty identifier used for clients, principals, roles, and deprecations.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "common__JsonObject": {
                "title": "JsonObject",
                "description": "An opaque JSON object. The envelope carries domain payloads without inspecting them, which is a statement about the envelope rather than about the payload: an operation's `input` and `result` are each bound to their own definition by `operations.schema.json`'s `x-omnivia-operation-catalogue` (`input_schema_ref` and `result_schema_ref`), and validating a payload against that binding is a separate step from decoding the envelope carrying it.",
                "type": "object",
            },
            "common__OpaqueToken": {
                "title": "OpaqueToken",
                "description": "A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never parse it. The pattern's trailing negative lookahead is an end-of-input assertion, not a widening of the character domain: a bare `$` matches before a final line terminator in some conforming regex engines, so a token spelled with a trailing newline would be schema-valid while the semantic validators -- which match the whole string -- refuse it. The lookahead pins the anchor to absolute end of input, so strict schema and semantic validation accept exactly the same tokens.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__OpenCode": {
                "title": "OpenCode",
                "description": "An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible minor releases can add vocabulary; consumers must preserve values they do not recognize.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "common__PageMetadata": {
                "title": "PageMetadata",
                "description": "A pagination position. Direction-neutral: the same shape is read differently on a request than on a result, and neither reading is the other's default. On a request, an absent `page` asks for the first page, and a present `page` must actually name a continuation token -- `{}` states nothing to continue from and is invalid. On a result, `page` is always present and states the position this read reached: a continuation token means more remains, and `{}` means the read is exhausted. Exhaustion is therefore stated, never implied by an absent field -- one spelling on every paginated result, so a caller never has to know which result type it is holding to know what 'no next page' looks like. Token issuance, encoding, expiry, and the bindings a token proves are deliberately out of scope here; a token is opaque, and a reader that needs to prove what one was bound to takes that binding as separate trusted input rather than parsing the token.",
                "type": "object",
                "properties": {
                    "continuation_token": {
                        "$ref": "#/$defs/common__OpaqueToken",
                        "description": "Opaque cursor. On a request, the position to continue from; on a result, the position the next page starts at. Absent on a result means the read is exhausted, which is why an exhausted result still carries `page` as `{}` rather than dropping the field.",
                    },
                },
                "required": [],
                "unevaluatedProperties": False,
            },
            "common__Timestamp": {
                "title": "Timestamp",
                "description": "An RFC 3339 timestamp in UTC with a literal `Z` offset.",
                "type": "string",
                "format": "date-time",
                "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$(?![\\s\\S])",
                "maxLength": 40,
            },
            "common__WorkspaceId": {
                "title": "WorkspaceId",
                "description": "Bounded, non-empty identifier of the workspace a request is scoped to.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "memory__GovernedRecord": {
                "title": "GovernedRecord",
                "description": "A provider-neutral governed record: which workspace it belongs to, what kind of record it is, its domain scope and authority level, its full L0-L4 governance, temporal, evidence, and provenance envelope, and its opaque JSON content. Carries no reference to, and is not a substitute for, any repo-local `Memory`, `MemoryFact`, or `SourceRef` domain class.",
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "$ref": "#/$defs/common__WorkspaceId",
                        "description": "Workspace this record belongs to.",
                    },
                    "record_type": {
                        "$ref": "#/$defs/memory__GovernedRecordType",
                        "description": "What kind of governed record this is.",
                    },
                    "domain_scope": {
                        "$ref": "#/$defs/memory__RecordDomainScope",
                        "description": "Non-empty domain/record classification this record is filed under. Every governed record carries exactly one; a caller may propose one through `memory.create`, but the server is always the final authority on what is actually stored here. Distinct from caller-authorization `Scope`.",
                    },
                    "authority_level": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the authority level this record's governance decision currently carries, such as `proposed` or `reviewed` or `canonical`. Server-owned: no `memory.create` input field lets a caller assert this directly.",
                    },
                    "reviewer": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the reviewer or policy that produced this record's current governance decision, when one has been recorded. Absent when no reviewer/policy decision applies yet, such as a freshly proposed record.",
                    },
                    "provenance": {
                        "$ref": "#/$defs/records__RecordProvenance",
                        "description": "Identity, governance layer/state/currentness, temporal metadata, history, and evidence for this record version.",
                    },
                    "content": {
                        "$ref": "#/$defs/common__JsonObject",
                        "description": "Opaque governed content this record carries.",
                    },
                },
                "required": [
                    "workspace_id",
                    "record_type",
                    "domain_scope",
                    "authority_level",
                    "provenance",
                    "content",
                ],
                "unevaluatedProperties": False,
            },
            "memory__GovernedRecordType": {
                "title": "GovernedRecordType",
                "description": "Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact` or `memory.entity` or `memory.relation`. Open by design so a compatible minor release can add record types without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__RecordDomainScope": {
                "title": "RecordDomainScope",
                "description": "Open, bounded, non-empty, dot-namespaced record classification stating what domain a governed record belongs to, such as `personal.preferences` or `project.roadmap`. Distinct from the caller-authorization `Scope` vocabulary (e.g. `memory:read`): a domain scope never grants or checks a permission, it only classifies what the record is about. Open by design so a compatible minor release can add classifications without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__CandidateAssertion": {
                "title": "CandidateAssertion",
                "description": "Who is asserting a governed record's claim, when, and on what evidence, plus the validity window they propose for it. This is caller-supplied provenance for the claim -- carried into `memory.create` and `record.supersede` inputs, and preserved on the resulting record's `RecordProvenance` -- not the server-owned governance decision: it never carries authority level, reviewer/policy identity, or any other field a least-authority-escalating mutation input is forbidden from carrying. Defined here rather than in `memory.schema.json` so `RecordProvenance` can preserve it without `records.schema.json` depending on a document that already depends on it.",
                "type": "object",
                "properties": {
                    "actor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Principal or system asserting this candidate.",
                    },
                    "actor_kind": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.",
                    },
                    "actor_role": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the role the actor asserted this candidate under.",
                    },
                    "asserted_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the actor asserted this candidate.",
                    },
                    "proposed_valid_from": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Start of the validity window the caller proposes for this candidate, when known. The server remains the final authority on the validity window actually stored.",
                    },
                    "proposed_valid_until": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "End of the validity window the caller proposes for this candidate, when bounded. The server remains the final authority on the validity window actually stored.",
                    },
                    "evidence": {
                        "type": "array",
                        "description": "Concrete evidence substantiating this assertion. May be empty only when the enclosing input's `evidence_disposition` explicitly excuses it; enforcing that agreement is a semantic-validation concern, not a wire-shape one.",
                        "items": {
                            "$ref": "#/$defs/records__EvidenceReference",
                        },
                        "maxItems": 256,
                    },
                },
                "required": [
                    "actor_id",
                    "actor_kind",
                    "actor_role",
                    "asserted_at",
                    "evidence",
                ],
                "unevaluatedProperties": False,
            },
            "records__CandidateExtractionMetadata": {
                "title": "CandidateExtractionMetadata",
                "description": "Optional provenance about the automated extractor that produced a governed record's claim, when one did. Absent entirely for a claim a human asserted directly. Defined here rather than in `memory.schema.json` so `RecordProvenance` can preserve it without `records.schema.json` depending on a document that already depends on it.",
                "type": "object",
                "properties": {
                    "extractor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the extractor that produced this candidate.",
                    },
                    "extractor_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the extractor that produced this candidate, when known.",
                    },
                    "model_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the model the extractor used, when known.",
                    },
                    "prompt_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the prompt the extractor used, when known.",
                    },
                    "extracted_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the extractor produced this candidate.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "The extractor's self-reported confidence in this candidate, on a 0-1 scale, when known.",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "reconciliation_state": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming this candidate's reconciliation/deduplication state against prior extractions, such as `novel` or `duplicate` or `merged`, when the extractor determined one. Open by design; an unrecognized value must be preserved, not coerced to a known one, and must never widen this candidate's authority.",
                    },
                },
                "required": [
                    "extractor_id",
                    "extracted_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__EvidenceDisposition": {
                "title": "EvidenceDisposition",
                "description": "Open, dot-namespaced code stating whether concrete evidence is actually available for a record, such as `available` or `unavailable` or `redacted`. Open by design; an unrecognized value must be preserved, not coerced to a known one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__EvidenceReference": {
                "title": "EvidenceReference",
                "description": "A concrete piece of evidence supporting one claim in a record.",
                "type": "object",
                "properties": {
                    "source": {
                        "$ref": "#/$defs/records__SourceReference",
                        "description": "The source this evidence was drawn from.",
                    },
                    "span": {
                        "$ref": "#/$defs/records__SourceSpan",
                        "description": "Addressable position within the source this evidence was drawn from, when known.",
                    },
                    "excerpt": {
                        "type": "string",
                        "description": "Optional short excerpt from the source substantiating the claim. Not a stable interface.",
                        "maxLength": 4096,
                    },
                },
                "required": [
                    "source",
                ],
                "unevaluatedProperties": False,
            },
            "records__GovernanceLayer": {
                "title": "GovernanceLayer",
                "description": "Open, dot-namespaced code naming the knowledge-governance layer a record belongs to: `l0` (raw evidence), `l1` (candidate observations), `l2` (governed records / canonical knowledge), `l3` (context models), or `l4` (organisational model). Distinct from workspace scope, which is a caller-facing tenancy boundary, not a knowledge-governance layer. Open by design so a compatible minor release can add layers without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__GovernanceState": {
                "title": "GovernanceState",
                "description": "Open, dot-namespaced code naming a record's position in its own governance workflow, such as `proposed` or `candidate` or `accepted` or `rejected`. Distinct from `GovernanceLayer` (which namespace a record belongs to) and `RecordCurrentness` (whether this version is the active one): a record can be `accepted` and still later superseded, or `proposed` and never adopted. Open by design so a compatible minor release can add states without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__ProvenanceEntry": {
                "title": "ProvenanceEntry",
                "description": "One step in a record's history: who or what did what, when, and -- for a governance transition -- the explicit rationale it was taken under.",
                "type": "object",
                "properties": {
                    "actor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Principal or system that performed this action.",
                    },
                    "actor_kind": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.",
                    },
                    "action": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming what happened, such as `created` or `modified` or `superseded`.",
                    },
                    "occurred_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this action occurred.",
                    },
                    "reason_code": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming why this action was taken, carried over verbatim from the `GovernanceRationale.reason_code` the transition was requested under. Absent on ordinary non-governance history, which never carries a rationale; a governance-transition event must carry it, and requiring that is a semantic-validation concern, not a wire-shape one.",
                    },
                    "reason_comment": {
                        "type": "string",
                        "description": "Bounded human-readable elaboration, carried over verbatim from the requesting `GovernanceRationale.comment`. Absent exactly when that comment was absent. Not a stable interface.",
                        "maxLength": 2048,
                    },
                    "evidence": {
                        "type": "array",
                        "description": "Evidence supporting this action, when applicable. Bounded at the same 256 items `CandidateAssertion.evidence` is, and deliberately so: a `record.supersede` transition appends exactly one event whose evidence must equal the replacement claim's complete assertion evidence, so a lower bound here would make an otherwise valid replacement impossible to record.",
                        "items": {
                            "$ref": "#/$defs/records__EvidenceReference",
                        },
                        "maxItems": 256,
                    },
                },
                "required": [
                    "actor_id",
                    "actor_kind",
                    "action",
                    "occurred_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordCurrentness": {
                "title": "RecordCurrentness",
                "description": "Open, dot-namespaced code naming whether a record version is the active one, such as `current` or `superseded` or `retracted`. Open by design; an unrecognized value must be preserved, not coerced to a known one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__RecordId": {
                "title": "RecordId",
                "description": "Stable identifier of a governed record, constant across every version of that record.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "records__RecordIdentity": {
                "title": "RecordIdentity",
                "description": "The identity, version, governance layer, governance state, and currentness of one record version.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier stable across every version of this record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "Opaque version of this specific revision.",
                    },
                    "layer": {
                        "$ref": "#/$defs/records__GovernanceLayer",
                        "description": "Governance layer this record belongs to.",
                    },
                    "governance_state": {
                        "$ref": "#/$defs/records__GovernanceState",
                        "description": "This record version's position in its own governance workflow, independent of `layer` and `currentness`.",
                    },
                    "currentness": {
                        "$ref": "#/$defs/records__RecordCurrentness",
                        "description": "Whether this version is the active one.",
                    },
                    "supersedes": {
                        "$ref": "#/$defs/records__SupersessionReference",
                        "description": "The earlier record version this version replaces, when this version is itself the newer one.",
                    },
                    "superseded_by": {
                        "$ref": "#/$defs/records__SupersessionReference",
                        "description": "The newer record version that replaced this one, present when `currentness` marks this version as superseded.",
                    },
                },
                "required": [
                    "record_id",
                    "version",
                    "layer",
                    "governance_state",
                    "currentness",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordProvenance": {
                "title": "RecordProvenance",
                "description": "The full provenance envelope for one record version: identity, temporal metadata, its authoring history, the sources it draws on, and the caller-supplied assertion/extraction lineage the claim in this version came from. `assertion`/`extraction` are structurally optional so a record written before they existed still decodes, but a governance transition that replaces or carries forward a claim must bind them; enforcing that is a semantic-validation concern, not a wire-shape one.",
                "type": "object",
                "properties": {
                    "identity": {
                        "$ref": "#/$defs/records__RecordIdentity",
                        "description": "Identity, version, governance layer, and currentness of this record.",
                    },
                    "temporal": {
                        "$ref": "#/$defs/records__RecordTemporalMetadata",
                        "description": "Observed, ingested, recorded, and valid time for this record.",
                    },
                    "history": {
                        "type": "array",
                        "description": "Ordered, append-only history of actions that produced this record version. Deliberately carries no `maxItems`: history is never erased or rewritten, and every governance transition appends exactly one event, so any finite inline cap would eventually make a previously valid record impossible to transition -- and raising the cap only postpones that contradiction. Bounding a response's size is a transport/operation concern, handled outside this inline provenance invariant, never by dropping, compacting, or summarising audit history.",
                        "items": {
                            "$ref": "#/$defs/records__ProvenanceEntry",
                        },
                    },
                    "evidence_disposition": {
                        "$ref": "#/$defs/records__EvidenceDisposition",
                        "description": "Whether concrete evidence is actually available for this record. `sources` may be empty only when this disposition explicitly states evidence is unavailable; enforcing that agreement is a semantic-validation concern, not a wire-shape one.",
                    },
                    "sources": {
                        "type": "array",
                        "description": "Sources this record draws on, independent of any single history entry's evidence. May be empty only when `evidence_disposition` explicitly states evidence is unavailable.",
                        "items": {
                            "$ref": "#/$defs/records__SourceReference",
                        },
                        "maxItems": 256,
                    },
                    "assertion": {
                        "$ref": "#/$defs/records__CandidateAssertion",
                        "description": "Who asserted the claim this version carries, when, on what evidence, and the validity window they proposed. Preserved verbatim from the `memory.create` or `record.supersede` input that supplied the claim, so candidate/replacement lineage survives every governance transition.",
                    },
                    "extraction": {
                        "$ref": "#/$defs/records__CandidateExtractionMetadata",
                        "description": "Provenance of the automated extractor that produced the claim this version carries, when one did. Absent for a claim a human asserted directly.",
                    },
                },
                "required": [
                    "identity",
                    "temporal",
                    "history",
                    "evidence_disposition",
                    "sources",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordTemporalMetadata": {
                "title": "RecordTemporalMetadata",
                "description": "The distinct instants a governed record's lifecycle turns on: when the underlying fact occurred in the world, when it was observed, when the system ingested it, when this version was persisted, the window it is asserted valid for, and when it was superseded.",
                "type": "object",
                "properties": {
                    "event_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the underlying fact occurred in the world (source/event time), when distinguishable from `observed_at`.",
                    },
                    "observed_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the underlying fact was observed to be true in the world, when known.",
                    },
                    "ingested_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the system first ingested the fact behind this record.",
                    },
                    "recorded_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this specific version was persisted.",
                    },
                    "valid_from": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Start of the window this record is asserted valid for, when bounded.",
                    },
                    "valid_until": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "End of the window this record is asserted valid for, when bounded.",
                    },
                    "superseded_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this record version was superseded by a newer version, present only once superseded.",
                    },
                },
                "required": [
                    "ingested_at",
                    "recorded_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordVersion": {
                "title": "RecordVersion",
                "description": "Opaque, server-issued version marker of one specific revision of a record. Clients must round-trip it verbatim and must never parse it.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "records__SourceKind": {
                "title": "SourceKind",
                "description": "Open, dot-namespaced code naming the kind of thing a source reference points at, such as `document` or `conversation` or `api_response`.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__SourceReference": {
                "title": "SourceReference",
                "description": "A pointer to the external or internal thing a record's claim came from.",
                "type": "object",
                "properties": {
                    "kind": {
                        "$ref": "#/$defs/records__SourceKind",
                        "description": "What kind of thing this reference points at.",
                    },
                    "source_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the source within its own system of record.",
                    },
                    "locator": {
                        "type": "string",
                        "description": "Optional locator within the source, such as a path, offset, or message id.",
                        "maxLength": 2048,
                    },
                    "retrieved_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the source was read to produce the record it supports.",
                    },
                },
                "required": [
                    "kind",
                    "source_id",
                ],
                "unevaluatedProperties": False,
            },
            "records__SourceSpan": {
                "title": "SourceSpan",
                "description": "An addressable position within a source: a pointer plus an optional character span, so evidence can be pinpointed within a source rather than only referencing the source as a whole.",
                "type": "object",
                "properties": {
                    "pointer": {
                        "type": "string",
                        "description": "Locator within the source, such as a JSON Pointer, XPath, byte offset path, or line reference.",
                        "maxLength": 2048,
                    },
                    "start_offset": {
                        "type": "integer",
                        "description": "Start of the span, in characters from the start of the pointed-at unit, when known.",
                        "minimum": 0,
                    },
                    "end_offset": {
                        "type": "integer",
                        "description": "End of the span, in characters from the start of the pointed-at unit, when known.",
                        "minimum": 0,
                    },
                },
                "required": [
                    "pointer",
                ],
                "unevaluatedProperties": False,
            },
            "records__SupersessionReference": {
                "title": "SupersessionReference",
                "description": "A direction-neutral pointer from one record version to another related record version. The direction of the relationship comes entirely from which field on `RecordIdentity` carries it (`supersedes` vs `superseded_by`); this DTO itself states only which record and version, and why.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier of the related record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "The specific related version, when known.",
                    },
                    "reason": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming why this supersession relationship exists.",
                    },
                },
                "required": [
                    "record_id",
                ],
                "unevaluatedProperties": False,
            },
        },
    },
    "https://contracts.omnivia.dev/application/v1/memory.schema.json#/$defs/MemorySearchInput": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MemorySearchInput",
        "description": "Input for `memory.search`. Workspace-scoped: the workspace is the request envelope's selected workspace; this payload never carries a second, independent workspace identifier. Carries the normalized query and requested order a later stateful conformance slice will bind an issued continuation token to, alongside principal/workspace/operation binding.",
        "type": "object",
        "properties": {
            "query": {
                "$ref": "#/$defs/memory__MemoryQuery",
                "description": "Normalized search query.",
            },
            "order": {
                "$ref": "#/$defs/memory__MemorySearchOrder",
                "description": "Requested result order. Absent means the server's default order.",
            },
            "view": {
                "$ref": "#/$defs/memory__GovernedRecordView",
                "description": "Which slice of records' versions to consider. Absent defaults to `current_canonical`.",
            },
            "record_type": {
                "$ref": "#/$defs/memory__GovernedRecordType",
                "description": "Restrict results to this record type, when set.",
            },
            "limit": {
                "$ref": "#/$defs/common__PageLimit",
                "description": "Bounded maximum number of records to return in this page.",
            },
            "page": {
                "$ref": "#/$defs/common__PageMetadata",
                "description": "Continuation position from a prior page, when paging.",
            },
        },
        "required": [
            "query",
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "common__OpaqueToken": {
                "title": "OpaqueToken",
                "description": "A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never parse it. The pattern's trailing negative lookahead is an end-of-input assertion, not a widening of the character domain: a bare `$` matches before a final line terminator in some conforming regex engines, so a token spelled with a trailing newline would be schema-valid while the semantic validators -- which match the whole string -- refuse it. The lookahead pins the anchor to absolute end of input, so strict schema and semantic validation accept exactly the same tokens.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__PageLimit": {
                "title": "PageLimit",
                "description": "A bounded positive page size a caller requests for a paginated read.",
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
            },
            "common__PageMetadata": {
                "title": "PageMetadata",
                "description": "A pagination position. Direction-neutral: the same shape is read differently on a request than on a result, and neither reading is the other's default. On a request, an absent `page` asks for the first page, and a present `page` must actually name a continuation token -- `{}` states nothing to continue from and is invalid. On a result, `page` is always present and states the position this read reached: a continuation token means more remains, and `{}` means the read is exhausted. Exhaustion is therefore stated, never implied by an absent field -- one spelling on every paginated result, so a caller never has to know which result type it is holding to know what 'no next page' looks like. Token issuance, encoding, expiry, and the bindings a token proves are deliberately out of scope here; a token is opaque, and a reader that needs to prove what one was bound to takes that binding as separate trusted input rather than parsing the token.",
                "type": "object",
                "properties": {
                    "continuation_token": {
                        "$ref": "#/$defs/common__OpaqueToken",
                        "description": "Opaque cursor. On a request, the position to continue from; on a result, the position the next page starts at. Absent on a result means the read is exhausted, which is why an exhausted result still carries `page` as `{}` rather than dropping the field.",
                    },
                },
                "required": [],
                "unevaluatedProperties": False,
            },
            "memory__GovernedRecordType": {
                "title": "GovernedRecordType",
                "description": "Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact` or `memory.entity` or `memory.relation`. Open by design so a compatible minor release can add record types without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__GovernedRecordView": {
                "title": "GovernedRecordView",
                "description": "Open, dot-namespaced code selecting which slice of a governed record's versions a read considers: `current_canonical` (the single active accepted version, the default when this field is absent), `candidates` (proposed/candidate versions not yet accepted), or `history` (every version, including superseded ones). Open by design so a compatible minor release can add views without breaking existing decoders. Default resolution when absent is a semantic concern (see `omnivia_core.contracts.v1.semantics`), not a wire-shape one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__MemoryQuery": {
                "title": "MemoryQuery",
                "description": "A caller-supplied, normalized search query for `memory.search`. Normalization (case-folding, whitespace, tokenization) is caller-side; this document defines no normalization algorithm.",
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
            },
            "memory__MemorySearchOrder": {
                "title": "MemorySearchOrder",
                "description": "Open, dot-namespaced code naming how `memory.search` results are ordered, such as `relevance` or `recency`. Open by design so a compatible minor release can add orders without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
        },
    },
    "https://contracts.omnivia.dev/application/v1/memory.schema.json#/$defs/MemorySearchResult": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MemorySearchResult",
        "description": "Result of `memory.search`.",
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "description": "Governed records in this page, ordered per the request's `order`.",
                "items": {
                    "$ref": "#/$defs/memory__GovernedRecord",
                },
                "maxItems": 500,
            },
            "page": {
                "$ref": "#/$defs/common__PageMetadata",
                "description": "Continuation position for the next page, absent on the last page.",
            },
        },
        "required": [
            "records",
            "page",
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "common__Identifier": {
                "title": "Identifier",
                "description": "Generic bounded, non-empty identifier used for clients, principals, roles, and deprecations.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "common__JsonObject": {
                "title": "JsonObject",
                "description": "An opaque JSON object. The envelope carries domain payloads without inspecting them, which is a statement about the envelope rather than about the payload: an operation's `input` and `result` are each bound to their own definition by `operations.schema.json`'s `x-omnivia-operation-catalogue` (`input_schema_ref` and `result_schema_ref`), and validating a payload against that binding is a separate step from decoding the envelope carrying it.",
                "type": "object",
            },
            "common__OpaqueToken": {
                "title": "OpaqueToken",
                "description": "A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never parse it. The pattern's trailing negative lookahead is an end-of-input assertion, not a widening of the character domain: a bare `$` matches before a final line terminator in some conforming regex engines, so a token spelled with a trailing newline would be schema-valid while the semantic validators -- which match the whole string -- refuse it. The lookahead pins the anchor to absolute end of input, so strict schema and semantic validation accept exactly the same tokens.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "common__OpenCode": {
                "title": "OpenCode",
                "description": "An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible minor releases can add vocabulary; consumers must preserve values they do not recognize.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "common__PageMetadata": {
                "title": "PageMetadata",
                "description": "A pagination position. Direction-neutral: the same shape is read differently on a request than on a result, and neither reading is the other's default. On a request, an absent `page` asks for the first page, and a present `page` must actually name a continuation token -- `{}` states nothing to continue from and is invalid. On a result, `page` is always present and states the position this read reached: a continuation token means more remains, and `{}` means the read is exhausted. Exhaustion is therefore stated, never implied by an absent field -- one spelling on every paginated result, so a caller never has to know which result type it is holding to know what 'no next page' looks like. Token issuance, encoding, expiry, and the bindings a token proves are deliberately out of scope here; a token is opaque, and a reader that needs to prove what one was bound to takes that binding as separate trusted input rather than parsing the token.",
                "type": "object",
                "properties": {
                    "continuation_token": {
                        "$ref": "#/$defs/common__OpaqueToken",
                        "description": "Opaque cursor. On a request, the position to continue from; on a result, the position the next page starts at. Absent on a result means the read is exhausted, which is why an exhausted result still carries `page` as `{}` rather than dropping the field.",
                    },
                },
                "required": [],
                "unevaluatedProperties": False,
            },
            "common__Timestamp": {
                "title": "Timestamp",
                "description": "An RFC 3339 timestamp in UTC with a literal `Z` offset.",
                "type": "string",
                "format": "date-time",
                "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$(?![\\s\\S])",
                "maxLength": 40,
            },
            "common__WorkspaceId": {
                "title": "WorkspaceId",
                "description": "Bounded, non-empty identifier of the workspace a request is scoped to.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "memory__GovernedRecord": {
                "title": "GovernedRecord",
                "description": "A provider-neutral governed record: which workspace it belongs to, what kind of record it is, its domain scope and authority level, its full L0-L4 governance, temporal, evidence, and provenance envelope, and its opaque JSON content. Carries no reference to, and is not a substitute for, any repo-local `Memory`, `MemoryFact`, or `SourceRef` domain class.",
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "$ref": "#/$defs/common__WorkspaceId",
                        "description": "Workspace this record belongs to.",
                    },
                    "record_type": {
                        "$ref": "#/$defs/memory__GovernedRecordType",
                        "description": "What kind of governed record this is.",
                    },
                    "domain_scope": {
                        "$ref": "#/$defs/memory__RecordDomainScope",
                        "description": "Non-empty domain/record classification this record is filed under. Every governed record carries exactly one; a caller may propose one through `memory.create`, but the server is always the final authority on what is actually stored here. Distinct from caller-authorization `Scope`.",
                    },
                    "authority_level": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the authority level this record's governance decision currently carries, such as `proposed` or `reviewed` or `canonical`. Server-owned: no `memory.create` input field lets a caller assert this directly.",
                    },
                    "reviewer": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the reviewer or policy that produced this record's current governance decision, when one has been recorded. Absent when no reviewer/policy decision applies yet, such as a freshly proposed record.",
                    },
                    "provenance": {
                        "$ref": "#/$defs/records__RecordProvenance",
                        "description": "Identity, governance layer/state/currentness, temporal metadata, history, and evidence for this record version.",
                    },
                    "content": {
                        "$ref": "#/$defs/common__JsonObject",
                        "description": "Opaque governed content this record carries.",
                    },
                },
                "required": [
                    "workspace_id",
                    "record_type",
                    "domain_scope",
                    "authority_level",
                    "provenance",
                    "content",
                ],
                "unevaluatedProperties": False,
            },
            "memory__GovernedRecordType": {
                "title": "GovernedRecordType",
                "description": "Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact` or `memory.entity` or `memory.relation`. Open by design so a compatible minor release can add record types without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "memory__RecordDomainScope": {
                "title": "RecordDomainScope",
                "description": "Open, bounded, non-empty, dot-namespaced record classification stating what domain a governed record belongs to, such as `personal.preferences` or `project.roadmap`. Distinct from the caller-authorization `Scope` vocabulary (e.g. `memory:read`): a domain scope never grants or checks a permission, it only classifies what the record is about. Open by design so a compatible minor release can add classifications without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__CandidateAssertion": {
                "title": "CandidateAssertion",
                "description": "Who is asserting a governed record's claim, when, and on what evidence, plus the validity window they propose for it. This is caller-supplied provenance for the claim -- carried into `memory.create` and `record.supersede` inputs, and preserved on the resulting record's `RecordProvenance` -- not the server-owned governance decision: it never carries authority level, reviewer/policy identity, or any other field a least-authority-escalating mutation input is forbidden from carrying. Defined here rather than in `memory.schema.json` so `RecordProvenance` can preserve it without `records.schema.json` depending on a document that already depends on it.",
                "type": "object",
                "properties": {
                    "actor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Principal or system asserting this candidate.",
                    },
                    "actor_kind": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.",
                    },
                    "actor_role": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the role the actor asserted this candidate under.",
                    },
                    "asserted_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the actor asserted this candidate.",
                    },
                    "proposed_valid_from": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Start of the validity window the caller proposes for this candidate, when known. The server remains the final authority on the validity window actually stored.",
                    },
                    "proposed_valid_until": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "End of the validity window the caller proposes for this candidate, when bounded. The server remains the final authority on the validity window actually stored.",
                    },
                    "evidence": {
                        "type": "array",
                        "description": "Concrete evidence substantiating this assertion. May be empty only when the enclosing input's `evidence_disposition` explicitly excuses it; enforcing that agreement is a semantic-validation concern, not a wire-shape one.",
                        "items": {
                            "$ref": "#/$defs/records__EvidenceReference",
                        },
                        "maxItems": 256,
                    },
                },
                "required": [
                    "actor_id",
                    "actor_kind",
                    "actor_role",
                    "asserted_at",
                    "evidence",
                ],
                "unevaluatedProperties": False,
            },
            "records__CandidateExtractionMetadata": {
                "title": "CandidateExtractionMetadata",
                "description": "Optional provenance about the automated extractor that produced a governed record's claim, when one did. Absent entirely for a claim a human asserted directly. Defined here rather than in `memory.schema.json` so `RecordProvenance` can preserve it without `records.schema.json` depending on a document that already depends on it.",
                "type": "object",
                "properties": {
                    "extractor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the extractor that produced this candidate.",
                    },
                    "extractor_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the extractor that produced this candidate, when known.",
                    },
                    "model_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the model the extractor used, when known.",
                    },
                    "prompt_version": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Version of the prompt the extractor used, when known.",
                    },
                    "extracted_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the extractor produced this candidate.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "The extractor's self-reported confidence in this candidate, on a 0-1 scale, when known.",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "reconciliation_state": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming this candidate's reconciliation/deduplication state against prior extractions, such as `novel` or `duplicate` or `merged`, when the extractor determined one. Open by design; an unrecognized value must be preserved, not coerced to a known one, and must never widen this candidate's authority.",
                    },
                },
                "required": [
                    "extractor_id",
                    "extracted_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__EvidenceDisposition": {
                "title": "EvidenceDisposition",
                "description": "Open, dot-namespaced code stating whether concrete evidence is actually available for a record, such as `available` or `unavailable` or `redacted`. Open by design; an unrecognized value must be preserved, not coerced to a known one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__EvidenceReference": {
                "title": "EvidenceReference",
                "description": "A concrete piece of evidence supporting one claim in a record.",
                "type": "object",
                "properties": {
                    "source": {
                        "$ref": "#/$defs/records__SourceReference",
                        "description": "The source this evidence was drawn from.",
                    },
                    "span": {
                        "$ref": "#/$defs/records__SourceSpan",
                        "description": "Addressable position within the source this evidence was drawn from, when known.",
                    },
                    "excerpt": {
                        "type": "string",
                        "description": "Optional short excerpt from the source substantiating the claim. Not a stable interface.",
                        "maxLength": 4096,
                    },
                },
                "required": [
                    "source",
                ],
                "unevaluatedProperties": False,
            },
            "records__GovernanceLayer": {
                "title": "GovernanceLayer",
                "description": "Open, dot-namespaced code naming the knowledge-governance layer a record belongs to: `l0` (raw evidence), `l1` (candidate observations), `l2` (governed records / canonical knowledge), `l3` (context models), or `l4` (organisational model). Distinct from workspace scope, which is a caller-facing tenancy boundary, not a knowledge-governance layer. Open by design so a compatible minor release can add layers without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__GovernanceState": {
                "title": "GovernanceState",
                "description": "Open, dot-namespaced code naming a record's position in its own governance workflow, such as `proposed` or `candidate` or `accepted` or `rejected`. Distinct from `GovernanceLayer` (which namespace a record belongs to) and `RecordCurrentness` (whether this version is the active one): a record can be `accepted` and still later superseded, or `proposed` and never adopted. Open by design so a compatible minor release can add states without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__ProvenanceEntry": {
                "title": "ProvenanceEntry",
                "description": "One step in a record's history: who or what did what, when, and -- for a governance transition -- the explicit rationale it was taken under.",
                "type": "object",
                "properties": {
                    "actor_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Principal or system that performed this action.",
                    },
                    "actor_kind": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming the kind of actor, such as `user` or `agent` or `ingestion_pipeline`.",
                    },
                    "action": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming what happened, such as `created` or `modified` or `superseded`.",
                    },
                    "occurred_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this action occurred.",
                    },
                    "reason_code": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming why this action was taken, carried over verbatim from the `GovernanceRationale.reason_code` the transition was requested under. Absent on ordinary non-governance history, which never carries a rationale; a governance-transition event must carry it, and requiring that is a semantic-validation concern, not a wire-shape one.",
                    },
                    "reason_comment": {
                        "type": "string",
                        "description": "Bounded human-readable elaboration, carried over verbatim from the requesting `GovernanceRationale.comment`. Absent exactly when that comment was absent. Not a stable interface.",
                        "maxLength": 2048,
                    },
                    "evidence": {
                        "type": "array",
                        "description": "Evidence supporting this action, when applicable. Bounded at the same 256 items `CandidateAssertion.evidence` is, and deliberately so: a `record.supersede` transition appends exactly one event whose evidence must equal the replacement claim's complete assertion evidence, so a lower bound here would make an otherwise valid replacement impossible to record.",
                        "items": {
                            "$ref": "#/$defs/records__EvidenceReference",
                        },
                        "maxItems": 256,
                    },
                },
                "required": [
                    "actor_id",
                    "actor_kind",
                    "action",
                    "occurred_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordCurrentness": {
                "title": "RecordCurrentness",
                "description": "Open, dot-namespaced code naming whether a record version is the active one, such as `current` or `superseded` or `retracted`. Open by design; an unrecognized value must be preserved, not coerced to a known one.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__RecordId": {
                "title": "RecordId",
                "description": "Stable identifier of a governed record, constant across every version of that record.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "records__RecordIdentity": {
                "title": "RecordIdentity",
                "description": "The identity, version, governance layer, governance state, and currentness of one record version.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier stable across every version of this record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "Opaque version of this specific revision.",
                    },
                    "layer": {
                        "$ref": "#/$defs/records__GovernanceLayer",
                        "description": "Governance layer this record belongs to.",
                    },
                    "governance_state": {
                        "$ref": "#/$defs/records__GovernanceState",
                        "description": "This record version's position in its own governance workflow, independent of `layer` and `currentness`.",
                    },
                    "currentness": {
                        "$ref": "#/$defs/records__RecordCurrentness",
                        "description": "Whether this version is the active one.",
                    },
                    "supersedes": {
                        "$ref": "#/$defs/records__SupersessionReference",
                        "description": "The earlier record version this version replaces, when this version is itself the newer one.",
                    },
                    "superseded_by": {
                        "$ref": "#/$defs/records__SupersessionReference",
                        "description": "The newer record version that replaced this one, present when `currentness` marks this version as superseded.",
                    },
                },
                "required": [
                    "record_id",
                    "version",
                    "layer",
                    "governance_state",
                    "currentness",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordProvenance": {
                "title": "RecordProvenance",
                "description": "The full provenance envelope for one record version: identity, temporal metadata, its authoring history, the sources it draws on, and the caller-supplied assertion/extraction lineage the claim in this version came from. `assertion`/`extraction` are structurally optional so a record written before they existed still decodes, but a governance transition that replaces or carries forward a claim must bind them; enforcing that is a semantic-validation concern, not a wire-shape one.",
                "type": "object",
                "properties": {
                    "identity": {
                        "$ref": "#/$defs/records__RecordIdentity",
                        "description": "Identity, version, governance layer, and currentness of this record.",
                    },
                    "temporal": {
                        "$ref": "#/$defs/records__RecordTemporalMetadata",
                        "description": "Observed, ingested, recorded, and valid time for this record.",
                    },
                    "history": {
                        "type": "array",
                        "description": "Ordered, append-only history of actions that produced this record version. Deliberately carries no `maxItems`: history is never erased or rewritten, and every governance transition appends exactly one event, so any finite inline cap would eventually make a previously valid record impossible to transition -- and raising the cap only postpones that contradiction. Bounding a response's size is a transport/operation concern, handled outside this inline provenance invariant, never by dropping, compacting, or summarising audit history.",
                        "items": {
                            "$ref": "#/$defs/records__ProvenanceEntry",
                        },
                    },
                    "evidence_disposition": {
                        "$ref": "#/$defs/records__EvidenceDisposition",
                        "description": "Whether concrete evidence is actually available for this record. `sources` may be empty only when this disposition explicitly states evidence is unavailable; enforcing that agreement is a semantic-validation concern, not a wire-shape one.",
                    },
                    "sources": {
                        "type": "array",
                        "description": "Sources this record draws on, independent of any single history entry's evidence. May be empty only when `evidence_disposition` explicitly states evidence is unavailable.",
                        "items": {
                            "$ref": "#/$defs/records__SourceReference",
                        },
                        "maxItems": 256,
                    },
                    "assertion": {
                        "$ref": "#/$defs/records__CandidateAssertion",
                        "description": "Who asserted the claim this version carries, when, on what evidence, and the validity window they proposed. Preserved verbatim from the `memory.create` or `record.supersede` input that supplied the claim, so candidate/replacement lineage survives every governance transition.",
                    },
                    "extraction": {
                        "$ref": "#/$defs/records__CandidateExtractionMetadata",
                        "description": "Provenance of the automated extractor that produced the claim this version carries, when one did. Absent for a claim a human asserted directly.",
                    },
                },
                "required": [
                    "identity",
                    "temporal",
                    "history",
                    "evidence_disposition",
                    "sources",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordTemporalMetadata": {
                "title": "RecordTemporalMetadata",
                "description": "The distinct instants a governed record's lifecycle turns on: when the underlying fact occurred in the world, when it was observed, when the system ingested it, when this version was persisted, the window it is asserted valid for, and when it was superseded.",
                "type": "object",
                "properties": {
                    "event_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the underlying fact occurred in the world (source/event time), when distinguishable from `observed_at`.",
                    },
                    "observed_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the underlying fact was observed to be true in the world, when known.",
                    },
                    "ingested_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the system first ingested the fact behind this record.",
                    },
                    "recorded_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this specific version was persisted.",
                    },
                    "valid_from": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "Start of the window this record is asserted valid for, when bounded.",
                    },
                    "valid_until": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "End of the window this record is asserted valid for, when bounded.",
                    },
                    "superseded_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this record version was superseded by a newer version, present only once superseded.",
                    },
                },
                "required": [
                    "ingested_at",
                    "recorded_at",
                ],
                "unevaluatedProperties": False,
            },
            "records__RecordVersion": {
                "title": "RecordVersion",
                "description": "Opaque, server-issued version marker of one specific revision of a record. Clients must round-trip it verbatim and must never parse it.",
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": "^[!-~]+$(?![\\s\\S])",
            },
            "records__SourceKind": {
                "title": "SourceKind",
                "description": "Open, dot-namespaced code naming the kind of thing a source reference points at, such as `document` or `conversation` or `api_response`.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "records__SourceReference": {
                "title": "SourceReference",
                "description": "A pointer to the external or internal thing a record's claim came from.",
                "type": "object",
                "properties": {
                    "kind": {
                        "$ref": "#/$defs/records__SourceKind",
                        "description": "What kind of thing this reference points at.",
                    },
                    "source_id": {
                        "$ref": "#/$defs/common__Identifier",
                        "description": "Identifier of the source within its own system of record.",
                    },
                    "locator": {
                        "type": "string",
                        "description": "Optional locator within the source, such as a path, offset, or message id.",
                        "maxLength": 2048,
                    },
                    "retrieved_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When the source was read to produce the record it supports.",
                    },
                },
                "required": [
                    "kind",
                    "source_id",
                ],
                "unevaluatedProperties": False,
            },
            "records__SourceSpan": {
                "title": "SourceSpan",
                "description": "An addressable position within a source: a pointer plus an optional character span, so evidence can be pinpointed within a source rather than only referencing the source as a whole.",
                "type": "object",
                "properties": {
                    "pointer": {
                        "type": "string",
                        "description": "Locator within the source, such as a JSON Pointer, XPath, byte offset path, or line reference.",
                        "maxLength": 2048,
                    },
                    "start_offset": {
                        "type": "integer",
                        "description": "Start of the span, in characters from the start of the pointed-at unit, when known.",
                        "minimum": 0,
                    },
                    "end_offset": {
                        "type": "integer",
                        "description": "End of the span, in characters from the start of the pointed-at unit, when known.",
                        "minimum": 0,
                    },
                },
                "required": [
                    "pointer",
                ],
                "unevaluatedProperties": False,
            },
            "records__SupersessionReference": {
                "title": "SupersessionReference",
                "description": "A direction-neutral pointer from one record version to another related record version. The direction of the relationship comes entirely from which field on `RecordIdentity` carries it (`supersedes` vs `superseded_by`); this DTO itself states only which record and version, and why.",
                "type": "object",
                "properties": {
                    "record_id": {
                        "$ref": "#/$defs/records__RecordId",
                        "description": "Identifier of the related record.",
                    },
                    "version": {
                        "$ref": "#/$defs/records__RecordVersion",
                        "description": "The specific related version, when known.",
                    },
                    "reason": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Open code naming why this supersession relationship exists.",
                    },
                },
                "required": [
                    "record_id",
                ],
                "unevaluatedProperties": False,
            },
        },
    },
    "https://contracts.omnivia.dev/application/v1/workspace.schema.json#/$defs/WorkspaceInspectInput": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "WorkspaceInspectInput",
        "description": "Input for `workspace.inspect`. Workspace-scoped: the workspace to inspect is the request envelope's selected workspace; this payload never carries a second, independent workspace identifier.",
        "type": "object",
        "properties": {},
        "required": [],
        "unevaluatedProperties": False,
    },
    "https://contracts.omnivia.dev/application/v1/workspace.schema.json#/$defs/WorkspaceInspectResult": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "WorkspaceInspectResult",
        "description": "Result of `workspace.inspect`: the envelope-selected workspace's concrete descriptor.",
        "type": "object",
        "properties": {
            "workspace": {
                "$ref": "#/$defs/workspace__WorkspaceDescriptor",
                "description": "The inspected workspace.",
            },
        },
        "required": [
            "workspace",
        ],
        "unevaluatedProperties": False,
        "$defs": {
            "common__ContractVersion": {
                "title": "ContractVersion",
                "description": "A `major.minor` contract version. Major changes are breaking; minor changes are additive and forward compatible.",
                "type": "string",
                "pattern": "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$(?![\\s\\S])",
                "maxLength": 32,
            },
            "common__OpenCode": {
                "title": "OpenCode",
                "description": "An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible minor releases can add vocabulary; consumers must preserve values they do not recognize.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
            "common__Timestamp": {
                "title": "Timestamp",
                "description": "An RFC 3339 timestamp in UTC with a literal `Z` offset.",
                "type": "string",
                "format": "date-time",
                "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$(?![\\s\\S])",
                "maxLength": 40,
            },
            "common__WorkspaceId": {
                "title": "WorkspaceId",
                "description": "Bounded, non-empty identifier of the workspace a request is scoped to.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$(?![\\s\\S])",
            },
            "compatibility__VersionWindow": {
                "title": "VersionWindow",
                "description": "An inclusive range of contract versions a peer supports.",
                "type": "object",
                "properties": {
                    "minimum": {
                        "$ref": "#/$defs/common__ContractVersion",
                        "description": "Lowest supported version, inclusive.",
                    },
                    "maximum": {
                        "$ref": "#/$defs/common__ContractVersion",
                        "description": "Highest supported version, inclusive.",
                    },
                },
                "required": [
                    "minimum",
                    "maximum",
                ],
                "unevaluatedProperties": False,
            },
            "workspace__WorkspaceCompatibility": {
                "title": "WorkspaceCompatibility",
                "description": "The concrete workspace-format version a workspace is stored at, and the inclusive version window this server build can read and write. Reuses the same `VersionWindow` and `OpenCode` primitives `VersionCapabilityEnvelope` negotiates with, rather than inventing a second version model.",
                "type": "object",
                "properties": {
                    "workspace_format_version": {
                        "$ref": "#/$defs/common__ContractVersion",
                        "description": "Concrete workspace-format version this workspace is currently stored at.",
                    },
                    "supported_workspace_versions": {
                        "$ref": "#/$defs/compatibility__VersionWindow",
                        "description": "Inclusive workspace-format version window this server build can read and write.",
                    },
                    "status": {
                        "$ref": "#/$defs/common__OpenCode",
                        "description": "Compatibility status of `workspace_format_version` against `supported_workspace_versions`. Known values are listed in `compatibility.schema.json`'s `x-omnivia-compatibility-statuses`; unknown values must be preserved.",
                    },
                },
                "required": [
                    "workspace_format_version",
                    "supported_workspace_versions",
                    "status",
                ],
                "unevaluatedProperties": False,
            },
            "workspace__WorkspaceDescriptor": {
                "title": "WorkspaceDescriptor",
                "description": "A workspace's identity, display name, lifecycle status, format compatibility, and lifecycle timestamps.",
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "$ref": "#/$defs/common__WorkspaceId",
                        "description": "Stable, server-assigned identifier of this workspace.",
                    },
                    "display_name": {
                        "type": "string",
                        "description": "Human-readable workspace name.",
                        "minLength": 1,
                        "maxLength": 256,
                    },
                    "status": {
                        "$ref": "#/$defs/workspace__WorkspaceStatus",
                        "description": "Current lifecycle status of this workspace.",
                    },
                    "compatibility": {
                        "$ref": "#/$defs/workspace__WorkspaceCompatibility",
                        "description": "Concrete workspace-format version and the compatibility window it falls within.",
                    },
                    "created_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this workspace was created.",
                    },
                    "updated_at": {
                        "$ref": "#/$defs/common__Timestamp",
                        "description": "When this workspace's descriptor was last updated, when known.",
                    },
                },
                "required": [
                    "workspace_id",
                    "display_name",
                    "status",
                    "compatibility",
                    "created_at",
                ],
                "unevaluatedProperties": False,
            },
            "workspace__WorkspaceStatus": {
                "title": "WorkspaceStatus",
                "description": "Open, dot-namespaced code naming a workspace's lifecycle status, such as `active` or `provisioning` or `archived`. Open by design so a compatible minor release can add statuses without breaking existing decoders.",
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$(?![\\s\\S])",
            },
        },
    },
}
