"""Q1-C independent scalar cross-check for the semantic-index qualification.

Standalone, standard-library-only re-implementation of the frozen
``q1.synthetic-768-cosine-v1`` profile's vector decode, unit-norm validation,
cosine scoring, and top-k ranking. It exists to catch bugs a shared
implementation would hide from itself, so it must not import Q1-A
(``canonical.py`` / ``generate.py`` / ``policy.py``), any Q1-B oracle, or any
product/package/engine/adapter/storage code. It loads by file path with no
package markers, exactly like the Q1-A modules it independently cross-checks.

Frozen contract:

- Profile ``q1.synthetic-768-cosine-v1``: 768 little-endian IEEE-754 float32
  scalars per vector, metric ``cosine_similarity``, normalization
  ``core_l2_unit_v1``.
- External record IDs: ``q1-record-(evidence|memory|knowledge)-[0-9]{6}``.
- ``k`` is exactly one of ``{1, 10, 50}``.
- Ranking: descending score, ties broken by ascending unsigned UTF-8
  ``record_id`` bytes.
"""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Sequence
from typing import Final, cast

DIMENSIONS: Final = 768
VECTOR_BYTE_LENGTH: Final = DIMENSIONS * 4
VALID_K: Final = (1, 10, 50)

# core_l2_unit_v1 vectors round-trip through float32, so each of the 768
# components carries at most ~2^-24 relative quantization error. Propagated
# through a sum of squares near 1.0, that bounds |norm - 1| to roughly 1e-7
# in the worst case. 1e-6 is a tight float32 validation allowance: still a
# full order of magnitude above that expected post-normalization round-trip
# error, while rejecting vectors that are not meaningfully unit-length.
NORM_TOLERANCE: Final = 1e-6

# cosine_similarity is mathematically bounded to [-1, 1]. This only absorbs
# float64 division/sqrt rounding at the boundary, not a real out-of-range
# score, which indicates a bug and must still raise.
SCORE_OVERSHOOT_TOLERANCE: Final = 1e-9

RECORD_ID_PATTERN: Final = re.compile(
    r"^q1-record-(?:evidence|memory|knowledge)-[0-9]{6}$"
)


class CrossCheckError(ValueError):
    """Raised for any Q1-C contract violation (value-level, not a type bug)."""


def decode_float32_le(data: object, dimensions: object) -> tuple[float, ...]:
    """Strictly decode ``dimensions`` little-endian float32 scalars from bytes.

    Performs no normalization or range validation -- only payload shape.
    """
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise TypeError(f"dimensions must be an int, got {dimensions!r}")
    if dimensions <= 0:
        raise CrossCheckError(f"dimensions must be positive, got {dimensions!r}")
    if not isinstance(data, bytes):
        raise TypeError(f"vector payload must be bytes, got {type(data)!r}")
    expected_length = dimensions * 4
    if len(data) != expected_length:
        raise CrossCheckError(
            f"vector payload must be exactly {expected_length} bytes for "
            f"{dimensions} float32 scalars, got {len(data)}"
        )
    values: tuple[float, ...] = struct.unpack(f"<{dimensions}f", data)
    return values


def _validate_unit_vector768(
    vector: Sequence[object], label: str
) -> tuple[tuple[float, ...], float]:
    """Validate one frozen-lane scalar vector; return it plus its sum-of-squares.

    Enforces exactly ``DIMENSIONS`` actual ``float`` scalars (no ``bool``/``int``
    duck-typing), all finite, no negative zero, and unit-normalized within
    ``NORM_TOLERANCE``. Never renormalizes: the returned values are exactly
    what was passed in.
    """
    if len(vector) != DIMENSIONS:
        raise CrossCheckError(
            f"vector {label} must have exactly {DIMENSIONS} scalars, got {len(vector)}"
        )
    values: list[float] = []
    sum_sq = 0.0
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, float):
            raise TypeError(f"vector {label} scalar must be float, got {type(value)!r}")
        if not math.isfinite(value):
            raise CrossCheckError(
                f"vector {label} contains a non-finite scalar: {value!r}"
            )
        if value == 0.0 and math.copysign(1.0, value) < 0.0:
            raise CrossCheckError(f"vector {label} contains a negative-zero scalar")
        sum_sq += value * value
        values.append(value)
    if sum_sq == 0.0:
        raise CrossCheckError(f"vector {label} has zero norm")
    norm = math.sqrt(sum_sq)
    if abs(norm - 1.0) > NORM_TOLERANCE:
        raise CrossCheckError(
            f"vector {label} is not core_l2_unit_v1 unit-normalized within "
            f"tolerance {NORM_TOLERANCE}: norm={norm!r}"
        )
    return tuple(values), sum_sq


def decode_unit_vector768(data: object) -> tuple[float, ...]:
    """Decode the frozen 768-scalar payload and validate ``core_l2_unit_v1``.

    Rejects empty/mismatched payloads, non-finite scalars, negative-zero
    scalars, zero-norm vectors, and vectors whose norm is not within
    ``NORM_TOLERANCE`` of 1.0. Never renormalizes: the returned values are
    exactly what the bytes decode to.
    """
    values = decode_float32_le(data, DIMENSIONS)
    validated, _ = _validate_unit_vector768(values, "vector")
    return validated


def _clamp_or_reject_score(score: float) -> float:
    """Clamp float64 rounding overshoot at the [-1, 1] boundary; else raise.

    ``score`` is mathematically bounded to [-1, 1]. Overshoot within
    ``SCORE_OVERSHOOT_TOLERANCE`` is float64 division/sqrt rounding noise and
    is clamped to the boundary. Anything past that tolerance is not
    achievable from valid unit-normalized inputs and indicates a real bug, so
    it still raises.
    """
    if not math.isfinite(score):
        raise CrossCheckError(
            f"cosine_similarity produced a non-finite score: {score!r}"
        )
    if score < -1.0:
        if score < -1.0 - SCORE_OVERSHOOT_TOLERANCE:
            raise CrossCheckError(f"cosine_similarity score out of bounds: {score!r}")
        return -1.0
    if score > 1.0:
        if score > 1.0 + SCORE_OVERSHOOT_TOLERANCE:
            raise CrossCheckError(f"cosine_similarity score out of bounds: {score!r}")
        return 1.0
    return score


def cosine_similarity(a: Sequence[object], b: Sequence[object]) -> float:
    """Scalar binary64 cosine similarity for two frozen-lane unit vectors.

    Strict and fail-closed: both inputs must be exactly ``DIMENSIONS`` actual
    ``float`` scalars, all finite, no negative zero, and unit-normalized
    within ``NORM_TOLERANCE``. Never renormalizes; clamping only absorbs
    float64 rounding at the [-1, 1] boundary, see ``_clamp_or_reject_score``.
    """
    va, norm_a_sq = _validate_unit_vector768(a, "a")
    vb, norm_b_sq = _validate_unit_vector768(b, "b")
    dot = 0.0
    for x, y in zip(va, vb):
        dot += x * y
    score = dot / (math.sqrt(norm_a_sq) * math.sqrt(norm_b_sq))
    return _clamp_or_reject_score(score)


def validate_record_id(record_id: object) -> str:
    """Validate the frozen external ID pattern and return it unchanged."""
    if not isinstance(record_id, str):
        raise TypeError(f"record_id must be str, got {type(record_id)!r}")
    if RECORD_ID_PATTERN.fullmatch(record_id) is None:
        raise CrossCheckError(
            f"record_id does not match the frozen pattern: {record_id!r}"
        )
    return record_id


def top_k(query_vector: object, records: object, k: object) -> list[tuple[str, float]]:
    """Rank eligible ``(record_id, vector_bytes)`` pairs against a query vector.

    Descending score, ties broken by ascending unsigned UTF-8 ``record_id``
    bytes. Returns fewer than ``k`` entries when fewer are eligible, and an
    empty list when none are eligible.
    """
    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError(f"k must be an int, got {k!r}")
    if k not in VALID_K:
        raise CrossCheckError(f"k must be one of {VALID_K}, got {k!r}")

    query = decode_unit_vector768(query_vector)

    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise TypeError(
            f"records must be a sequence of (record_id, vector) pairs, got {type(records)!r}"
        )
    record_entries = cast("Sequence[object]", records)

    seen_ids: set[str] = set()
    scored: list[tuple[str, float]] = []
    for entry in record_entries:
        if isinstance(entry, (str, bytes)) or not isinstance(entry, Sequence):
            raise CrossCheckError(
                f"malformed record entry, expected a 2-item pair: {entry!r}"
            )
        pair = cast("Sequence[object]", entry)
        if len(pair) != 2:
            raise CrossCheckError(
                f"malformed record entry, expected a 2-item pair: {entry!r}"
            )
        record_id = validate_record_id(pair[0])
        if record_id in seen_ids:
            raise CrossCheckError(f"duplicate record_id: {record_id!r}")
        seen_ids.add(record_id)
        vector = decode_unit_vector768(pair[1])
        score = cosine_similarity(query, vector)
        scored.append((record_id, score))

    scored.sort(key=lambda item: (-item[1], item[0].encode("utf-8")))
    return scored[:k]
