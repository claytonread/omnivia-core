"""Standalone exhaustive reference oracle for the frozen Q1 semantic-index contract.

This is Q1-B: an independent, brute-force cosine-similarity scorer and ranker
used as one of the two ground-truth oracles for the Q1 semantic-index
qualification. It consumes only the frozen Q1-A schema contract (external
record ID shape, `{1, 10, 50}` k values, little-endian float32 vectors); it
does not import `generate.py`, `canonical.py`, or `policy.py`, and shares no
code with the Q1-C cross-check oracle. It has no engine, storage, or product
dependency of any kind.

Loadable by file path with no package markers, matching the rest of `q1/`.

Every eligible active candidate is decoded and scored before any truncation
happens: `score_candidates` builds the complete scored list first, then sorts
and slices, so a malformed candidate anywhere in the input -- including after
enough valid candidates to fill `k` -- still fails closed before a result is
returned.
"""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

DEFAULT_DIMENSIONS: Final[int] = 768
K_VALUES: Final[tuple[int, int, int]] = (1, 10, 50)

# Matches the Q1-A frontier/ground-truth schema `external_record_id` pattern
# exactly: fixed-width ASCII IDs are what makes unsigned UTF-8 tie-breaking
# stable across languages.
_EXTERNAL_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"q1-record-(?:evidence|memory|knowledge)-[0-9]{6}"
)

# Cosine similarity is mathematically bounded to [-1, 1]; binary64 rounding
# in the dot-product/norm division can overshoot that bound by a few ULPs.
# Only an overshoot this tiny is clamped -- anything larger fails closed.
_SCORE_CLAMP_EPSILON: Final[float] = 8.0 * math.ulp(1.0)


def _require_dimensions(dimensions: object) -> int:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise TypeError(f"dimensions must be an int, got {type(dimensions)!r}")
    if dimensions <= 0:
        raise ValueError(f"dimensions must be positive, got {dimensions!r}")
    return dimensions


def _require_bytes_like(value: object, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{name} must be bytes-like, got {type(value)!r}")
    return bytes(value)


def decode_float32_le(
    payload: bytes | bytearray | memoryview, dimensions: int = DEFAULT_DIMENSIONS
) -> tuple[float, ...]:
    """Decode exactly `dimensions` little-endian float32 components.

    Rejects wrong-typed or wrong-length payloads, and any decoded component
    that is non-finite or negative zero.
    """
    _require_dimensions(dimensions)
    data = _require_bytes_like(payload, "payload")
    expected_length = dimensions * 4
    if len(data) != expected_length:
        raise ValueError(
            f"payload is {len(data)} bytes, expected {expected_length} "
            f"for dimensions={dimensions}"
        )
    decoded: list[float] = []
    for value in struct.unpack(f"<{dimensions}f", data):
        if not math.isfinite(value):
            raise ValueError(f"decoded component is not finite: {value!r}")
        if value == 0.0 and math.copysign(1.0, value) < 0.0:
            raise ValueError("decoded component is negative zero")
        decoded.append(value)
    return tuple(decoded)


@dataclass(frozen=True)
class Candidate:
    """One typed, immutable scoring candidate.

    Dimension-dependent vector validation happens in :func:`score_candidates`.
    """

    external_id: str
    vector: bytes | bytearray | memoryview
    active: bool
    eligible: bool

    def __post_init__(self) -> None:
        if isinstance(self.external_id, bool) or not isinstance(self.external_id, str):
            raise TypeError(
                f"external_id must be a str, got {type(self.external_id)!r}"
            )
        if _EXTERNAL_ID_RE.fullmatch(self.external_id) is None:
            raise ValueError(
                f"external_id does not match the schema pattern: {self.external_id!r}"
            )
        object.__setattr__(self, "vector", _require_bytes_like(self.vector, "vector"))
        if not isinstance(self.active, bool):
            raise TypeError(f"active must be a bool, got {type(self.active)!r}")
        if not isinstance(self.eligible, bool):
            raise TypeError(f"eligible must be a bool, got {type(self.eligible)!r}")


@dataclass(frozen=True)
class RankedResult:
    """One ranked scoring result: external ID and its exhaustive cosine score."""

    external_id: str
    score: float


def _validated_components(values: Sequence[float], name: str) -> tuple[float, ...]:
    validated: list[float] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name}[{index}] must be numeric, got {type(value)!r}")
        try:
            numeric = float(value)
        except OverflowError as exc:
            raise ValueError(
                f"{name}[{index}] cannot be represented in binary64"
            ) from exc
        if not math.isfinite(numeric):
            raise ValueError(f"{name}[{index}] is not finite: {numeric!r}")
        if numeric == 0.0 and math.copysign(1.0, numeric) < 0.0:
            raise ValueError(f"{name}[{index}] is negative zero")
        validated.append(numeric)
    return tuple(validated)


def _l2_norm(values: Sequence[float], name: str) -> float:
    norm = math.sqrt(math.fsum(value * value for value in values))
    if not math.isfinite(norm):
        raise ValueError(f"{name} norm is not finite: {norm!r}")
    if norm == 0.0:
        raise ValueError(f"{name} vector has zero norm")
    return norm


def _bounded_score(score: float) -> float:
    if not math.isfinite(score):
        raise ValueError(f"cosine score is not finite: {score!r}")
    if score > 1.0:
        if score - 1.0 > _SCORE_CLAMP_EPSILON:
            raise ValueError(f"cosine score out of range: {score!r}")
        return 1.0
    if score < -1.0:
        if -1.0 - score > _SCORE_CLAMP_EPSILON:
            raise ValueError(f"cosine score out of range: {score!r}")
        return -1.0
    # Ground-truth scores enter RFC 8785 evidence later; make orthogonal
    # results canonicalizable by never returning IEEE negative zero.
    return 0.0 if score == 0.0 else score


def _cosine_from_validated(
    query: Sequence[float],
    candidate: Sequence[float],
    query_norm: float,
    candidate_norm: float,
) -> float:
    denominator = query_norm * candidate_norm
    if not math.isfinite(denominator) or denominator == 0.0:
        raise ValueError(f"cosine denominator is invalid: {denominator!r}")
    dot = math.fsum(q * c for q, c in zip(query, candidate))
    return _bounded_score(dot / denominator)


def cosine_similarity(query: Sequence[float], candidate: Sequence[float]) -> float:
    """Exhaustive binary64 cosine similarity via `math.fsum` accumulation.

    Raises on dimension mismatch, either zero-norm vector, or a resulting
    score that is non-finite or out of `[-1, 1]` beyond a tiny rounding clamp.
    """
    query_values = _validated_components(query, "query")
    candidate_values = _validated_components(candidate, "candidate")
    if len(query_values) != len(candidate_values):
        raise ValueError(
            f"query has {len(query_values)} dimensions, "
            f"candidate has {len(candidate_values)}"
        )
    query_norm = _l2_norm(query_values, "query")
    candidate_norm = _l2_norm(candidate_values, "candidate")
    return _cosine_from_validated(
        query_values, candidate_values, query_norm, candidate_norm
    )


def _sort_key(entry: RankedResult) -> tuple[float, bytes]:
    # Descending score, then ascending unsigned UTF-8 external-ID byte order.
    return (-entry.score, entry.external_id.encode("utf-8"))


def score_candidates(
    query_vector: bytes | bytearray | memoryview,
    candidates: Iterable[Candidate],
    k: int,
    dimensions: int = DEFAULT_DIMENSIONS,
) -> tuple[RankedResult, ...]:
    """Exhaustively score every active-and-eligible candidate and return the
    top `k`, sorted descending by score then ascending by unsigned UTF-8
    external ID. `k` larger than the eligible count returns all of them; an
    empty eligible set returns an empty tuple. Never pads.

    Every candidate is validated and decoded before active/eligible filtering;
    every included candidate is then scored before sorting or truncation. A
    malformed candidate anywhere in the input therefore fails closed before a
    result is returned.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k not in K_VALUES:
        raise ValueError(f"k must be one of {K_VALUES}, got {k!r}")
    _require_dimensions(dimensions)
    query = decode_float32_le(query_vector, dimensions)
    query_norm = _l2_norm(query, "query")

    candidate_list = list(candidates)
    seen_ids: set[str] = set()
    validated_candidates: list[tuple[Candidate, tuple[float, ...], float]] = []
    for candidate in candidate_list:
        if not isinstance(candidate, Candidate):
            raise TypeError(
                f"candidates must contain Candidate instances, got {type(candidate)!r}"
            )
        if candidate.external_id in seen_ids:
            raise ValueError(f"duplicate external_id: {candidate.external_id!r}")
        seen_ids.add(candidate.external_id)
        vector = decode_float32_le(candidate.vector, dimensions)
        vector_norm = _l2_norm(vector, f"candidate {candidate.external_id!r}")
        validated_candidates.append((candidate, vector, vector_norm))

    scored: list[RankedResult] = []
    for candidate, vector, vector_norm in validated_candidates:
        if not (candidate.active and candidate.eligible):
            continue
        score = _cosine_from_validated(query, vector, query_norm, vector_norm)
        scored.append(RankedResult(candidate.external_id, score))

    scored.sort(key=_sort_key)
    return tuple(scored[:k])
