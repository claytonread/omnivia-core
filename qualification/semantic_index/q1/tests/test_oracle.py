"""Tests for oracle.py, imported by file path with no package markers."""

from __future__ import annotations

import ast
import importlib.util
import math
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

_DIR = Path(__file__).resolve().parents[1]


def _load(module_name: str, file_name: str) -> Any:
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _DIR / file_name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


oracle: Any = _load("q1_oracle_under_test", "oracle.py")


def _le(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def _id(lane: str, index: int) -> str:
    return f"q1-record-{lane}-{index:06d}"


def _unit768(index: int, value: float = 1.0) -> list[float]:
    """A 768-dim vector with `value` at `index` and zeros elsewhere.

    A single non-zero component is exactly L2-unit norm when `value` is
    +/-1.0, satisfying core_l2_unit_v1 without any float32 rounding risk.
    """
    vector = [0.0] * oracle.DEFAULT_DIMENSIONS
    vector[index] = value
    return vector


def _candidate(
    lane: str,
    index: int,
    values: list[float],
    *,
    active: bool = True,
    eligible: bool = True,
) -> Any:
    return oracle.Candidate(
        external_id=_id(lane, index),
        vector=_le(values),
        active=active,
        eligible=eligible,
    )


# --- decode_float32_le ------------------------------------------------------


def test_decode_float32_le_positive_known_values() -> None:
    values = [1.0, -2.5, 0.0, 3.25] + [0.0] * 764
    decoded = oracle.decode_float32_le(_le(values))
    assert decoded == tuple(values)
    assert isinstance(decoded, tuple)


def test_decode_float32_le_accepts_bytearray_and_memoryview() -> None:
    values = [1.0] * 768
    payload = _le(values)
    assert oracle.decode_float32_le(bytearray(payload)) == tuple(values)
    assert oracle.decode_float32_le(memoryview(payload)) == tuple(values)


def test_decode_float32_le_default_dimensions_is_768() -> None:
    assert oracle.DEFAULT_DIMENSIONS == 768
    payload = _le([0.5] * 768)
    decoded = oracle.decode_float32_le(payload)
    assert len(decoded) == 768


def test_decode_float32_le_distinguishes_little_from_big_endian() -> None:
    # Exact in float32, no round-trip drift.
    values = [1.5, -12345.25, 0.0625] + [0.0] * 765
    little = struct.pack("<768f", *values)
    big = struct.pack(">768f", *values)
    assert little != big
    decoded_little = oracle.decode_float32_le(little)
    decoded_big = oracle.decode_float32_le(big)
    assert decoded_little == tuple(values)
    assert decoded_big != decoded_little


def test_decode_float32_le_rejects_wrong_byte_length() -> None:
    too_short = _le([1.0] * 767)
    too_long = _le([1.0] * 769)
    with pytest.raises(ValueError):
        oracle.decode_float32_le(too_short)
    with pytest.raises(ValueError):
        oracle.decode_float32_le(too_long)


def test_decode_float32_le_rejects_non_finite_components() -> None:
    nan_payload = struct.pack("<768f", float("nan"), *([1.0] * 767))
    inf_payload = struct.pack("<768f", float("inf"), *([1.0] * 767))
    with pytest.raises(ValueError):
        oracle.decode_float32_le(nan_payload)
    with pytest.raises(ValueError):
        oracle.decode_float32_le(inf_payload)


def test_decode_float32_le_rejects_negative_zero() -> None:
    payload = struct.pack("<768f", -0.0, *([1.0] * 767))
    with pytest.raises(ValueError):
        oracle.decode_float32_le(payload)


def test_decode_float32_le_rejects_non_bytes_like_payload() -> None:
    for bad in ("not bytes", [1.0, 2.0], 12345, None):
        with pytest.raises(TypeError):
            oracle.decode_float32_le(bad)


@pytest.mark.parametrize(
    "bad_dimensions",
    # 2, 512, 767, 769 are valid ints but not the admitted 768 profile.
    [0, -1, 2, 512, 767, 769, True, False, 1.5, "768", None],
)
def test_decode_float32_le_rejects_non_768_dimensions(bad_dimensions: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        oracle.decode_float32_le(b"\x00" * 4, dimensions=bad_dimensions)


# --- Candidate ---------------------------------------------------------------


def test_candidate_accepts_well_formed_record() -> None:
    candidate = _candidate("evidence", 1, _unit768(0))
    assert candidate.external_id == "q1-record-evidence-000001"
    assert candidate.active is True
    assert candidate.eligible is True
    assert isinstance(candidate.vector, bytes)


@pytest.mark.parametrize(
    "bad_id",
    [
        "not-an-id",
        "q1-record-evidence-1",
        "q1-record-evidence-0000001",
        "q1-record-unknownlane-000001",
        "q1-record-evidence-00000a",
        "q1-record-évidence-000001",
        "q1-record-evidence-000001\n",
        "q1-record-evidence-000001\x00",
        "",
        123,
        None,
        True,
    ],
)
def test_candidate_rejects_malformed_external_id(bad_id: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        oracle.Candidate(
            external_id=bad_id, vector=_le([1.0]), active=True, eligible=True
        )


@pytest.mark.parametrize("bad_vector", ["not bytes", [1.0, 2.0], 12345, None])
def test_candidate_rejects_non_bytes_like_vector(bad_vector: object) -> None:
    with pytest.raises(TypeError):
        oracle.Candidate(
            external_id=_id("evidence", 1),
            vector=bad_vector,
            active=True,
            eligible=True,
        )


@pytest.mark.parametrize("bad_flag", [1, 0, "true", None, 1.0])
def test_candidate_rejects_non_bool_flags(bad_flag: object) -> None:
    with pytest.raises(TypeError):
        oracle.Candidate(
            external_id=_id("evidence", 1),
            vector=_le([1.0]),
            active=bad_flag,
            eligible=True,
        )
    with pytest.raises(TypeError):
        oracle.Candidate(
            external_id=_id("evidence", 1),
            vector=_le([1.0]),
            active=True,
            eligible=bad_flag,
        )


# --- cosine_similarity --------------------------------------------------------


def test_cosine_similarity_known_rankings() -> None:
    query = tuple(_unit768(0))
    identical = tuple(_unit768(0))
    orthogonal = tuple(_unit768(1))
    opposite = tuple(_unit768(0, value=-1.0))
    assert oracle.cosine_similarity(query, identical) == pytest.approx(1.0)
    assert oracle.cosine_similarity(query, orthogonal) == pytest.approx(0.0)
    assert oracle.cosine_similarity(query, opposite) == pytest.approx(-1.0)


def test_cosine_similarity_rejects_zero_norm_vectors() -> None:
    with pytest.raises(ValueError):
        oracle.cosine_similarity(tuple([0.0] * 768), tuple(_unit768(0)))
    with pytest.raises(ValueError):
        oracle.cosine_similarity(tuple(_unit768(0)), tuple([0.0] * 768))


def test_cosine_similarity_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError):
        oracle.cosine_similarity(tuple(_unit768(0)), tuple([1.0] + [0.0] * 766))


@pytest.mark.parametrize("dimensions", [0, 1, 2, 10, 767, 769])
def test_cosine_similarity_rejects_equal_non_768_dimensions(dimensions: int) -> None:
    vector = tuple([1.0] + [0.0] * (dimensions - 1)) if dimensions else ()
    with pytest.raises(ValueError):
        oracle.cosine_similarity(vector, vector)


@pytest.mark.parametrize(
    "query,candidate",
    [
        (_unit768(0, value=0.5), _unit768(0)),
        (_unit768(0), _unit768(0, value=2.0)),
    ],
)
def test_cosine_similarity_rejects_non_unit_vectors(
    query: list[float], candidate: list[float]
) -> None:
    with pytest.raises(ValueError):
        oracle.cosine_similarity(query, candidate)


def test_cosine_similarity_bounds_are_finite_and_in_range() -> None:
    query = tuple(_unit768(0))
    candidate = tuple([0.6, 0.8] + [0.0] * 766)
    score = oracle.cosine_similarity(query, candidate)
    assert math.isfinite(score)
    assert -1.0 <= score <= 1.0


def test_cosine_similarity_returns_positive_zero() -> None:
    score = oracle.cosine_similarity(tuple(_unit768(0)), tuple(_unit768(1, value=-1.0)))
    assert score == 0.0
    assert math.copysign(1.0, score) == 1.0


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), -0.0, True, "1.0"],
)
def test_cosine_similarity_rejects_invalid_components(bad_value: object) -> None:
    bad_values: list[object] = [0.0] * 768
    bad_values[0] = bad_value
    with pytest.raises((TypeError, ValueError)):
        oracle.cosine_similarity(bad_values, tuple(_unit768(0)))


def test_cosine_similarity_matches_fsum_accumulation_not_naive_summation() -> None:
    # Normalize the classic ten-0.1 versus ten-1.0 example into the admitted
    # 768D profile. Naive accumulation still diverges by one binary64 ULP.
    query_base = [0.1] * 10
    candidate_base = [1.0] * 10
    query_scale = math.sqrt(math.fsum(value * value for value in query_base))
    candidate_scale = math.sqrt(math.fsum(value * value for value in candidate_base))
    query = tuple([value / query_scale for value in query_base] + [0.0] * 758)
    candidate = tuple(
        [value / candidate_scale for value in candidate_base] + [0.0] * 758
    )

    def naive_score(q: tuple[float, ...], c: tuple[float, ...]) -> float:
        dot = 0.0
        for a, b in zip(q, c):
            dot += a * b
        qn = 0.0
        for a in q:
            qn += a * a
        cn = 0.0
        for b in c:
            cn += b * b
        return dot / (math.sqrt(qn) * math.sqrt(cn))

    def fsum_score(q: tuple[float, ...], c: tuple[float, ...]) -> float:
        dot = math.fsum(a * b for a, b in zip(q, c))
        qn = math.sqrt(math.fsum(a * a for a in q))
        cn = math.sqrt(math.fsum(b * b for b in c))
        return dot / (qn * cn)

    naive = naive_score(query, candidate)
    exact = fsum_score(query, candidate)
    # The two accumulation strategies must actually diverge for this input --
    # otherwise this test would prove nothing about which one is in use.
    assert naive != exact
    actual = oracle.cosine_similarity(query, candidate)
    assert actual == exact
    assert actual != naive


# --- score overshoot clamp: frozen at exactly 1e-12 --------------------------


def test_score_clamp_epsilon_is_frozen_at_1e_minus_12() -> None:
    assert oracle._SCORE_CLAMP_EPSILON == 1e-12


def test_bounded_score_clamps_at_exact_positive_boundary() -> None:
    boundary = 1.0 + oracle._SCORE_CLAMP_EPSILON
    assert oracle._bounded_score(1.0) == 1.0
    assert oracle._bounded_score(boundary) == 1.0


def test_bounded_score_rejects_just_above_positive_boundary() -> None:
    boundary = 1.0 + oracle._SCORE_CLAMP_EPSILON
    just_over = math.nextafter(boundary, math.inf)
    with pytest.raises(ValueError):
        oracle._bounded_score(just_over)


def test_bounded_score_clamps_at_exact_negative_boundary() -> None:
    boundary = -1.0 - oracle._SCORE_CLAMP_EPSILON
    assert oracle._bounded_score(-1.0) == -1.0
    assert oracle._bounded_score(boundary) == -1.0


def test_bounded_score_rejects_just_below_negative_boundary() -> None:
    boundary = -1.0 - oracle._SCORE_CLAMP_EPSILON
    just_under = math.nextafter(boundary, -math.inf)
    with pytest.raises(ValueError):
        oracle._bounded_score(just_under)


# --- core_l2_unit_v1: independent bounded L2 norm check -----------------------


def test_core_l2_unit_tolerance_is_frozen_at_1e_minus_6() -> None:
    assert oracle._CORE_L2_UNIT_TOLERANCE == 1e-6


def test_require_core_l2_unit_norm_accepts_within_tolerance() -> None:
    tolerance = oracle._CORE_L2_UNIT_TOLERANCE
    values = _unit768(0, value=1.0 + tolerance / 2.0)
    norm = oracle._require_core_l2_unit_norm(values, "test")
    assert norm == pytest.approx(1.0 + tolerance / 2.0)


def test_require_core_l2_unit_norm_rejects_outside_tolerance() -> None:
    tolerance = oracle._CORE_L2_UNIT_TOLERANCE
    values = _unit768(0, value=1.0 + tolerance * 5.0)
    with pytest.raises(ValueError):
        oracle._require_core_l2_unit_norm(values, "test")


def test_require_core_l2_unit_norm_rejects_zero_vector() -> None:
    with pytest.raises(ValueError):
        oracle._require_core_l2_unit_norm([0.0] * 768, "test")


def test_require_core_l2_unit_norm_never_rescales_the_vector() -> None:
    # The returned norm is the actual measured value, not 1.0 -- proving the
    # vector itself is never renormalized/rewritten to force unit norm.
    tolerance = oracle._CORE_L2_UNIT_TOLERANCE
    values = _unit768(0, value=1.0 + tolerance / 2.0)
    norm = oracle._require_core_l2_unit_norm(values, "test")
    assert norm != 1.0


# --- score_candidates: filtering, k, ordering --------------------------------


def test_score_candidates_filters_inactive_and_ineligible() -> None:
    query = _le(_unit768(0))
    candidates = [
        _candidate("evidence", 1, _unit768(0), active=False, eligible=True),
        _candidate("evidence", 2, _unit768(0), active=True, eligible=False),
        _candidate("evidence", 3, _unit768(0), active=True, eligible=True),
    ]
    results = oracle.score_candidates(query, candidates, k=10)
    assert [r.external_id for r in results] == ["q1-record-evidence-000003"]


def test_score_candidates_empty_eligible_set_returns_empty() -> None:
    query = _le(_unit768(0))
    candidates = [
        _candidate("evidence", 1, _unit768(0), active=False, eligible=True),
        _candidate("evidence", 2, _unit768(0), active=True, eligible=False),
    ]
    results = oracle.score_candidates(query, candidates, k=10)
    assert results == ()


def test_score_candidates_no_candidates_returns_empty() -> None:
    query = _le(_unit768(0))
    assert oracle.score_candidates(query, [], k=1) == ()


def test_score_candidates_rejects_zero_query_even_for_empty_frontier() -> None:
    with pytest.raises(ValueError):
        oracle.score_candidates(_le([0.0] * 768), [], k=1)


@pytest.mark.parametrize("k", [1, 10, 50])
def test_score_candidates_exact_k_fewer_candidates_than_k(k: int) -> None:
    query = _le(_unit768(0))
    candidates = [_candidate("evidence", i, _unit768(0)) for i in range(1, 4)]
    results = oracle.score_candidates(query, candidates, k=k)
    assert len(results) == min(3, k)


def test_score_candidates_k_truncates_and_never_pads() -> None:
    query = _le(_unit768(0))
    candidates = [_candidate("evidence", i, _unit768(0)) for i in range(1, 21)]
    results = oracle.score_candidates(query, candidates, k=10)
    assert len(results) == 10


@pytest.mark.parametrize("bad_k", [0, 2, 5, 100, -1, True, False, 10.0, "10", None])
def test_score_candidates_rejects_invalid_k(bad_k: object) -> None:
    query = _le(_unit768(0))
    candidates = [_candidate("evidence", 1, _unit768(0))]
    with pytest.raises((TypeError, ValueError)):
        oracle.score_candidates(query, candidates, k=bad_k)


@pytest.mark.parametrize(
    "bad_dimensions",
    # 2, 512, 767, 769 are valid ints but not the admitted 768 profile.
    [0, -1, 2, 512, 767, 769, True, False, 1.5, "768", None],
)
def test_score_candidates_rejects_non_768_dimensions(bad_dimensions: object) -> None:
    query = _le(_unit768(0))
    candidates = [_candidate("evidence", 1, _unit768(0))]
    with pytest.raises((TypeError, ValueError)):
        oracle.score_candidates(query, candidates, k=1, dimensions=bad_dimensions)


def test_score_candidates_rejects_non_candidate_items() -> None:
    query = _le(_unit768(0))
    with pytest.raises(TypeError):
        oracle.score_candidates(query, [{"external_id": "x"}], k=1)


def test_score_candidates_rejects_duplicate_external_ids() -> None:
    query = _le(_unit768(0))
    candidates = [
        _candidate("evidence", 1, _unit768(0)),
        _candidate("evidence", 1, _unit768(1)),
    ]
    with pytest.raises(ValueError):
        oracle.score_candidates(query, candidates, k=10)


def test_score_candidates_rejects_dimension_mismatched_candidate_vector() -> None:
    query = _le(_unit768(0))
    bad_candidate = oracle.Candidate(
        external_id=_id("evidence", 1),
        vector=_le([1.0] * 767),  # wrong length for the fixed 768 profile
        active=True,
        eligible=True,
    )
    with pytest.raises(ValueError):
        oracle.score_candidates(query, [bad_candidate], k=1)


@pytest.mark.parametrize("active,eligible", [(False, True), (True, False)])
def test_score_candidates_validates_filtered_candidate_vectors(
    active: bool, eligible: bool
) -> None:
    query = _le(_unit768(0))
    bad_length = oracle.Candidate(
        external_id=_id("evidence", 1),
        vector=_le([1.0] * 767),
        active=active,
        eligible=eligible,
    )
    with pytest.raises(ValueError):
        oracle.score_candidates(query, [bad_length], k=1)

    zero_norm = oracle.Candidate(
        external_id=_id("evidence", 2),
        vector=_le([0.0] * 768),
        active=active,
        eligible=eligible,
    )
    with pytest.raises(ValueError):
        oracle.score_candidates(query, [zero_norm], k=1)


# --- core_l2_unit_v1 enforced for query and every candidate, incl. filtered --


def test_score_candidates_rejects_non_unit_norm_query() -> None:
    query = _le(_unit768(0, value=1.5))
    candidates = [_candidate("evidence", 1, _unit768(0))]
    with pytest.raises(ValueError):
        oracle.score_candidates(query, candidates, k=1)


@pytest.mark.parametrize(
    "active,eligible", [(False, True), (True, False), (False, False)]
)
def test_score_candidates_rejects_non_unit_norm_candidate_regardless_of_flags(
    active: bool, eligible: bool
) -> None:
    query = _le(_unit768(0))
    off_norm = oracle.Candidate(
        external_id=_id("evidence", 1),
        vector=_le(_unit768(0, value=2.0)),  # norm 2.0, violates core_l2_unit_v1
        active=active,
        eligible=eligible,
    )
    with pytest.raises(ValueError):
        oracle.score_candidates(query, [off_norm], k=1)


def test_score_candidates_never_renormalizes_a_non_unit_candidate() -> None:
    # A candidate scaled well outside tolerance must fail closed, not be
    # silently rescaled back to unit norm and scored anyway.
    query = _le(_unit768(0))
    scaled = oracle.Candidate(
        external_id=_id("evidence", 1),
        vector=_le(_unit768(0, value=0.5)),  # norm 0.5, violates core_l2_unit_v1
        active=True,
        eligible=True,
    )
    with pytest.raises(ValueError):
        oracle.score_candidates(query, [scaled], k=1)


# --- ordering: descending score, ties by unsigned UTF-8 external ID ---------


def test_score_candidates_orders_descending_by_score() -> None:
    query = _le(_unit768(0))
    candidates = [
        _candidate("evidence", 1, _unit768(1)),  # orthogonal -> 0.0
        _candidate("evidence", 2, _unit768(0)),  # identical -> 1.0
        _candidate("evidence", 3, _unit768(0, value=-1.0)),  # opposite -> -1.0
    ]
    results = oracle.score_candidates(query, candidates, k=10)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    assert [r.external_id for r in results] == [
        "q1-record-evidence-000002",
        "q1-record-evidence-000001",
        "q1-record-evidence-000003",
    ]


def test_score_candidates_ties_break_by_unsigned_utf8_external_id() -> None:
    query = _le(_unit768(0))
    tied_values = _unit768(0)
    candidates_in_order = [
        _candidate("knowledge", 5, tied_values),
        _candidate("evidence", 9, tied_values),
        _candidate("memory", 1, tied_values),
        _candidate("evidence", 2, tied_values),
    ]
    results = oracle.score_candidates(query, candidates_in_order, k=10)
    ids = [r.external_id for r in results]
    assert ids == sorted(ids, key=lambda v: v.encode("utf-8"))
    assert all(r.score == pytest.approx(1.0) for r in results)


def test_score_candidates_tie_order_is_input_order_independent() -> None:
    query = _le(_unit768(0))
    tied_values = _unit768(0)
    base = [
        _candidate("knowledge", 5, tied_values),
        _candidate("evidence", 9, tied_values),
        _candidate("memory", 1, tied_values),
        _candidate("evidence", 2, tied_values),
    ]
    forward = oracle.score_candidates(query, base, k=10)
    reversed_input = oracle.score_candidates(query, list(reversed(base)), k=10)
    assert [r.external_id for r in forward] == [r.external_id for r in reversed_input]


# --- exhaustive scoring evidence ---------------------------------------------


def test_score_candidates_scores_every_eligible_candidate_before_truncating() -> None:
    # k=1 with three valid eligible candidates ahead of a malformed one at the
    # tail. A scorer that stops after collecting k good scores would return a
    # top-1 result and never touch the malformed tail; this oracle must fail
    # closed instead, proving it scored every eligible candidate first.
    query = _le(_unit768(0))
    good = [_candidate("evidence", i, _unit768(0)) for i in range(1, 4)]
    malformed = oracle.Candidate(
        external_id=_id("evidence", 9),
        vector=_le([1.0] * 767),  # wrong length for the fixed 768 profile
        active=True,
        eligible=True,
    )
    with pytest.raises(ValueError):
        oracle.score_candidates(query, [*good, malformed], k=1)


def test_score_candidates_consumes_a_one_shot_iterable_fully() -> None:
    query = _le(_unit768(0))
    candidates = [_candidate("evidence", i, _unit768(0)) for i in range(1, 21)]
    pulled: list[str] = []

    def one_shot() -> Any:
        for candidate in candidates:
            pulled.append(candidate.external_id)
            yield candidate

    results = oracle.score_candidates(query, one_shot(), k=1)
    assert len(results) == 1
    # Every candidate was pulled from the one-shot generator despite k=1,
    # proving no premature top-k short-circuit on the input iterable.
    assert len(pulled) == 20
    assert pulled == [c.external_id for c in candidates]


# --- no forbidden Q1-A implementation imports --------------------------------


def test_oracle_module_does_not_import_forbidden_q1a_modules() -> None:
    import types

    forbidden_basenames = {"generate.py", "canonical.py", "policy.py"}
    imported_modules = [
        value for value in vars(oracle).values() if isinstance(value, types.ModuleType)
    ]
    forbidden_hits = [
        module
        for module in imported_modules
        if (module_file := getattr(module, "__file__", None)) is not None
        and Path(module_file).name in forbidden_basenames
    ]
    assert forbidden_hits == []


def test_oracle_source_has_no_forbidden_import_statements() -> None:
    source = (_DIR / "oracle.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported_names.isdisjoint({"generate", "canonical", "policy"})
