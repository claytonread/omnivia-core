"""Model-neutral semantic vectors as values: identities, a strict codec, an exact scorer.

This module is on the same side of the import boundary as `storage/retrieval.py`, and for
the same reason. It imports the frozen contract, `storage.retrieval` (itself pure) and
the standard library, and nothing else: no `sqlite3`, no connection, no repository, no
projection adapter, no callback. Nothing here can reach a candidate that is not already
inside the value it was handed. `ProjectedSemanticFrontier` is to the semantic lane what
`retrieval.ProjectedFrontier` is to the lexical one -- already-authorized
`EvidenceCandidate` values beside their vectors, and no way back to a store.

**Four identities, deliberately separate.** Conflating them is how an index built by one
model silently serves queries embedded by another:

* `SemanticEmbeddingProfile` -- *what the numbers mean*: dimensions, weights, tokenizer,
  transform, pooling, normalization and the canonical-input version. Two vectors are only
  comparable when their profile digests are equal.
* `ExecutionProvenance` -- *how this build produced them*: provider, runtime, build,
  hardware and inference precision, plus an explicit `CompatibilityDecision`. Provenance is
  evidence, not meaning; it never changes the profile digest.
* `RepresentationIdentity` / `GenerationIdentity` -- *how the numbers are stored* (exact
  float32 little-endian, metric, codec and index version) and *which run holds them*.
* `SemanticRetrievalPolicy` -- *how a request uses them*: per-lane candidate budget,
  threshold, fusion version, tie-break version and final result limit.

**The codec refuses rather than repairs.** A vector is exactly `dimensions` finite float32
components, little-endian, with no negative zero (a second spelling of zero that changes a
canonical digest and nothing else) and an L2 norm within `UNIT_NORM_TOLERANCE` of 1. A
vector outside the tolerance is never renormalized: renormalizing would hide a provider
that does not honour the profile's normalization identifier.

**Scoring is exhaustive over the supplied frontier.** Every vector is decoded and
validated before any score exists, so a malformed vector anywhere in the frontier fails the
whole call instead of being skipped. The order is score descending, then the UTF-8 bytes of
the evidence id ascending -- a total order that depends on nothing but the value. A score of
`0.0` is a real score: it is kept, ranked and reported, never treated as absent.

**Fusion is versioned and its score is not confidence.** `rrf.v1` is reciprocal-rank fusion
with `k = 60` over the lexical and semantic *ranks*. The result is a rank score for ordering;
it carries no calibration and the field is named accordingly. An unknown version refuses.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.storage.retrieval import EvidenceCandidate

#: The only normalization this build implements: L2-unit vectors, validated not repaired.
NORMALIZATION_CORE_L2_UNIT_V1: Final = "core_l2_unit_v1"

#: Absolute tolerance on `abs(norm - 1)`. 1e-6 comfortably holds a float32-rounded unit
#: vector at every admitted dimension (rounding error is ~1e-7) while still refusing a
#: vector that was never normalized.
UNIT_NORM_TOLERANCE: Final = 1e-6

#: Admitted vector width. Model-neutral, so a bound rather than a pin.
MIN_DIMENSIONS: Final = 1
MAX_DIMENSIONS: Final = 8192

REPRESENTATION_FLOAT32_LE: Final = "float32_le"
BYTE_ORDER_LITTLE: Final = "little"
METRIC_COSINE: Final = "cosine"
METRIC_DOT: Final = "dot"
METRICS: Final[tuple[str, ...]] = (METRIC_COSINE, METRIC_DOT)

#: The canonical-input versions this build can construct bytes for.
CANONICAL_INPUT_V1: Final = "omnivia.semantic-input.v1"
CANONICAL_INPUT_VERSIONS: Final[tuple[str, ...]] = (CANONICAL_INPUT_V1,)

FUSION_RRF_V1: Final = "rrf.v1"
FUSION_VERSIONS: Final[tuple[str, ...]] = (FUSION_RRF_V1,)
#: `rrf.v1`'s rank constant. Part of the version: changing it is `rrf.v2`.
RRF_V1_K: Final = 60

TIE_BREAK_V1: Final = "score_desc.evidence_id_utf8.v1"
TIE_BREAK_VERSIONS: Final[tuple[str, ...]] = (TIE_BREAK_V1,)

COMPATIBLE: Final = "compatible"
INCOMPATIBLE: Final = "incompatible"
COMPATIBILITY_STATUSES: Final[tuple[str, ...]] = (COMPATIBLE, INCOMPATIBLE)

#: Hard maximum on each lane's retained candidates. Applied after exhaustive scoring,
#: never as a cap on the frontier that is scored.
MAX_CANDIDATE_BUDGET: Final = 200
DEFAULT_CANDIDATE_BUDGET: Final = 50

#: Hard maximum and default for the final fused result list.
MAX_RESULT_LIMIT: Final = 50
DEFAULT_RESULT_LIMIT: Final = 10

_TOKEN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")


class SemanticVectorError(ValueError):
    """A semantic value is malformed. Never repaired, never partially accepted."""


class UnsupportedSemanticVersion(SemanticVectorError):
    """A fusion, tie-break, input or normalization version this build does not implement."""


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise SemanticVectorError(
            f"{name} must be a 1-128 character identifier of [A-Za-z0-9._:/@+-] "
            "starting with an alphanumeric"
        )
    return value


def _int_in(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SemanticVectorError(f"{name} must be an integer in [{low}, {high}]")
    return value


def identity_digest(format_id: str, fields: Mapping[str, object]) -> str:
    """`sha256:` over the contract's canonical JSON of `{"format": ..., **fields}`."""
    document = {"format": format_id, **fields}
    return "sha256:" + hashlib.sha256(to_canonical_json(document).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticEmbeddingProfile:
    """What the vector components mean. The unit of comparability."""

    dimensions: int
    weights_id: str
    tokenizer_id: str
    transform_id: str
    pooling_id: str
    normalization_id: str
    canonical_input_version: str

    def __post_init__(self) -> None:
        _int_in(self.dimensions, "dimensions", MIN_DIMENSIONS, MAX_DIMENSIONS)
        for name in ("weights_id", "tokenizer_id", "transform_id", "pooling_id"):
            _token(getattr(self, name), name)
        if _token(self.normalization_id, "normalization_id") != NORMALIZATION_CORE_L2_UNIT_V1:
            raise UnsupportedSemanticVersion(
                f"normalization_id must be {NORMALIZATION_CORE_L2_UNIT_V1!r}"
            )
        if _token(self.canonical_input_version, "canonical_input_version") not in (
            CANONICAL_INPUT_VERSIONS
        ):
            raise UnsupportedSemanticVersion(
                f"canonical_input_version must be one of {list(CANONICAL_INPUT_VERSIONS)!r}"
            )

    @property
    def digest(self) -> str:
        return identity_digest(
            "omnivia.semantic-profile.v1",
            {
                "dimensions": self.dimensions,
                "weights_id": self.weights_id,
                "tokenizer_id": self.tokenizer_id,
                "transform_id": self.transform_id,
                "pooling_id": self.pooling_id,
                "normalization_id": self.normalization_id,
                "canonical_input_version": self.canonical_input_version,
            },
        )


@dataclass(frozen=True, slots=True)
class CompatibilityDecision:
    """An explicit, recorded decision -- never an inference from absent data."""

    status: str
    reason: str

    def __post_init__(self) -> None:
        if self.status not in COMPATIBILITY_STATUSES:
            raise SemanticVectorError(
                f"compatibility status must be one of {list(COMPATIBILITY_STATUSES)!r}"
            )
        _token(self.reason, "compatibility reason")

    @property
    def compatible(self) -> bool:
        return self.status == COMPATIBLE


@dataclass(frozen=True, slots=True)
class ExecutionProvenance:
    """How a generation was computed. Evidence about the run, not part of the profile."""

    provider_id: str
    runtime_id: str
    build_id: str
    hardware_id: str
    inference_precision: str
    compatibility: CompatibilityDecision

    def __post_init__(self) -> None:
        for name in (
            "provider_id",
            "runtime_id",
            "build_id",
            "hardware_id",
            "inference_precision",
        ):
            _token(getattr(self, name), name)

    @property
    def digest(self) -> str:
        """Over the five execution facts only: the decision is about them, not one of them."""
        return identity_digest(
            "omnivia.semantic-execution.v1",
            {
                "provider_id": self.provider_id,
                "runtime_id": self.runtime_id,
                "build_id": self.build_id,
                "hardware_id": self.hardware_id,
                "inference_precision": self.inference_precision,
            },
        )


def decide_compatibility(
    index: ExecutionProvenance, query: ExecutionProvenance
) -> CompatibilityDecision:
    """Whether a query embedded by `query` may be scored against vectors built by `index`.

    Conservative on purpose: only an identical execution is compatible. A looser rule
    ("same weights, different runtime") is a policy someone must write down and version;
    until then a mismatch is a refusal with a reason, not a guess.
    """
    if not index.compatibility.compatible:
        return CompatibilityDecision(INCOMPATIBLE, "index_declared_incompatible")
    if index.digest != query.digest:
        return CompatibilityDecision(INCOMPATIBLE, "execution_differs")
    return CompatibilityDecision(COMPATIBLE, "identical_execution")


@dataclass(frozen=True, slots=True)
class RepresentationIdentity:
    """How the components are stored: exact float32 little-endian, a metric, a codec."""

    scalar_encoding: str
    byte_order: str
    metric: str
    codec_version: str
    index_version: str

    def __post_init__(self) -> None:
        if self.scalar_encoding != REPRESENTATION_FLOAT32_LE:
            raise UnsupportedSemanticVersion(
                f"scalar_encoding must be {REPRESENTATION_FLOAT32_LE!r}"
            )
        if self.byte_order != BYTE_ORDER_LITTLE:
            raise UnsupportedSemanticVersion(f"byte_order must be {BYTE_ORDER_LITTLE!r}")
        if self.metric not in METRICS:
            raise UnsupportedSemanticVersion(f"metric must be one of {list(METRICS)!r}")
        _token(self.codec_version, "codec_version")
        _token(self.index_version, "index_version")

    @property
    def digest(self) -> str:
        return identity_digest(
            "omnivia.semantic-representation.v1",
            {
                "scalar_encoding": self.scalar_encoding,
                "byte_order": self.byte_order,
                "metric": self.metric,
                "codec_version": self.codec_version,
                "index_version": self.index_version,
            },
        )


@dataclass(frozen=True, slots=True)
class GenerationIdentity:
    """Which run holds which profile, representation and execution."""

    generation_id: str
    profile_digest: str
    representation_digest: str
    execution_digest: str
    compatibility_status: str

    def __post_init__(self) -> None:
        _token(self.generation_id, "generation_id")
        for name in ("profile_digest", "representation_digest", "execution_digest"):
            if _DIGEST.fullmatch(getattr(self, name)) is None:
                raise SemanticVectorError(f"{name} must be a sha256: digest")
        if self.compatibility_status not in COMPATIBILITY_STATUSES:
            raise SemanticVectorError("compatibility_status is not a known status")

    @property
    def digest(self) -> str:
        return identity_digest(
            "omnivia.semantic-generation.v1",
            {
                "generation_id": self.generation_id,
                "profile_digest": self.profile_digest,
                "representation_digest": self.representation_digest,
                "execution_digest": self.execution_digest,
                "compatibility_status": self.compatibility_status,
            },
        )


@dataclass(frozen=True, slots=True)
class SemanticRetrievalPolicy:
    """How a request uses a generation. `threshold=None` means no score floor.

    `candidate_budget` is each lane's retained top-N after full scoring; `result_limit`
    is the length of the final fused list.
    """

    candidate_budget: int = DEFAULT_CANDIDATE_BUDGET
    threshold: float | None = None
    fusion_version: str = FUSION_RRF_V1
    tie_break_version: str = TIE_BREAK_V1
    result_limit: int = DEFAULT_RESULT_LIMIT

    def __post_init__(self) -> None:
        _int_in(self.candidate_budget, "candidate_budget", 1, MAX_CANDIDATE_BUDGET)
        _int_in(self.result_limit, "result_limit", 1, MAX_RESULT_LIMIT)
        if self.threshold is not None:
            threshold = self.threshold
            if (
                isinstance(threshold, bool)
                or not isinstance(threshold, float)
                or not math.isfinite(threshold)
                or not -1.0 <= threshold <= 1.0
                or (threshold == 0.0 and math.copysign(1.0, threshold) < 0.0)
            ):
                raise SemanticVectorError(
                    "threshold must be None or a finite float in [-1, 1], not negative zero"
                )
        if self.fusion_version not in FUSION_VERSIONS:
            raise UnsupportedSemanticVersion(
                f"fusion_version must be one of {list(FUSION_VERSIONS)!r}"
            )
        if self.tie_break_version not in TIE_BREAK_VERSIONS:
            raise UnsupportedSemanticVersion(
                f"tie_break_version must be one of {list(TIE_BREAK_VERSIONS)!r}"
            )

    @property
    def digest(self) -> str:
        return identity_digest(
            "omnivia.semantic-policy.v1",
            {
                "candidate_budget": self.candidate_budget,
                "threshold": self.threshold,
                "fusion_version": self.fusion_version,
                "tie_break_version": self.tie_break_version,
                "result_limit": self.result_limit,
            },
        )


# --------------------------------------------------------------------------
# Canonical model input
# --------------------------------------------------------------------------


def canonical_model_input(text: str) -> bytes:
    """`omnivia.semantic-input.v1`: the NFKC-normalized text as UTF-8.

    Not case-folded -- unlike the lexical lane, an embedding model may be case-sensitive,
    and the model, not this module, decides what case means to it.
    """
    return unicodedata.normalize("NFKC", text).encode("utf-8")


def input_digest(canonical_input: bytes) -> str:
    """The exact-input identity vectors are deduplicated by."""
    return "sha256:" + hashlib.sha256(canonical_input).hexdigest()


# --------------------------------------------------------------------------
# Strict float32 little-endian codec
# --------------------------------------------------------------------------


def _check_components(values: Sequence[float], dimensions: int) -> tuple[float, ...]:
    if len(values) != dimensions:
        raise SemanticVectorError(f"vector has {len(values)} components, expected {dimensions}")
    for index, value in enumerate(values):
        if not math.isfinite(value):
            raise SemanticVectorError(f"component {index} is not finite")
        if value == 0.0 and math.copysign(1.0, value) < 0.0:
            raise SemanticVectorError(f"component {index} is negative zero")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if not math.isfinite(norm) or abs(norm - 1.0) > UNIT_NORM_TOLERANCE:
        raise SemanticVectorError(
            f"vector violates {NORMALIZATION_CORE_L2_UNIT_V1}: norm is not within "
            f"{UNIT_NORM_TOLERANCE} of 1"
        )
    return tuple(values)


def decode_vector(payload: bytes, *, dimensions: int) -> tuple[float, ...]:
    """Exactly `dimensions` unit-norm float32 components, or `SemanticVectorError`."""
    _int_in(dimensions, "dimensions", MIN_DIMENSIONS, MAX_DIMENSIONS)
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise SemanticVectorError("vector payload must be bytes-like")
    data = bytes(payload)
    if len(data) != dimensions * 4:
        raise SemanticVectorError(
            f"vector payload is {len(data)} bytes, expected {dimensions * 4}"
        )
    return _check_components(struct.unpack(f"<{dimensions}f", data), dimensions)


def encode_vector(values: Sequence[float], *, dimensions: int) -> bytes:
    """The canonical bytes of a unit-norm vector, validated after float32 rounding.

    Validation runs on the *rounded* components because those are what is stored and
    scored: a double that underflows to a float32 negative zero, or overflows, is refused
    here rather than discovered at read time.
    """
    _int_in(dimensions, "dimensions", MIN_DIMENSIONS, MAX_DIMENSIONS)
    if len(values) != dimensions:
        raise SemanticVectorError(f"vector has {len(values)} components, expected {dimensions}")
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SemanticVectorError(f"component {index} is not a real number")
    try:
        payload = struct.pack(f"<{dimensions}f", *values)
    except (OverflowError, struct.error) as exc:
        raise SemanticVectorError("a component does not fit in float32") from exc
    decode_vector(payload, dimensions=dimensions)
    return payload


# --------------------------------------------------------------------------
# The projected frontier and the exhaustive scorer
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectedSemanticCandidate:
    """One authorized candidate, its stored vector bytes and that vector's exact input.

    `input_digest` is the stored occurrence's encoder-input identity, so two occurrences
    sharing one deduplicated vector stay distinguishable without exposing vector bytes.
    """

    candidate: EvidenceCandidate
    vector: bytes
    input_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.input_digest, str) or _DIGEST.fullmatch(self.input_digest) is None:
            raise SemanticVectorError("input_digest must be a sha256: digest")


@dataclass(frozen=True, slots=True)
class ProjectedSemanticFrontier:
    """The frozen frontier beside its vectors, and nothing else.

    `checksum` is the authorized frontier's own, carried verbatim: this is the same set of
    candidates, so a second digest could only agree by accident. The value is validated on
    construction -- membership, workspace and every vector -- so a scorer handed one has no
    unchecked input left.
    """

    workspace_id: str
    checksum: str
    dimensions: int
    profile_digest: str
    generation_digest: str
    candidates: tuple[ProjectedSemanticCandidate, ...]

    def __post_init__(self) -> None:
        _int_in(self.dimensions, "dimensions", MIN_DIMENSIONS, MAX_DIMENSIONS)
        seen: set[str] = set()
        for item in self.candidates:
            evidence_id = item.candidate.evidence_id
            if evidence_id in seen:
                raise SemanticVectorError("a frontier member appears twice")
            seen.add(evidence_id)
            if item.candidate.workspace_id != self.workspace_id:
                raise SemanticVectorError("a candidate belongs to another workspace")
            decode_vector(item.vector, dimensions=self.dimensions)


@dataclass(frozen=True, slots=True)
class SemanticHit:
    """One ranked frontier member. `rank` is 1-based; `score` may be exactly `0.0`."""

    candidate: EvidenceCandidate
    score: float
    rank: int
    input_digest: str


def _sort_key(evidence_id: str) -> bytes:
    return evidence_id.encode("utf-8")


def _similarity(query: tuple[float, ...], vector: tuple[float, ...], metric: str) -> float:
    dot = math.fsum(q * c for q, c in zip(query, vector, strict=True))
    if metric == METRIC_DOT:
        score = dot
    else:
        denominator = math.sqrt(math.fsum(q * q for q in query)) * math.sqrt(
            math.fsum(c * c for c in vector)
        )
        score = dot / denominator
    if not math.isfinite(score) or abs(score) > 1.0 + 1e-12:
        raise SemanticVectorError("similarity is outside [-1, 1]")
    score = max(-1.0, min(1.0, score))
    # An IEEE negative zero is a second spelling of 0.0 and would break canonical digests.
    return 0.0 if score == 0.0 else score


def rank_semantic(
    projected: ProjectedSemanticFrontier,
    query_vector: bytes,
    *,
    metric: str,
    policy: SemanticRetrievalPolicy,
) -> tuple[SemanticHit, ...]:
    """Exact top-`candidate_budget` of the complete projected frontier.

    Every vector is decoded and scored before any threshold or truncation, so the result
    is a function of the whole frontier and never of an early exit. The threshold is a
    floor on the score (`score >= threshold`); `0.0` passes a floor of `0.0` or below.
    """
    if metric not in METRICS:
        raise UnsupportedSemanticVersion(f"metric must be one of {list(METRICS)!r}")
    query = decode_vector(query_vector, dimensions=projected.dimensions)
    scored = [
        (
            item,
            _similarity(query, decode_vector(item.vector, dimensions=projected.dimensions), metric),
        )
        for item in projected.candidates
    ]
    if policy.threshold is not None:
        floor = policy.threshold
        scored = [pair for pair in scored if pair[1] >= floor]
    scored.sort(key=lambda pair: (-pair[1], _sort_key(pair[0].candidate.evidence_id)))
    return tuple(
        SemanticHit(
            candidate=item.candidate, score=score, rank=rank, input_digest=item.input_digest
        )
        for rank, (item, score) in enumerate(scored[: policy.candidate_budget], start=1)
    )


# --------------------------------------------------------------------------
# Versioned fusion
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FusedHit:
    """One fused member. `rank_score` orders results; it is not a confidence."""

    candidate: EvidenceCandidate
    rank_score: float
    lexical_rank: int | None
    semantic_rank: int | None
    semantic_score: float | None
    semantic_input_digest: str | None


def fuse_ranked(
    lexical: Sequence[EvidenceCandidate],
    semantic: Sequence[SemanticHit],
    *,
    version: str,
    limit: int,
) -> tuple[FusedHit, ...]:
    """Fuse a lexical ranking with a semantic ranking under an explicit version.

    `rrf.v1`: each list contributes `1 / (60 + rank)` for a 1-based rank; a member in one
    list scores that one term, and an absent lane contributes zero. Sums are computed as
    exact fractions and ordered by (score descending, UTF-8 evidence id ascending), so
    equal scores -- the common case, e.g. rank 1 in either list alone -- break on identity
    and never on arrival order. Semantic ranks must be exactly 1..n in order, so a
    re-ordered or gapped list is refused rather than fused. Both inputs must already be
    members of one authorized frontier; membership is the caller's proof, and a member
    present in both lists must be the same artifact.
    """
    if version not in FUSION_VERSIONS:
        raise UnsupportedSemanticVersion(f"fusion version must be one of {list(FUSION_VERSIONS)!r}")
    _int_in(limit, "limit", 1, MAX_RESULT_LIMIT)
    candidates: dict[str, EvidenceCandidate] = {}
    scores: dict[str, Fraction] = {}
    lexical_rank: dict[str, int] = {}
    semantic_rank: dict[str, int] = {}
    semantic_score: dict[str, float] = {}
    semantic_input: dict[str, str] = {}

    def admit(candidate: EvidenceCandidate) -> None:
        known = candidates.setdefault(candidate.evidence_id, candidate)
        if known.content_checksum != candidate.content_checksum:
            raise SemanticVectorError("one evidence id names two different artifacts")

    for rank, candidate in enumerate(lexical, start=1):
        if candidate.evidence_id in lexical_rank:
            raise SemanticVectorError("a lexical result appears twice")
        admit(candidate)
        lexical_rank[candidate.evidence_id] = rank
        scores[candidate.evidence_id] = scores.get(candidate.evidence_id, Fraction(0)) + Fraction(
            1, RRF_V1_K + rank
        )
    for position, hit in enumerate(semantic, start=1):
        evidence_id = hit.candidate.evidence_id
        if evidence_id in semantic_rank:
            raise SemanticVectorError("a semantic result appears twice")
        if hit.rank != position:
            raise SemanticVectorError("semantic ranks must be one-based and consecutive")
        admit(hit.candidate)
        semantic_rank[evidence_id] = hit.rank
        semantic_score[evidence_id] = hit.score
        semantic_input[evidence_id] = hit.input_digest
        scores[evidence_id] = scores.get(evidence_id, Fraction(0)) + Fraction(
            1, RRF_V1_K + hit.rank
        )
    ordered = sorted(scores, key=lambda evidence_id: (-scores[evidence_id], _sort_key(evidence_id)))
    return tuple(
        FusedHit(
            candidate=candidates[evidence_id],
            rank_score=float(scores[evidence_id]),
            lexical_rank=lexical_rank.get(evidence_id),
            semantic_rank=semantic_rank.get(evidence_id),
            semantic_score=semantic_score.get(evidence_id),
            semantic_input_digest=semantic_input.get(evidence_id),
        )
        for evidence_id in ordered[:limit]
    )


__all__ = [
    "BYTE_ORDER_LITTLE",
    "CANONICAL_INPUT_V1",
    "CANONICAL_INPUT_VERSIONS",
    "COMPATIBILITY_STATUSES",
    "COMPATIBLE",
    "DEFAULT_CANDIDATE_BUDGET",
    "DEFAULT_RESULT_LIMIT",
    "FUSION_RRF_V1",
    "FUSION_VERSIONS",
    "INCOMPATIBLE",
    "MAX_CANDIDATE_BUDGET",
    "MAX_DIMENSIONS",
    "MAX_RESULT_LIMIT",
    "METRICS",
    "METRIC_COSINE",
    "METRIC_DOT",
    "MIN_DIMENSIONS",
    "NORMALIZATION_CORE_L2_UNIT_V1",
    "REPRESENTATION_FLOAT32_LE",
    "RRF_V1_K",
    "TIE_BREAK_V1",
    "TIE_BREAK_VERSIONS",
    "UNIT_NORM_TOLERANCE",
    "CompatibilityDecision",
    "ExecutionProvenance",
    "FusedHit",
    "GenerationIdentity",
    "ProjectedSemanticCandidate",
    "ProjectedSemanticFrontier",
    "RepresentationIdentity",
    "SemanticEmbeddingProfile",
    "SemanticHit",
    "SemanticRetrievalPolicy",
    "SemanticVectorError",
    "UnsupportedSemanticVersion",
    "canonical_model_input",
    "decide_compatibility",
    "decode_vector",
    "encode_vector",
    "fuse_ranked",
    "identity_digest",
    "input_digest",
    "rank_semantic",
]
