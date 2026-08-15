"""Tests for the standalone Q1-C scalar cross-check.

Loads ``crosscheck.py`` by file path (no package markers, matching the rest
of Q1) and never imports Q1-A (``canonical`` / ``generate`` / ``policy``) or
any Q1-B oracle implementation -- all fixture bytes are built directly with
``struct`` so this test suite stays as independent as the module it checks.
"""

from __future__ import annotations

import ast
import importlib.util
import math
import struct
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "crosscheck.py"


def _load_crosscheck() -> ModuleType:
    spec = importlib.util.spec_from_file_location("q1_crosscheck", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cc = _load_crosscheck()

DIM = cc.DIMENSIONS


def pack_le(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def pack_be(values: list[float]) -> bytes:
    return struct.pack(f">{len(values)}f", *values)


def axis_vector(index: int, value: float = 1.0) -> list[float]:
    vals = [0.0] * DIM
    vals[index] = value
    return vals


def uniform_vector(value: float) -> list[float]:
    return [value] * DIM


UNIT_UNIFORM_NEGATIVE = uniform_vector(-1.0 / math.sqrt(DIM))
E0 = axis_vector(0, 1.0)
E1 = axis_vector(1, 1.0)
NEG_E0 = axis_vector(0, -1.0)


# --- decode: known-good little-endian bytes -------------------------------


def test_decode_known_little_endian_bytes() -> None:
    data = pack_le(E0)
    decoded = cc.decode_unit_vector768(data)
    assert decoded[0] == 1.0
    assert all(v == 0.0 for v in decoded[1:])


def test_decode_actual_768_dimensional_input() -> None:
    data = pack_le(UNIT_UNIFORM_NEGATIVE)
    decoded = cc.decode_unit_vector768(data)
    assert len(decoded) == 768


# --- decode: rejection cases -------------------------------------------


def test_decode_rejects_empty_payload() -> None:
    with pytest.raises(cc.CrossCheckError):
        cc.decode_unit_vector768(b"")


def test_decode_rejects_misaligned_length() -> None:
    data = pack_le(E0)[:-1]
    with pytest.raises(cc.CrossCheckError):
        cc.decode_unit_vector768(data)


def test_decode_rejects_wrong_dimension() -> None:
    short_vector = [0.0] * 767
    short_vector[0] = 1.0
    data = pack_le(short_vector)
    with pytest.raises(cc.CrossCheckError):
        cc.decode_unit_vector768(data)


def test_decode_float32_le_supports_explicit_dimension() -> None:
    short_vector = [1.0, 0.0, 0.0]
    data = pack_le(short_vector)
    decoded = cc.decode_float32_le(data, 3)
    assert decoded == (1.0, 0.0, 0.0)


def test_decode_float32_le_rejects_bool_dimension() -> None:
    with pytest.raises(TypeError):
        cc.decode_float32_le(pack_le(E0), True)


def test_decode_float32_le_rejects_non_int_dimension() -> None:
    with pytest.raises(TypeError):
        cc.decode_float32_le(pack_le(E0), 768.0)


def test_decode_rejects_non_bytes() -> None:
    with pytest.raises(TypeError):
        cc.decode_unit_vector768(list(E0))
    with pytest.raises(TypeError):
        cc.decode_unit_vector768("not bytes")
    with pytest.raises(TypeError):
        cc.decode_unit_vector768(bytearray(pack_le(E0)))


def test_decode_rejects_nonfinite_scalar() -> None:
    vals = list(E0)
    data = bytearray(pack_le(vals))
    data[0:4] = struct.pack("<f", float("nan"))
    with pytest.raises(cc.CrossCheckError):
        cc.decode_unit_vector768(bytes(data))


def test_decode_rejects_infinite_scalar() -> None:
    vals = list(E0)
    data = bytearray(pack_le(vals))
    data[0:4] = struct.pack("<f", float("inf"))
    with pytest.raises(cc.CrossCheckError):
        cc.decode_unit_vector768(bytes(data))


def test_decode_rejects_negative_zero_scalar() -> None:
    vals = list(E1)
    data = bytearray(pack_le(vals))
    data[0:4] = struct.pack("<f", -0.0)
    with pytest.raises(cc.CrossCheckError):
        cc.decode_unit_vector768(bytes(data))


def test_decode_rejects_zero_vector() -> None:
    data = pack_le([0.0] * DIM)
    with pytest.raises(cc.CrossCheckError):
        cc.decode_unit_vector768(data)


def test_decode_rejects_non_unit_vector() -> None:
    data = pack_le(uniform_vector(0.5))
    with pytest.raises(cc.CrossCheckError):
        cc.decode_unit_vector768(data)


def test_decode_rejects_big_endian_payload() -> None:
    data = pack_be(UNIT_UNIFORM_NEGATIVE)
    with pytest.raises(cc.CrossCheckError):
        cc.decode_unit_vector768(data)


# --- normalization: positives, including all-negative components ------


def test_decode_accepts_axis_unit_vector() -> None:
    cc.decode_unit_vector768(pack_le(E0))


def test_decode_accepts_all_negative_unit_vector() -> None:
    decoded = cc.decode_unit_vector768(pack_le(UNIT_UNIFORM_NEGATIVE))
    assert all(v < 0.0 for v in decoded)


def test_decode_does_not_renormalize() -> None:
    data = pack_le(UNIT_UNIFORM_NEGATIVE)
    decoded = cc.decode_unit_vector768(data)
    expected = struct.unpack(f"<{DIM}f", data)
    assert decoded == expected


# --- cosine_similarity ---------------------------------------------------


def test_cosine_similarity_identical_vectors() -> None:
    a = cc.decode_unit_vector768(pack_le(E0))
    assert cc.cosine_similarity(a, a) == 1.0


def test_cosine_similarity_opposite_vectors() -> None:
    a = cc.decode_unit_vector768(pack_le(E0))
    b = cc.decode_unit_vector768(pack_le(NEG_E0))
    assert cc.cosine_similarity(a, b) == -1.0


def test_cosine_similarity_orthogonal_vectors() -> None:
    a = cc.decode_unit_vector768(pack_le(E0))
    b = cc.decode_unit_vector768(pack_le(E1))
    assert cc.cosine_similarity(a, b) == 0.0


def test_cosine_similarity_clamps_bounded_overshoot() -> None:
    a = (1.0 + 1e-10,) + (0.0,) * (DIM - 1)
    b = (1.0,) + (0.0,) * (DIM - 1)
    score = cc.cosine_similarity(a, b)
    assert score == 1.0


def test_cosine_similarity_rejects_length_mismatch() -> None:
    with pytest.raises(cc.CrossCheckError):
        cc.cosine_similarity(list(E0), list(E0)[:-1])


def test_cosine_similarity_rejects_short_vectors() -> None:
    with pytest.raises(cc.CrossCheckError):
        cc.cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0])


def test_cosine_similarity_rejects_zero_vector() -> None:
    with pytest.raises(cc.CrossCheckError):
        cc.cosine_similarity([0.0] * DIM, list(E0))


def test_cosine_similarity_rejects_nonfinite_scalar() -> None:
    a = list(E0)
    a[1] = float("nan")
    with pytest.raises(cc.CrossCheckError):
        cc.cosine_similarity(a, list(E0))


def test_cosine_similarity_rejects_negative_zero_scalar() -> None:
    a = list(E1)
    a[0] = -0.0
    with pytest.raises(cc.CrossCheckError):
        cc.cosine_similarity(a, list(E0))


def test_cosine_similarity_rejects_non_unit_vector() -> None:
    with pytest.raises(cc.CrossCheckError):
        cc.cosine_similarity(uniform_vector(0.5), list(E0))


def test_cosine_similarity_rejects_invalid_scalar_type() -> None:
    a = cast(list[float], ["not-a-float"] * DIM)
    with pytest.raises(TypeError):
        cc.cosine_similarity(a, list(E0))


def test_cosine_similarity_rejects_bool_scalar() -> None:
    a = cast(list[float], [True] + [0.0] * (DIM - 1))
    with pytest.raises(TypeError):
        cc.cosine_similarity(a, list(E0))


def test_cosine_similarity_rejects_int_scalar() -> None:
    a = cast(list[float], [1] + [0.0] * (DIM - 1))
    with pytest.raises(TypeError):
        cc.cosine_similarity(a, list(E0))


# --- _clamp_or_reject_score (private, direct) -----------------------------


def test_clamp_or_reject_score_clamps_positive_overshoot_within_tolerance() -> None:
    score = 1.0 + (cc.SCORE_OVERSHOOT_TOLERANCE / 2)
    assert cc._clamp_or_reject_score(score) == 1.0


def test_clamp_or_reject_score_clamps_negative_overshoot_within_tolerance() -> None:
    score = -1.0 - (cc.SCORE_OVERSHOOT_TOLERANCE / 2)
    assert cc._clamp_or_reject_score(score) == -1.0


def test_clamp_or_reject_score_rejects_positive_gross_overshoot() -> None:
    score = 1.0 + (cc.SCORE_OVERSHOOT_TOLERANCE * 10)
    with pytest.raises(cc.CrossCheckError):
        cc._clamp_or_reject_score(score)


def test_clamp_or_reject_score_rejects_negative_gross_overshoot() -> None:
    score = -1.0 - (cc.SCORE_OVERSHOOT_TOLERANCE * 10)
    with pytest.raises(cc.CrossCheckError):
        cc._clamp_or_reject_score(score)


def test_clamp_or_reject_score_rejects_nonfinite() -> None:
    with pytest.raises(cc.CrossCheckError):
        cc._clamp_or_reject_score(float("nan"))
    with pytest.raises(cc.CrossCheckError):
        cc._clamp_or_reject_score(float("inf"))
    with pytest.raises(cc.CrossCheckError):
        cc._clamp_or_reject_score(float("-inf"))


# --- record IDs -----------------------------------------------------------


@pytest.mark.parametrize(
    "record_id",
    [
        "q1-record-evidence-000001",
        "q1-record-memory-000042",
        "q1-record-knowledge-999999",
    ],
)
def test_validate_record_id_accepts_valid_ids(record_id: str) -> None:
    assert cc.validate_record_id(record_id) == record_id


@pytest.mark.parametrize(
    "record_id",
    [
        "q1-record-evidence-1",
        "q1-record-evidence-0000001",
        "q1-record-bogus-000001",
        "Q1-RECORD-EVIDENCE-000001",
        "q1-record-evidence-000001 ",
        "",
    ],
)
def test_validate_record_id_rejects_invalid_ids(record_id: str) -> None:
    with pytest.raises(cc.CrossCheckError):
        cc.validate_record_id(record_id)


def test_validate_record_id_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        cc.validate_record_id(123)


# --- top_k ------------------------------------------------------------


def _records(n: int) -> list[tuple[str, bytes]]:
    """n records, each a distinct axis-aligned unit vector."""
    out = []
    for i in range(n):
        vec = axis_vector(i % DIM, 1.0)
        out.append((f"q1-record-evidence-{i:06d}", pack_le(vec)))
    return out


def test_top_k_ranks_by_descending_score() -> None:
    query = pack_le(E0)
    records = [
        ("q1-record-evidence-000002", pack_le(E1)),
        ("q1-record-evidence-000001", pack_le(E0)),
        ("q1-record-evidence-000003", pack_le(NEG_E0)),
    ]
    result = cc.top_k(query, records, 10)
    assert [r[0] for r in result] == [
        "q1-record-evidence-000001",
        "q1-record-evidence-000002",
        "q1-record-evidence-000003",
    ]
    assert result[0][1] == 1.0
    assert result[1][1] == 0.0
    assert result[2][1] == -1.0


def test_top_k_ties_broken_by_ascending_utf8_record_id() -> None:
    query = pack_le(E1)
    records = [
        ("q1-record-knowledge-000001", pack_le(E1)),
        ("q1-record-evidence-000001", pack_le(E1)),
        ("q1-record-memory-000001", pack_le(E1)),
    ]
    result = cc.top_k(query, records, 10)
    assert [r[0] for r in result] == [
        "q1-record-evidence-000001",
        "q1-record-knowledge-000001",
        "q1-record-memory-000001",
    ]
    assert result[0][1] == result[1][1] == result[2][1] == 1.0


@pytest.mark.parametrize("k", [1, 10, 50])
def test_top_k_respects_k(k: int) -> None:
    query = pack_le(E0)
    records = _records(60)
    result = cc.top_k(query, records, k)
    assert len(result) == k


def test_top_k_fewer_eligible_than_k_returns_all() -> None:
    query = pack_le(E0)
    records = _records(3)
    result = cc.top_k(query, records, 50)
    assert len(result) == 3


def test_top_k_empty_returns_none() -> None:
    query = pack_le(E0)
    result = cc.top_k(query, [], 10)
    assert result == []


@pytest.mark.parametrize("k", [0, 2, 11, 51, -1, 100])
def test_top_k_rejects_invalid_k_value(k: int) -> None:
    with pytest.raises(cc.CrossCheckError):
        cc.top_k(pack_le(E0), [], k)


def test_top_k_rejects_non_int_k() -> None:
    with pytest.raises(TypeError):
        cc.top_k(pack_le(E0), [], 10.0)
    with pytest.raises(TypeError):
        cc.top_k(pack_le(E0), [], True)


def test_top_k_rejects_duplicate_ids() -> None:
    query = pack_le(E0)
    records = [
        ("q1-record-evidence-000001", pack_le(E0)),
        ("q1-record-evidence-000001", pack_le(E1)),
    ]
    with pytest.raises(cc.CrossCheckError):
        cc.top_k(query, records, 1)


def test_top_k_rejects_invalid_record_id() -> None:
    query = pack_le(E0)
    records = [("not-a-valid-id", pack_le(E0))]
    with pytest.raises(cc.CrossCheckError):
        cc.top_k(query, records, 1)


def test_top_k_rejects_malformed_vector() -> None:
    query = pack_le(E0)
    records = [("q1-record-evidence-000001", pack_le(uniform_vector(0.5)))]
    with pytest.raises(cc.CrossCheckError):
        cc.top_k(query, records, 1)


@pytest.mark.parametrize(
    "records",
    [
        "not-a-sequence-of-pairs",
        b"not-a-sequence-of-pairs",
        123,
    ],
)
def test_top_k_rejects_malformed_records_container(records: object) -> None:
    with pytest.raises(TypeError):
        cc.top_k(pack_le(E0), records, 1)


@pytest.mark.parametrize(
    "entry",
    [
        ("q1-record-evidence-000001",),
        ("q1-record-evidence-000001", pack_le(E0), "extra"),
        "q1-record-evidence-000001",
        123,
    ],
)
def test_top_k_rejects_malformed_record_shape(entry: object) -> None:
    with pytest.raises(cc.CrossCheckError):
        cc.top_k(pack_le(E0), [entry], 1)


# --- import boundary -------------------------------------------------


def test_crosscheck_imports_only_standard_library() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(MODULE_PATH))
    module_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            module_names.append(node.module)
    assert module_names, "expected at least one import to check"
    top_level = {name.split(".")[0] for name in module_names}
    forbidden_markers = (
        "omnivia",
        "canonical",
        "generate",
        "policy",
        "numpy",
        "engine",
        "adapter",
        "storage",
        "database",
        "sqlite",
    )
    for name in top_level:
        lowered = name.lower()
        assert not any(marker in lowered for marker in forbidden_markers), name
        assert name == "__future__" or name in sys.stdlib_module_names, name


def test_crosscheck_module_has_no_relative_or_package_imports() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(MODULE_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "crosscheck.py must not use relative imports"
