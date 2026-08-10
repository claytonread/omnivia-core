"""The frozen Context Pack frontier, and the pure builder that turns one into a pack.

`context_pack.build` is the one operation in this build whose *output is a checkable
claim about how it was produced*. The artifact is content-addressed, it states the
authorized frontier it ranked over, and a second party holding the document plus an
out-of-band manifest can recompute both. Everything in this module exists to make those
two claims true by construction rather than by assertion.

**The line, and which side each half of this module sits on.**

`retrieval.py` established the shape for `evidence.search` and `knowledge.search`: a
module that holds the store (`repository.py`, `governed.py`, `projections/fts.py`) reads,
a *pure* module freezes and ranks, and the freeze is the line nothing crosses. This module
is that pure module for `context_pack.build`, and it keeps the boundary exactly:

- it imports the standard library and the frozen contract, and **nothing else**. No
  `sqlite3`, no `omnivia_core_runtime.*`, so there is no module in reach that could
  produce a candidate;
- `build_context_pack` takes two frozen values and nothing else. No connection, no
  repository, no projection, no callback, and no parameter carrying a default a handle
  could be bound into;
- neither function captures a cell, and no module-level name here holds anything with an
  `execute` or a `cursor` on it.

So an unauthorized candidate is not merely forbidden to the builder -- it is unreachable,
which is what `test_context_pack_determinism.py` checks rather than takes on trust. It
checks it by *mutation*: each of the four prohibited shapes is written into a copy of this
module's source and the structural guard is required to fail on it, because a guard that
passes the file it was written against proves only that the file has not changed. Each
mutant is then *run*, against a tracing store the test owns, and the smuggled read is
required to have actually executed and to have put a candidate into the pack that this
module's own build cannot produce -- so what the guard kills is a defect that happens rather
than a shape that merely exists.

**The freeze is a value transform, and the coherent read is the caller's.**

:func:`freeze_context_pack_frontier` takes the material an authoritative read already
produced and freezes it: it deep-copies every candidate, sorts every partition into one
deterministic order, enforces the frontier ceiling, and produces the out-of-band
`ContextPackAuthorizedCandidateSetManifest` *at the freeze*, from the frozen members,
before the first ranking or selection decision. That is exactly the split
`retrieval.authorized_frontier` already has -- `repository.read_evidence_candidates` holds
the connection, the freeze holds none.

The copy is load-bearing rather than defensive habit, and it is a *canonical contract round
trip* rather than a `deepcopy`. `GovernedRecord.content` and `EvidenceArtifact.metadata` are
opaque JSON: the DTOs around them are frozen dataclasses, the payloads inside them are
ordinary mutable mappings and lists, and the caller that read them still holds a reference.
Without the copy, a mutation *after* the freeze would change a candidate's matched surface,
its emitted section content, its token count and therefore the pack's own identity -- a
frontier that is frozen in name and live in fact.

`deepcopy` cannot do that job, in both directions. A DTO that came off the contract's own
decoder carries `MappingProxyType` payloads and `deepcopy` raises on one, so the valid
input is the input it refuses; a hand-built DTO it copies successfully into fresh *mutable*
dictionaries, which disconnects the caller's reference and leaves the frontier mutable
through its own. `EvidenceArtifact.from_wire(artifact.to_wire())` does both jobs at once,
because `_decode_json_value` recursively renders objects as read-only mappings and arrays
as tuples -- and it is value-preserving, so nothing about the candidate or the pack's
identity moves. The same round trip is what the *result* is returned through, after its
digest is assigned, so a sealed pack is not editable through the value the caller holds.

What this module therefore does **not** own is the coherent read itself. The Lane D
handler must perform it, in one explicit read transaction, before calling the freeze:

    connection.execute("BEGIN")            # one snapshot for every read below
    checkpoint = authoritative_checkpoint(connection, workspace_id=...)
    readiness  = projection_readiness(connection, workspace_id=..., source_checkpoint=...)
    build      = active_build(connection, workspace_id=...)      # storage/projections/fts
    evidence   = read_evidence_candidates(connection, workspace_id=...)
    current    = read_governed_record_values(connection, ..., view="current_canonical")
    history    = read_governed_record_values(connection, ..., view="history")
    connection.execute("COMMIT")

Every one of those reads already states a total `ORDER BY` and already runs inside
`authorised(connection, mutations=False, ddl=False)`; `resolve_governed_versions` already
declines to end a transaction it did not open, which is what lets all six share one
snapshot. The handler then applies the authorization chain that is likewise already
written -- `local_owner_label_grant` and `authorized_frontier` for L0, the resolver's own
view/governance/temporal narrowing plus the request's `record_type` and `domain_scope`
selectors for L2 -- and hands the surviving material here. Nothing below sees anything
else.

**`context_models` is empty, and it is empty for a stated reason.** Migration 0009 admits
`context_model` as a `layer` value and refuses it a seal outright, so this build has no
sealable authoritative L3 source to select from. The partition is therefore empty rather
than populated from a weaker source, and it carries no omission either: an omission
accounts for a candidate the frontier held, and there was never one.

**History is frontier input, not pack content.** The history partition is frozen, is in
the manifest, and is what makes the frontier reproducible -- and no member of it is ever
selected. A superseded version presented beside a current one is two contradictory claims
about one instant, so every history candidate is accounted for by omission instead.

**One instant, supplied, used twice.** :class:`ContextPackBuildContext` carries one
`canonical_resolution_time`, and it is what `canonical_resolution_time`, `generated_at`
and `freshness.as_of` are all rendered from. There is no clock in this module -- no
`time`, no `datetime`, no `utcnow` -- so two builds from one context are byte-identical by
construction, and changing the generation instant alone is not a thing this builder can be
asked to do.

**Everything the builder cannot know, it is given -- and everything it does know, it
checks.** `retrieval_version` is the one version field that is genuinely a producer
statement about something else: nothing here retrieves, the candidates arrive already read,
and which retrieval produced them is a fact only the later handler holds. So it is a
*required* field of the build context, recorded verbatim, and the pure slice proves nothing
about where it came from.

Everything else this module names, it implements. The normalization is `_normalize`, the
ranking is `_Selectable.order_key`, the selection is the greedy pass in
`build_context_pack`, the tokenizer is `_TOKEN`, the builder is this file and the reranker
does not exist. Those six are therefore constants here
(`CONTEXT_PACK_BUILDER_VERSION` and its siblings, with `CONTEXT_PACK_RERANKER_DISABLED` for
the pass that never runs) and the build context is required to state exactly them -- for
the same reason the summarizer and model versions are required to be
`CONTEXT_PACK_SUMMARIZER_DISABLED` and empty. A version string a caller chose for code it
does not own is a fabricated claim that verifies, which is worse than a missing one.

The same reasoning runs through every guard in this module: a supplied fact
this code cannot check is recorded verbatim, and a supplied fact it *can* check is checked
and fails closed. The normalized query is checked against this module's own normalization
of the request, the attested filter chain against the chain that must have run, the
freshness maps against the one projection this operation is served from, each candidate
against the workspace and the partition it was frozen into, and the authority and scope
arrays against the contract's own canonical-array rule -- refused when they do not satisfy
it rather than sorted, deduplicated or repaired into satisfying it, because Amendment 009
makes the effective authority the exact set already authorized.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from types import MappingProxyType
from typing import Final, NamedTuple

from omnivia_core.contracts.v1 import (
    CONTEXT_PACK_ARTIFACT_CANONICALIZATION,
    CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT,
    CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
    CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY,
    CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS,
    CONTEXT_PACK_FORMAT_VERSION,
    CONTEXT_PACK_NORMALIZED_REQUEST_VIEW,
    CONTEXT_PACK_SUMMARIZER_DISABLED,
    ContextPackAuthorizationContext,
    ContextPackAuthorizedCandidateSetManifest,
    ContextPackAuthorizedEvidenceCandidate,
    ContextPackAuthorizedRecordCandidate,
    ContextPackBudget,
    ContextPackBuildInput,
    ContextPackBuildResult,
    ContextPackCitation,
    ContextPackEvidenceCitation,
    ContextPackEvidenceReference,
    ContextPackNormalizedRequest,
    ContextPackRecordCitation,
    ContextPackReproducibility,
    ContextPackSection,
    EvidenceArtifact,
    GovernedRecord,
    GrantedAuthority,
    Omission,
    ProjectionFreshness,
    RecordVersionReference,
    compute_authorized_candidate_set_checksum,
    compute_context_pack_artifact_digest,
    to_canonical_json,
)
from omnivia_core.contracts.v1.canonical_json import utf16_sort_key
from omnivia_core.contracts.v1.semantics import (
    RECORD_CURRENTNESS_CURRENT,
    RECORD_CURRENTNESS_SUPERSEDED,
)

#: The filters that must have run *before* a frontier is frozen, named so the frozen value
#: can state what narrowed it rather than leaving a reviewer to infer it. The union of the
#: L0 chain (`retrieval.FRONTIER_FILTERS`), the L2 chain
#: (`retrieval.GOVERNED_FRONTIER_FILTERS`) and the request-envelope authorization this
#: operation additionally carries. There is no post-freeze member and adding one would be
#: the defect the freeze exists to prevent.
CONTEXT_PACK_FRONTIER_FILTERS: Final[tuple[str, ...]] = (
    "workspace",
    "scope",
    "purpose",
    "capability",
    "policy",
    "evidence_label_acl",
    "sensitivity",
    "tombstone",
    "view",
    "governance",
    "temporal",
    "record_type",
    "domain_scope",
)

#: The largest authorized frontier this builder will freeze, and the reason there is a
#: number at all.
#:
#: It is not the contract's manifest `maxItems` (20 000). It is the ceiling that makes
#: *exact* frontier accounting possible: every frozen candidate this build does not select
#: is accounted for by an `Omission`, and `ContextPackBuildResult.omissions` has a schema
#: `maxItems` of 256. A frontier wider than that cannot be accounted for at all, and the
#: two ways to pretend otherwise -- truncating the frontier before digesting it, or
#: digesting it and leaving the surplus unaccounted -- are both a pack lying about what it
#: ranked over. So the freeze refuses instead, and the handler maps that refusal to
#: `size_limit_exceeded`.
#:
#: The same number is `sections`'s `maxItems`, and that is not a coincidence worth
#: economising on: at most one section is emitted per frozen candidate, so one ceiling
#: keeps both arrays inside the contract's bounds.
CONTEXT_PACK_MAX_FRONTIER_CANDIDATES: Final = 256

#: `ContextPackSection.content`'s schema `maxLength`, restated here for the reason the
#: contract restates its own bounds: nothing upstream bounds a governed record's opaque
#: JSON, so a candidate whose canonical content is longer than this cannot be emitted as a
#: section at all. It is skipped and accounted for rather than truncated -- a truncated
#: section is a pack presenting part of a record as the record, and its token count and its
#: citation would both then describe content the caller never received.
CONTEXT_PACK_MAX_SECTION_CONTENT_LENGTH: Final = 16_384

#: The one projection that contributes to this operation's freshness statement, and
#: therefore the only key its two projection maps may carry.
#:
#: The value is `storage.projections.EVIDENCE_SEARCH_PROJECTION_ID`, restated rather than
#: imported: importing it would put an `omnivia_core_runtime.*` module in this one's import
#: list and give up the boundary the module docstring claims. Restated means it can drift,
#: which is why the freeze pins the key set exactly -- a freshness statement naming a
#: projection this operation never read from, or omitting the one it did, is a claim about
#: a snapshot rather than a report of one. The *values* stay supplied: this module owns no
#: projection and may not invent a build digest or a checkpoint.
CONTEXT_PACK_FRESHNESS_PROJECTION_ID: Final = "evidence.search.fts5"

#: The algorithm identities this file actually implements, and the reason they are
#: constants here rather than free fields of the build context.
#:
#: The module docstring below used to say this slice owns none of its version fields, and
#: for four of them that was simply false. The normalization *is* the NFKC-then-casefold in
#: :func:`_normalize`; the ranking *is* the substring count, recency and identity tie-break
#: in :meth:`_Selectable.order_key`; the selection *is* the greedy pass under the budget in
#: :func:`build_context_pack`; the tokenizer *is* :data:`_TOKEN`; and the builder is this
#: function. A free field for any of those is a place a caller states a version for code it
#: does not own, which is the same fabricated-but-verifying claim
#: :data:`CONTEXT_PACK_SUMMARIZER_DISABLED` already refuses -- so each is pinned here and
#: required to match, and a change to the algorithm without a change to its constant is the
#: one drift a reviewer can then actually see.
#:
#: `retrieval_version` is deliberately *not* in this list. Nothing in this module retrieves:
#: the candidates arrive already read, and which retrieval produced them is a fact only the
#: later handler holds. It stays a required, unchecked producer statement, and the pure
#: slice proves nothing about its source.
CONTEXT_PACK_BUILDER_VERSION: Final = "context-pack.builder.v1"
CONTEXT_PACK_NORMALIZATION_VERSION: Final = "context-pack.normalization.nfkc-casefold.v1"
CONTEXT_PACK_RANKING_VERSION: Final = "context-pack.ranking.count-recency-identity.v1"
CONTEXT_PACK_SELECTION_VERSION: Final = "context-pack.selection.greedy-budget.v1"
CONTEXT_PACK_TOKENIZER_ID: Final = "context-pack.tokenizer.alnum-run-or-char"
CONTEXT_PACK_TOKENIZER_VERSION: Final = "context-pack.tokenizer.v1"

#: The reranker twin of :data:`CONTEXT_PACK_SUMMARIZER_DISABLED`, and true for the same
#: reason: no reranking pass exists in this file, so `disabled` is the only honest value and
#: any label naming one is a version string for code that never ran.
CONTEXT_PACK_RERANKER_DISABLED: Final = "disabled"

#: Section `kind`s, one per partition a section can be built from. `OpenCode`s, so lower
#: snake case with dotted segments.
_KIND_EVIDENCE: Final = "evidence_artifact"
_KIND_RECORD: Final = "governed_record"

#: Omission codes. Server-authored `OpenCode`s: no message on this path carries a caller
#: value, and the `path` names server-side identities only.
OMISSION_NOT_MATCHED: Final = "context_pack.query_did_not_match"
OMISSION_SECTION_TOO_LARGE: Final = "context_pack.section_content_too_large"
OMISSION_SUPERSEDED: Final = "context_pack.superseded_version"
OMISSION_TOKEN_BUDGET: Final = "context_pack.token_budget_exhausted"

_OMISSION_MESSAGES: Final[Mapping[str, str]] = {
    OMISSION_NOT_MATCHED: (
        "this authorized candidate is on the frozen frontier but the normalized query "
        "does not occur in it, so it was not selected"
    ),
    OMISSION_SECTION_TOO_LARGE: (
        "this authorized candidate matched the normalized query but its canonical content "
        "is longer than one section may carry; it is omitted whole rather than truncated"
    ),
    OMISSION_SUPERSEDED: (
        "this governed version had already been superseded at the canonical-resolution "
        "instant; it is frontier and reproducibility input, never selected content"
    ),
    OMISSION_TOKEN_BUDGET: (
        "this authorized candidate matched the normalized query but does not fit the "
        "remaining token budget"
    ),
}

#: A well-formed `ContextPackDigest` that stands in for the two members the artifact digest
#: is computed with removed. Its value is never hashed -- `compute_context_pack_artifact_digest`
#: strips both members -- but both must be *present* and well-typed for the reduction to be
#: defined over a strict result at all.
_DIGEST_PLACEHOLDER: Final = "sha256:" + "0" * 64

#: One token: a run of Unicode alphanumerics, or any single non-space character that is not
#: part of one. Punctuation counts, which is what keeps a canonical-JSON section's count
#: strictly positive for every non-empty content string the contract admits -- a section
#: whose content costs nothing to send is content the budget never paid for, and the
#: contract refuses it.
#:
#: This is a *count*, and it does not claim to be a model tokenizer. It is published under
#: :data:`CONTEXT_PACK_TOKENIZER_ID` and :data:`CONTEXT_PACK_TOKENIZER_VERSION`, which name
#: this pattern and nothing else: the tokenizer a pack attests is the one that produced its
#: counts, so the identifier belongs beside the regex rather than in a caller's hands.
_TOKEN: Final = re.compile(r"[^\W_]+|[^\s]")


class ContextPackFrontierTooLarge(Exception):
    """One authorized frontier is too wide for a pack that accounts for all of it.

    The single deterministic builder error this module raises, carrying the ceiling and
    the observed size and nothing about any candidate. The Lane D handler maps it to the
    contract's `size_limit_exceeded`; it deliberately does not subclass this tree's
    `StorageError`, because that lives in a module holding a connection and importing it
    would put a store back within this module's reach.
    """


class ContextPackBuilderInputInvalid(Exception):
    """One builder input is impossible for this operation, so nothing is produced.

    Kept apart from :class:`ContextPackFrontierTooLarge` because the two mean different
    things to the handler that maps them: a frontier that is merely too wide is a caller's
    request being too large, while everything raised here -- a foreign workspace on a
    candidate, a history member that is not superseded, a normalized query that is not the
    request's, a freshness statement naming the wrong projection, an authority array the
    contract would refuse -- is a producer-side invariant that a correct handler cannot
    reach at all. Both fail closed; only the first is a caller's fault.

    Every message names the rule and nothing about any candidate. The whole point of this
    module is that it cannot leak what it was handed, and an exception string is exactly
    the place a candidate's identity would otherwise escape.
    """


@dataclass(frozen=True, slots=True)
class FrozenEvidence:
    """One authorized L0 artifact on the frozen frontier, and the one ranking fact the
    contract DTO cannot carry.

    `recorded_at_us` is the integer 0008 stored. `EvidenceArtifact.temporal.recorded_at` is
    the contract's millisecond `Timestamp`, and bulk ingestion writes many artifacts at one
    microsecond, so the digits a recency tie-break needs are exactly the ones the wire
    rendering drops.

    The identity is read off the artifact rather than duplicated beside it, so a candidate
    cannot be ranked under one identity and returned under another.
    """

    recorded_at_us: int
    artifact: EvidenceArtifact


@dataclass(frozen=True, slots=True)
class FrozenRecord:
    """One authorized governed version on the frozen frontier, with its stored instant.

    The governed twin of :class:`FrozenEvidence`, for the same two reasons: the millisecond
    `Timestamp` is not a total recency order, and the identity belongs to the record.
    """

    recorded_at_us: int
    record: GovernedRecord


@dataclass(frozen=True, slots=True)
class ContextPackFrozenFrontier:
    """The complete authorized frontier one pack may be built from, frozen.

    Frozen in all three senses: immutable, materialised before the first ranking, selection
    or budget decision, and *disconnected* -- every candidate is a deep copy, so the opaque
    JSON inside it is no longer the mapping the reader still holds. What it deliberately
    does not carry is any way to obtain a candidate it does not already hold -- no
    connection, no repository, no projection, no callback and no id it could resolve -- so
    a builder handed one has the whole of its input in the value.

    `candidate_set_checksum` is the contract's own digest over the contract's own manifest
    shape, computed at the freeze. It is in-process verifier input and never a response
    field: a frontier digest read back out of the artifact it is meant to check verifies
    nothing.

    The three projection members are read in the *same* snapshot as the candidates and are
    reported verbatim -- the FTS projection's active `build_digest` as its version and the
    source checkpoint that build is level with as its watermark. Nothing here derives a
    freshness claim from anything else, and the two maps are read-only views so a frontier
    cannot be re-dated after it was frozen.
    """

    workspace_id: str
    evidence: tuple[FrozenEvidence, ...]
    records: tuple[FrozenRecord, ...]
    history: tuple[FrozenRecord, ...]
    context_models: tuple[FrozenRecord, ...]
    filters_applied: tuple[str, ...]
    candidate_set_checksum: str
    projection_versions: Mapping[str, str]
    projection_watermarks: Mapping[str, str]
    projection_stale: bool


class ContextPackFreeze(NamedTuple):
    """What a freeze produces: the frozen frontier, and the manifest that vouches for it.

    Two values rather than one field on the frontier, because they travel differently. The
    frontier is what the builder ranks over; the manifest is out-of-band verifier input
    that goes to the validator and never to the wire. Keeping them separate is what makes
    "the manifest was produced at the freeze, not reconstructed from the result" a property
    of the call graph rather than a comment.
    """

    frontier: ContextPackFrozenFrontier
    manifest: ContextPackAuthorizedCandidateSetManifest


@dataclass(frozen=True, slots=True)
class ContextPackBuildContext:
    """Everything one build needs that is not on the frontier, supplied and immutable.

    Every field is required. There are no defaults, and that is structural rather than
    stylistic: a default is a place a live handle, a wall-clock reading or an invented
    version string could be bound in without any call site changing.

    `canonical_resolution_time` is *the* instant -- the one the authoritative read resolved
    at, already rendered as the contract's `Timestamp`. It is what
    `reproducibility.canonical_resolution_time`, `reproducibility.generated_at` and
    `freshness.as_of` are all set from, because the contract requires those three to be the
    same *string*: this artifact is content-addressed, so two spellings of one moment are
    two identities.

    The version fields split in two, and the split is the honest one rather than the
    convenient one. `retrieval_version` is a producer statement this slice genuinely cannot
    check -- nothing here retrieves -- so it is recorded verbatim. The builder,
    normalization, ranking, reranking, selection and tokenizer identities name algorithms
    that live in *this file*, so they are required to equal the constants above:
    :func:`build_context_pack` refuses any other label, because a caller free to name a
    version for code it does not own can state one that verifies and is untrue.

    `policy_versions` and `model_versions` arrive as caller-owned mappings and are copied at
    construction into deterministically ordered read-only views. Without the copy "frozen"
    would describe the dataclass and not the value: the caller still holds the original
    dictionary, and a write to it between two builds from *one* context would give the two
    packs different policy attestations and therefore different identities.
    """

    request: ContextPackBuildInput
    workspace_id: str
    authority: GrantedAuthority
    scopes: tuple[str, ...]
    purpose: str
    policy_versions: Mapping[str, str]
    canonical_resolution_time: str
    normalized_query: str
    normalization_version: str
    builder_version: str
    retrieval_version: str
    ranking_version: str
    reranking_version: str
    selection_version: str
    tokenizer_id: str
    tokenizer_version: str
    summarizer_version: str
    model_versions: Mapping[str, str]

    def __post_init__(self) -> None:
        """Copy the two caller-owned mappings into deterministic read-only views.

        `object.__setattr__` because the dataclass is frozen, which is the documented way to
        normalise a field at construction: the boundary is the constructor, so there is no
        window in which the instance holds the caller's own object. `dict(sorted(...))`
        rather than `dict(...)` so the stored order is a function of the keys and not of how
        the caller's dictionary was assembled, and `MappingProxyType` so a reference kept to
        the *stored* value is no more writable than one kept to the original.
        """
        for field in ("policy_versions", "model_versions"):
            supplied: Mapping[str, str] = getattr(self, field)
            object.__setattr__(
                self, field, MappingProxyType(dict(sorted(supplied.items())))
            )


@dataclass(frozen=True, slots=True)
class _Selectable:
    """One frontier candidate reduced to what selection and section-building need.

    Built once, before ranking, so the relevance a candidate was ordered by, the content a
    section would carry, the tokens it would cost and the identity it would be cited under
    are one reading of one candidate rather than four. `relevance` is counted over
    `content` itself, so the text a query is matched against is exactly the text the caller
    would receive.
    """

    partition: str
    first: str
    second: str
    relevance: int
    recorded_at_us: int
    content: str
    tokens: int
    artifact: EvidenceArtifact | None
    record: GovernedRecord | None

    @property
    def order_key(self) -> tuple[int, int, bytes, bytes, bytes]:
        """The total order selection runs in.

        Relevance descending, then recency descending, then partition, then the identity
        pair -- negated where descending, because `sort` is ascending and a key is the
        honest place to state direction.

        Both tie-breaks are load-bearing rather than decorative. A count-based signal ties
        constantly, and equal instants are equally common because one sealed correction
        writes every version at one microsecond. The identity pair closes the key, and the
        freeze is what makes it total: `compute_authorized_candidate_set_checksum` refuses
        a manifest that repeats `(partition, first, second)`, so a frontier that reached
        this point has no two candidates sharing one. Insertion order, mapping iteration
        order and unordered SQL therefore cannot reach the output.
        """
        return (
            -self.relevance,
            -self.recorded_at_us,
            utf16_sort_key(self.partition),
            utf16_sort_key(self.first),
            utf16_sort_key(self.second),
        )

    @property
    def path(self) -> str:
        """The stable pointer an omission names this candidate by."""
        return _path(self.partition, self.first, self.second)


def _path(partition: str, first: str, second: str) -> str:
    """One candidate's omission `path`: partition, identity, version-or-checksum."""
    return f"{partition}/{first}@{second}"


def _sort_key(*parts: str) -> tuple[bytes, ...]:
    """The ordering key for a compound identity, under the contract's own rule.

    Unsigned UTF-16 code unit, which is what RFC 8785 imposes on object member names and
    what the contract's identity-bearing arrays are required to ascend by. Python's native
    string comparison is code-*point* ordering; the two coincide across every v1 identity
    alphabet and diverge above the BMP, so using the contract's helper means this module's
    sort and the validator's ascending check (`_identity_sort_key`, of which this is the
    same composition) can never be two different orderings.
    """
    return tuple(utf16_sort_key(part) for part in parts)


def _normalize(text: str) -> str:
    """One spelling of a text, so matching is a property of what it says.

    NFKC then case-folded, applied identically to the query and to every candidate's
    content -- normalizing one side only makes a match depend on which side a caller typed.
    The same two steps `retrieval.normalize_query` applies, restated rather than imported
    because importing it would put an `omnivia_core_runtime.*` module in this one's import
    list and give up the boundary the docstring above claims.
    """
    return unicodedata.normalize("NFKC", text).casefold()


def _token_count(text: str) -> int:
    """How many tokens one section's exact model-facing content occupies."""
    return len(_TOKEN.findall(text))


def _relevance(surface: str, needle: str) -> int:
    """How many times the normalized query occurs in a normalized surface.

    A count, and stated as one: there is no index behind it, no term weighting and no
    corpus statistics. It is computed from one candidate's own content and nothing else, so
    no candidate can move the score of another -- which is the property a corpus-wide
    statistic gives up. An empty needle scores nothing rather than matching everything;
    the contract already refuses an empty query upstream, so this is a fail-closed guard
    rather than a reachable branch.
    """
    if not needle:
        return 0
    return surface.count(needle)


def _require(condition: bool, message: str) -> None:
    """Fail closed on a builder invariant, naming the rule and never the material."""
    if not condition:
        raise ContextPackBuilderInputInvalid(message)


def _frozen_evidence(item: FrozenEvidence) -> FrozenEvidence:
    """One evidence candidate as a disconnected, transitively immutable value.

    The copy is a canonical contract round trip rather than a `deepcopy`, and that is a
    correctness fix rather than a style one. `EvidenceArtifact.metadata` is opaque JSON;
    `from_wire` renders every nested mapping as a `MappingProxyType`, and `deepcopy`
    cannot copy one at all -- it raises, so a DTO that came off the contract decoder could
    not be frozen. On a hand-built DTO `deepcopy` did succeed and produced fresh *mutable*
    dictionaries, which is the worse failure: the frontier was disconnected from the
    caller's mapping and still mutable through its own.

    `EvidenceArtifact.from_wire(artifact.to_wire())` is the fix in one line, because the
    decoder's `_decode_json_value` recursively freezes objects to read-only mappings and
    arrays to tuples. The round trip is value-preserving by the contract's own statement --
    a decode/encode pair reproduces the document exactly -- so nothing about the candidate,
    its canonical bytes or the pack's identity changes.
    """
    return FrozenEvidence(
        recorded_at_us=item.recorded_at_us,
        artifact=EvidenceArtifact.from_wire(item.artifact.to_wire()),
    )


def _frozen_record(item: FrozenRecord) -> FrozenRecord:
    """One governed candidate as a disconnected, transitively immutable value.

    The governed twin of :func:`_frozen_evidence`, for the same reason and over the same
    hazard: `GovernedRecord.content` is opaque JSON the reader still holds a reference to.
    """
    return FrozenRecord(
        recorded_at_us=item.recorded_at_us,
        record=GovernedRecord.from_wire(item.record.to_wire()),
    )


def _sorted_evidence(items: Sequence[FrozenEvidence]) -> tuple[FrozenEvidence, ...]:
    """One evidence partition in ascending `(evidence_id, content_checksum)` order."""
    return tuple(
        sorted(
            items,
            key=lambda item: _sort_key(
                item.artifact.evidence_id, item.artifact.content_checksum
            ),
        )
    )


def _sorted_records(items: Sequence[FrozenRecord]) -> tuple[FrozenRecord, ...]:
    """One governed partition in ascending `(record_id, version)` order."""
    return tuple(
        sorted(
            items,
            key=lambda item: _sort_key(
                item.record.provenance.identity.record_id,
                item.record.provenance.identity.version,
            ),
        )
    )


def freeze_context_pack_frontier(
    *,
    workspace_id: str,
    evidence: Sequence[FrozenEvidence],
    records: Sequence[FrozenRecord],
    history: Sequence[FrozenRecord],
    filters_applied: Sequence[str],
    projection_versions: Mapping[str, str],
    projection_watermarks: Mapping[str, str],
    projection_stale: bool,
) -> ContextPackFreeze:
    """Freeze one authorized frontier and produce the manifest that vouches for it.

    Everything handed in has already been filtered: workspace, scope, purpose, capability
    and policy by the request envelope's authorization, evidence-label ACL, sensitivity,
    tombstone and temporal by `retrieval.authorized_frontier`, and view, governance,
    temporal, `record_type` and `domain_scope` by the governed resolver and the request's
    own selectors. This function narrows nothing further -- a filter applied here would be
    a filter applied *at* the freeze rather than before it, and a second opinion about a
    snapshot it cannot re-read.

    What it does own is everything that has to be true of a frozen frontier:

    - **the attested filter chain**, required to be exactly
      :data:`CONTEXT_PACK_FRONTIER_FILTERS`. A frontier states what narrowed it, and an
      unchecked statement is the one field of the frozen value nothing else can contradict;
    - **the freshness statement**, required to be non-stale and to name exactly
      :data:`CONTEXT_PACK_FRESHNESS_PROJECTION_ID` in both maps. The values stay the
      caller's; the shape of the claim does not;
    - **the copy**, a canonical contract round trip per candidate
      (:func:`_frozen_evidence`, :func:`_frozen_record`), so the opaque JSON a caller still
      holds a reference to is no longer the JSON this frontier matches, sections and
      digests over -- and is no longer mutable through *any* reference, since the decoder
      renders nested objects as read-only mappings and arrays as tuples. Without it the
      word "frozen" would describe the dataclass and not the value;
    - **the workspace and partition invariants**: every candidate carries the freeze's own
      `workspace_id`, every member of `records` is `current` and every member of `history`
      is `superseded`. A candidate failing one is refused rather than dropped or repaired --
      dropping it would silently narrow the frontier the manifest then vouches for, and
      repairing it would restate a stored fact this module cannot re-read;
    - **one deterministic order per partition**, by identity, so the frontier is a function
      of its members and not of the order rows arrived in;
    - **the ceiling**, refused rather than truncated
      (:data:`CONTEXT_PACK_MAX_FRONTIER_CANDIDATES`, and see its note for why the number is
      what it is);
    - **the manifest, built here**, from the frozen members, before the first ranking or
      selection decision -- which is the whole of its evidential value. A manifest
      reconstructed from a result, from the selected items, or from a fixture written by
      reading a previous result asserts equality with itself;
    - **the checksum**, the contract's own digest over that manifest.

    `context_models` is empty and is not a parameter: 0009 refuses a seal to the
    `context_model` layer, so there is no sealable authoritative L3 source to pass. Making
    it an argument would offer a place to put material this build cannot vouch for.

    `filters_applied` is required rather than defaulted, for the reason
    `GovernedFrontier.filters_applied` is: a default would let a frontier narrowed by four
    filters claim thirteen.

    Duplicate and contradictory candidates are not checked here and are not tolerated
    either -- `compute_authorized_candidate_set_checksum` refuses a repeated identity, one
    `evidence_id` in two content states, and one governed version claimed by two
    partitions, and it refuses them as contract errors rather than silently collapsing
    them. Restating those rules here would be a second place for them to drift.
    """
    _require(
        tuple(filters_applied) == CONTEXT_PACK_FRONTIER_FILTERS,
        "filters_applied is not exactly the filter chain this operation runs before a "
        "freeze; a frontier may not restate, reorder, repeat or omit what narrowed it",
    )
    _require(
        not projection_stale,
        "the contributing projection is stale; this build refuses to produce a pack whose "
        "own freshness statement says the material it ranked over was behind the write "
        "model, rather than emitting one that is known-invalid at the instant it is sealed",
    )
    version_keys = set(projection_versions)
    _require(
        version_keys == set(projection_watermarks),
        "projection_versions and projection_watermarks name different projections; they "
        "are one statement about one set, and a version without its watermark leaves a "
        "reader unable to tell how far behind that projection is",
    )
    _require(
        version_keys == {CONTEXT_PACK_FRESHNESS_PROJECTION_ID},
        "the freshness maps must name exactly the one projection this operation is served "
        f"from ({CONTEXT_PACK_FRESHNESS_PROJECTION_ID}); naming another, or none, is a "
        "claim about a snapshot rather than a report of the one that was read",
    )

    # Before the copy, because it is a property of the input's size alone and copying a
    # frontier this build is going to refuse is work nobody asked for -- and because a
    # frontier that is both too wide and internally inconsistent should be reported as too
    # wide, which is the failure the handler can map to a caller-facing outcome.
    total = len(evidence) + len(records) + len(history)
    if total > CONTEXT_PACK_MAX_FRONTIER_CANDIDATES:
        raise ContextPackFrontierTooLarge(
            f"the authorized frontier holds {total} candidates, above the maximum of "
            f"{CONTEXT_PACK_MAX_FRONTIER_CANDIDATES} a pack can account for in full; it is "
            "refused rather than truncated, because a truncated frontier is a pack stating "
            "it ranked over material it never saw"
        )

    frozen_evidence = _sorted_evidence(tuple(_frozen_evidence(item) for item in evidence))
    frozen_records = _sorted_records(tuple(_frozen_record(item) for item in records))
    frozen_history = _sorted_records(tuple(_frozen_record(item) for item in history))

    for item in frozen_evidence:
        _require(
            item.artifact.workspace_id == workspace_id,
            "an evidence candidate belongs to a workspace other than the one being frozen",
        )
    for partition_members, currentness, rule in (
        (frozen_records, RECORD_CURRENTNESS_CURRENT, "records"),
        (frozen_history, RECORD_CURRENTNESS_SUPERSEDED, "history"),
    ):
        for governed in partition_members:
            _require(
                governed.record.workspace_id == workspace_id,
                "a governed candidate belongs to a workspace other than the one being "
                "frozen",
            )
            _require(
                governed.record.provenance.identity.currentness == currentness,
                f"a candidate in the {rule} partition does not carry currentness "
                f"{currentness!r}; the partition a version is frozen into is a claim about "
                "it, and this build refuses to silently move, drop or repair one",
            )

    manifest = ContextPackAuthorizedCandidateSetManifest(
        format=CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT,
        workspace_id=workspace_id,
        candidates=(
            *(
                ContextPackAuthorizedEvidenceCandidate(
                    partition=CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
                    evidence_id=item.artifact.evidence_id,
                    content_checksum=item.artifact.content_checksum,
                )
                for item in frozen_evidence
            ),
            *(
                ContextPackAuthorizedRecordCandidate(
                    partition=partition,
                    record_id=item.record.provenance.identity.record_id,
                    version=item.record.provenance.identity.version,
                )
                for partition, members in (
                    (CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS, frozen_records),
                    (CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY, frozen_history),
                )
                for item in members
            ),
        ),
    )

    frontier = ContextPackFrozenFrontier(
        workspace_id=workspace_id,
        evidence=frozen_evidence,
        records=frozen_records,
        history=frozen_history,
        # Empty by construction rather than by an empty argument -- see the docstring.
        context_models=(),
        filters_applied=tuple(filters_applied),
        candidate_set_checksum=compute_authorized_candidate_set_checksum(manifest),
        projection_versions=MappingProxyType(dict(sorted(projection_versions.items()))),
        projection_watermarks=MappingProxyType(
            dict(sorted(projection_watermarks.items()))
        ),
        projection_stale=projection_stale,
    )
    return ContextPackFreeze(frontier=frontier, manifest=manifest)


def _selectables(
    frontier: ContextPackFrozenFrontier, needle: str
) -> tuple[_Selectable, ...]:
    """Every frozen candidate a section could be built from, scored, in ranked order.

    `history` and `context_models` are absent on purpose: they are frontier and
    reproducibility input, and a superseded version returned beside a current one is two
    contradictory claims about one instant. They are accounted for as omissions instead,
    which is why they are still counted exactly once.

    Both partitions are reduced the same way -- the candidate's own canonical JSON is the
    content a section would carry *and* the surface the query is matched against. One
    reading rather than two: a candidate that matched on a surface it does not emit would
    be a pack whose relevance claim describes text the caller never saw. `to_canonical_json`
    sorts keys, so the reduction is a property of what a candidate says and not of how a
    decoder happened to build it.
    """
    items: list[_Selectable] = []
    for candidate in frontier.evidence:
        artifact = candidate.artifact
        content = to_canonical_json(artifact.source.to_wire())
        items.append(
            _Selectable(
                partition=CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
                first=artifact.evidence_id,
                second=artifact.content_checksum,
                relevance=_relevance(_normalize(content), needle),
                recorded_at_us=candidate.recorded_at_us,
                content=content,
                tokens=_token_count(content),
                artifact=artifact,
                record=None,
            )
        )
    for governed in frontier.records:
        record = governed.record
        identity = record.provenance.identity
        # `record.to_wire()["content"]` rather than `record.content`: a frozen candidate's
        # opaque JSON is a tree of read-only mappings, and `json.dumps` refuses one. The
        # encoder is the contract's own inverse of the decode that froze it, so this is the
        # same value and the same canonical bytes.
        content = to_canonical_json(record.to_wire()["content"])
        items.append(
            _Selectable(
                partition=CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS,
                first=identity.record_id,
                second=identity.version,
                relevance=_relevance(_normalize(content), needle),
                recorded_at_us=governed.recorded_at_us,
                content=content,
                tokens=_token_count(content),
                artifact=None,
                record=record,
            )
        )
    items.sort(key=lambda item: item.order_key)
    return tuple(items)


def build_context_pack(
    frontier: ContextPackFrozenFrontier, context: ContextPackBuildContext
) -> ContextPackBuildResult:
    """Build one Context Pack from a frozen frontier and an immutable build context.

    Read this signature together with this module's import list, because that pair is the
    whole isolation proof: two parameters, both frozen values, neither callable and neither
    carrying a default; and a module that imports the standard library and the frozen
    contract and nothing that could reach a row. An unauthorized candidate is unreachable
    here rather than merely forbidden.

    The order below is the artifact's own guarantee, top to bottom:

    1. **score, then order.** Every frozen selectable candidate is scored against the
       normalized query and put in one total order (:meth:`_Selectable.order_key`). Nothing
       above this line has seen the query, and nothing below it can see anything the
       frontier does not hold;
    2. **select under the budget.** Ranked order, greedy, and a candidate that does not fit
       the remaining budget is skipped rather than ending the selection -- a later, smaller
       candidate that does fit is content the caller was entitled to;
    3. **cite everything selected, section everything cited.** One citation and one section
       per selected candidate, numbered in ranked order, so `citation_id` and `section_id`
       ascend exactly as the contract requires and every coverage rule holds in both
       directions by construction;
    4. **account for everything else.** Every frozen candidate that was not selected gets
       exactly one omission: it did not match, it would not fit in one section, it did not
       fit the budget, or it is a superseded version this pack will not present as current.
       The arithmetic is exact -- `frozen == selected + omitted`, by identity -- which is
       what makes the accounting checkable rather than plausible;
    5. **state the reproducibility record from supplied and read values only.** The three
       instants are one supplied string; the freshness is the projection material read in
       the frontier's own snapshot; the version fields are the context's; the selected
       identities are the ones actually selected, sorted;
    6. **close the content addressing.** The digest is computed over the complete result
       with `pack_id` and `reproducibility.artifact_checksum` removed, and both are then set
       to it.

    The result is *produced*, not judged: `validate_context_pack_build_result` is the
    judge, and it needs the out-of-band manifest and the caller's exact expectations, which
    a producer cannot supply about itself without the check agreeing with itself by
    construction.
    """
    request = context.request
    _require(
        context.workspace_id == frontier.workspace_id,
        "the build context and the frozen frontier name different workspaces; a pack may "
        "not attest one workspace's authorization over another's material",
    )
    # The query that is ranked and the query that is attested are one value or there is no
    # build. They were two before: this module normalized the request itself to rank, and
    # wrote down whatever `normalized_query` it was handed, so a pack could state a
    # normalized query that had nothing to do with the text it actually matched on.
    needle = _normalize(request.query)
    _require(
        context.normalized_query == needle,
        "normalized_query is not this module's normalization of the request's query; the "
        "form that is ranked and the form that is attested must be one value",
    )
    _require(
        context.summarizer_version == CONTEXT_PACK_SUMMARIZER_DISABLED
        and not context.model_versions,
        "no summarizer and no model runs in this build, so the only truthful statements "
        f"are summarizer_version {CONTEXT_PACK_SUMMARIZER_DISABLED!r} and an empty "
        "model_versions; a version string for a component that never ran is a fabricated "
        "claim that verifies",
    )
    _require(
        (
            context.builder_version,
            context.normalization_version,
            context.ranking_version,
            context.reranking_version,
            context.selection_version,
            context.tokenizer_id,
            context.tokenizer_version,
        )
        == (
            CONTEXT_PACK_BUILDER_VERSION,
            CONTEXT_PACK_NORMALIZATION_VERSION,
            CONTEXT_PACK_RANKING_VERSION,
            CONTEXT_PACK_RERANKER_DISABLED,
            CONTEXT_PACK_SELECTION_VERSION,
            CONTEXT_PACK_TOKENIZER_ID,
            CONTEXT_PACK_TOKENIZER_VERSION,
        ),
        "the builder, normalization, ranking, reranking, selection and tokenizer "
        "identities name algorithms implemented in this module, so they must be exactly "
        "the constants this module publishes; a caller-chosen label for code it does not "
        "own is a version statement nothing can check",
    )
    _require_canonical_authority(context.authority)
    _require_canonical_scopes(context.scopes)
    budget = request.token_budget

    selected: list[_Selectable] = []
    omissions: list[Omission] = []
    used = 0
    for item in _selectables(frontier, needle):
        if item.relevance <= 0:
            omissions.append(_omission(OMISSION_NOT_MATCHED, item.path))
        elif len(item.content) > CONTEXT_PACK_MAX_SECTION_CONTENT_LENGTH:
            omissions.append(_omission(OMISSION_SECTION_TOO_LARGE, item.path))
        elif used + item.tokens > budget:
            omissions.append(_omission(OMISSION_TOKEN_BUDGET, item.path))
        else:
            used += item.tokens
            selected.append(item)
    for governed in frontier.history:
        identity = governed.record.provenance.identity
        omissions.append(
            _omission(
                OMISSION_SUPERSEDED,
                _path(
                    CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY,
                    identity.record_id,
                    identity.version,
                ),
            )
        )

    sections: list[ContextPackSection] = []
    citations: list[ContextPackCitation] = []
    for ordinal, item in enumerate(selected, start=1):
        citation_id = f"c-{ordinal:04d}"
        sections.append(
            ContextPackSection(
                section_id=f"s-{ordinal:04d}",
                kind=_KIND_EVIDENCE if item.artifact is not None else _KIND_RECORD,
                content=item.content,
                citation_ids=(citation_id,),
                token_count=item.tokens,
            )
        )
        if item.artifact is not None:
            citations.append(
                ContextPackEvidenceCitation(
                    citation_id=citation_id,
                    evidence_reference=ContextPackEvidenceReference(
                        evidence_id=item.first, content_checksum=item.second
                    ),
                )
            )
        else:
            citations.append(
                ContextPackRecordCitation(
                    citation_id=citation_id,
                    record_reference=RecordVersionReference(
                        record_id=item.first, version=item.second
                    ),
                )
            )

    selected_evidence = _sorted_evidence(
        tuple(
            FrozenEvidence(recorded_at_us=item.recorded_at_us, artifact=item.artifact)
            for item in selected
            if item.artifact is not None
        )
    )
    selected_records = _sorted_records(
        tuple(
            FrozenRecord(recorded_at_us=item.recorded_at_us, record=item.record)
            for item in selected
            if item.record is not None
        )
    )

    resolution = context.canonical_resolution_time
    reproducibility = ContextPackReproducibility(
        pack_format_version=CONTEXT_PACK_FORMAT_VERSION,
        builder_version=context.builder_version,
        normalized_request=ContextPackNormalizedRequest(
            normalized_query=context.normalized_query,
            mode=request.mode,
            view=CONTEXT_PACK_NORMALIZED_REQUEST_VIEW,
            token_budget=budget,
            normalization_version=context.normalization_version,
            domain_scope=request.domain_scope,
            record_type=request.record_type,
        ),
        authorization_context=ContextPackAuthorizationContext(
            workspace_id=context.workspace_id,
            # Verbatim, both. See `_require_canonical_authority`.
            authority=context.authority,
            scopes=context.scopes,
            purpose=context.purpose,
            policy_versions=dict(sorted(context.policy_versions.items())),
            # Literally true of this build and checkable from it: the frontier was frozen,
            # digested and handed here before the first line of `build_context_pack` ran,
            # and nothing this function can reach could widen it.
            pre_ranking_authorization_enforced=True,
            authorized_candidate_set_checksum=frontier.candidate_set_checksum,
        ),
        evidence_versions=tuple(
            ContextPackEvidenceReference(
                evidence_id=item.artifact.evidence_id,
                content_checksum=item.artifact.content_checksum,
            )
            for item in selected_evidence
        ),
        record_versions=tuple(
            RecordVersionReference(
                record_id=item.record.provenance.identity.record_id,
                version=item.record.provenance.identity.version,
            )
            for item in selected_records
        ),
        freshness=ProjectionFreshness(
            as_of=resolution,
            projection_versions=dict(frontier.projection_versions),
            projection_watermarks=dict(frontier.projection_watermarks),
            stale=frontier.projection_stale,
        ),
        retrieval_version=context.retrieval_version,
        ranking_version=context.ranking_version,
        reranking_version=context.reranking_version,
        selection_version=context.selection_version,
        tokenizer_id=context.tokenizer_id,
        tokenizer_version=context.tokenizer_version,
        summarizer_version=context.summarizer_version,
        model_versions=dict(sorted(context.model_versions.items())),
        canonical_resolution_time=resolution,
        # The same string, from the same field, and there is no second source it could come
        # from: a deterministic build is logically complete at the instant it resolved at,
        # and a wall-clock generation time would give two identical builds two identities.
        generated_at=resolution,
        artifact_canonicalization=CONTEXT_PACK_ARTIFACT_CANONICALIZATION,
        artifact_checksum=_DIGEST_PLACEHOLDER,
    )

    result = ContextPackBuildResult(
        pack_id=_DIGEST_PLACEHOLDER,
        mode=request.mode,
        query=request.query,
        sections=tuple(sections),
        evidence=tuple(item.artifact for item in selected_evidence),
        records=tuple(item.record for item in selected_records),
        # Frontier input, never content. See the module docstring.
        history=(),
        context_models=(),
        citations=tuple(citations),
        # Nothing is asserted that was not observed: this build has no conflict detector
        # and no uncertainty model, and a fabricated statement about cited content would be
        # exactly the unverifiable claim this artifact exists to make impossible.
        conflicts=(),
        uncertainties=(),
        omissions=_sorted_omissions(omissions),
        budget=ContextPackBudget(token_budget=budget, tokens_used=used),
        reproducibility=reproducibility,
        fresh_authorization_required=True,
    )
    digest = compute_context_pack_artifact_digest(result.to_wire())
    sealed = replace(
        result,
        pack_id=digest,
        reproducibility=replace(reproducibility, artifact_checksum=digest),
    )
    # Returned through the contract's own decoder, after the digest is assigned and never
    # before it. Until this line the result is a frozen dataclass wrapped around ordinary
    # dictionaries -- the opaque JSON in each selected record's `content`, each artifact's
    # `metadata`, and the four mapping fields on the reproducibility record are all still
    # mutable through the value the caller receives, so a pack could be edited after it was
    # sealed and would then no longer be what its own `pack_id` addresses. `from_wire`
    # renders every nested object as a read-only mapping and every array as a tuple, and it
    # is value-preserving: the canonical bytes, and therefore the digest, are unchanged,
    # which is why it is safe to run it *after* the content addressing is closed.
    return ContextPackBuildResult.from_wire(sealed.to_wire())


def _omission(code: str, path: str) -> Omission:
    """One accounting entry for one frozen candidate that did not become content."""
    return Omission(code=code, path=path, message=_OMISSION_MESSAGES[code])


def _sorted_omissions(omissions: Sequence[Omission]) -> tuple[Omission, ...]:
    """Omissions in the contract's ascending `(code, path, message)` order.

    Absent optionals order as empty, which is the rule the contract states; every omission
    this module builds carries both, so the third component only ever separates two entries
    that share a code and a path -- which cannot happen, since the path is one candidate's
    identity and each candidate is accounted for exactly once.
    """
    return tuple(
        sorted(
            omissions,
            key=lambda item: _sort_key(
                item.code, item.path or "", item.message or ""
            ),
        )
    )


def _ascending(keys: Sequence[tuple[bytes, ...]]) -> bool:
    """Whether a sequence of identity keys is *strictly* ascending -- so also unique."""
    return all(before < after for before, after in pairwise(keys))


def _require_canonical_authority(authority: GrantedAuthority) -> None:
    """Require the supplied grant to already be the canonical array, and change nothing.

    This module used to sort and deduplicate here, and that was the defect rather than the
    service. Amendment 009 makes the effective authority the exact set that was already
    authorized: a builder that sorts is a builder that would silently accept a caller
    handing it a different array from the one authorization produced, and a builder that
    deduplicates repairs a malformed grant into a well-formed attestation of something
    nobody granted. Neither is checkable afterwards, because the artifact then carries the
    repaired value and nothing records that a repair happened.

    So the requirement is exactly the contract's own stored-array rule -- roles strictly
    ascending, capabilities strictly ascending by `(id, version)` and naming each
    capability id once, which is what `_validate_authorization_context` enforces -- and the
    value is written to the artifact byte for byte. A grant that does not already satisfy
    it fails closed here rather than being made to satisfy it.
    """
    _require(
        _ascending([_sort_key(role) for role in authority.roles]),
        "authority.roles is not strictly ascending and duplicate-free; the effective "
        "authority is the exact set already authorized, so a malformed grant is refused "
        "rather than sorted or deduplicated into a well-formed one",
    )
    _require(
        _ascending([_sort_key(ref.id, ref.version) for ref in authority.capabilities]),
        "authority.capabilities is not strictly ascending by (id, version); the effective "
        "authority is the exact set already authorized, so a malformed grant is refused "
        "rather than reordered into a well-formed one",
    )
    ids = [ref.id for ref in authority.capabilities]
    _require(
        len(set(ids)) == len(ids),
        "authority.capabilities names one capability id twice; a granted authority names "
        "each capability once, at a single version",
    )


def _require_canonical_scopes(scopes: Sequence[str]) -> None:
    """Require the supplied scopes to already be the canonical array, and change nothing.

    The scope twin of :func:`_require_canonical_authority`, for the same reason: a pack
    records at least one scope in force, ascending and duplicate-free, and the set it
    records is the one that was already in force rather than one this module tidied up.
    """
    _require(
        bool(scopes),
        "scopes is empty; a pack records at least one scope in force, and a build that "
        "recorded none would attest an authorization that could never be satisfied",
    )
    _require(
        _ascending([_sort_key(scope) for scope in scopes]),
        "scopes is not strictly ascending and duplicate-free; the scopes in force are the "
        "exact ones already authorized, so a malformed set is refused rather than sorted "
        "or deduplicated",
    )


__all__ = [
    "CONTEXT_PACK_BUILDER_VERSION",
    "CONTEXT_PACK_FRESHNESS_PROJECTION_ID",
    "CONTEXT_PACK_FRONTIER_FILTERS",
    "CONTEXT_PACK_MAX_FRONTIER_CANDIDATES",
    "CONTEXT_PACK_MAX_SECTION_CONTENT_LENGTH",
    "CONTEXT_PACK_NORMALIZATION_VERSION",
    "CONTEXT_PACK_RANKING_VERSION",
    "CONTEXT_PACK_RERANKER_DISABLED",
    "CONTEXT_PACK_SELECTION_VERSION",
    "CONTEXT_PACK_TOKENIZER_ID",
    "CONTEXT_PACK_TOKENIZER_VERSION",
    "OMISSION_NOT_MATCHED",
    "OMISSION_SECTION_TOO_LARGE",
    "OMISSION_SUPERSEDED",
    "OMISSION_TOKEN_BUDGET",
    "ContextPackBuildContext",
    "ContextPackBuilderInputInvalid",
    "ContextPackFreeze",
    "ContextPackFrontierTooLarge",
    "ContextPackFrozenFrontier",
    "FrozenEvidence",
    "FrozenRecord",
    "build_context_pack",
    "freeze_context_pack_frontier",
]
