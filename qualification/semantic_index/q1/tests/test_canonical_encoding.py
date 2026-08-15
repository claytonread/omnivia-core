"""Tests for canonical.py, imported by file path with no package markers."""

from __future__ import annotations

import importlib.util
import math
import re
import struct
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "canonical.py"
_SPEC = importlib.util.spec_from_file_location("q1_canonical_under_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
canonical: Any = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(canonical)

_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def test_canonical_json_bytes_sorted_keys_and_separators() -> None:
    data = canonical.canonical_json_bytes({"b": 1, "a": 2})
    assert data == b'{"a":2,"b":1}'


def test_canonical_json_bytes_deterministic_utf8_non_ascii() -> None:
    data = canonical.canonical_json_bytes({"café": "über", "日": "本"})
    text = data.decode("utf-8")
    assert "café" in text
    assert "ü" in text
    assert "日" in text
    # Encoded once, decoded back byte-identically.
    assert data == text.encode("utf-8")


def test_canonical_json_uses_rfc_8785_utf16_member_order() -> None:
    value = {"דּ": "hebrew", "😀": "emoji"}
    assert canonical.canonical_json_bytes(value) == (
        '{"😀":"emoji","דּ":"hebrew"}'.encode()
    )


def test_canonical_json_bytes_stable_separators_no_whitespace() -> None:
    data = canonical.canonical_json_bytes([1, 2, {"x": 3}])
    assert b" " not in data


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_bytes_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError):
        canonical.canonical_json_bytes({"v": bad})


def test_canonical_json_bytes_collapses_negative_zero_per_rfc_8785() -> None:
    assert canonical.canonical_json_bytes({"v": -0.0}) == b'{"v":0}'


def test_canonical_json_bytes_accepts_positive_zero() -> None:
    data = canonical.canonical_json_bytes({"v": 0.0})
    assert data == b'{"v":0}'


@pytest.mark.parametrize(
    "bit_pattern,expected",
    [
        ("0000000000000001", "5e-324"),
        ("7fefffffffffffff", "1.7976931348623157e+308"),
        ("4340000000000000", "9007199254740992"),
        ("4430000000000000", "295147905179352830000"),
        ("444b1ae4d6e2ef50", "1e+21"),
        ("3eb0c6f7a0b5ed8d", "0.000001"),
        ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ],
)
def test_rfc_8785_number_serialization_vectors(bit_pattern: str, expected: str) -> None:
    value = struct.unpack(">d", bytes.fromhex(bit_pattern))[0]
    assert canonical.serialize_number(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(1.0, "1"), (1e-7, "1e-7"), (1e20, "100000000000000000000")],
)
def test_number_serialization_is_ecmascript_not_python(
    value: float, expected: str
) -> None:
    assert canonical.serialize_number(value) == expected


def test_canonical_json_bytes_rejects_non_string_object_keys() -> None:
    with pytest.raises(TypeError):
        canonical.canonical_json_bytes({1: "a"})


def test_canonical_json_bytes_rejects_unsupported_values() -> None:
    with pytest.raises(ValueError):
        canonical.canonical_json_bytes({"v": {1, 2, 3}})


def test_canonical_json_bytes_rejects_inexact_binary64_integer() -> None:
    with pytest.raises(ValueError, match="losslessly"):
        canonical.canonical_json_bytes({"v": 2**53 + 1})


def test_parse_json_document_rejects_duplicate_members_and_nonfinite() -> None:
    with pytest.raises(ValueError, match="duplicate JSON member"):
        canonical.parse_json_document('{"a":1,"a":2}')
    with pytest.raises(ValueError, match="not a JSON value"):
        canonical.parse_json_document('{"a":NaN}')


def test_parse_json_document_rejects_invalid_utf8() -> None:
    with pytest.raises(ValueError, match="valid UTF-8"):
        canonical.parse_json_document(b'{"a":"\xff"}')


def test_canonical_id_is_stable_sha256_hex() -> None:
    digest = canonical.canonical_id({"a": 1})
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == canonical.canonical_id({"a": 1})


def test_canonical_id_differs_by_content() -> None:
    assert canonical.canonical_id({"a": 1}) != canonical.canonical_id({"a": 2})


def test_canonical_sha256_ref_format() -> None:
    ref = canonical.canonical_sha256_ref({"a": 1})
    assert _SHA256_REF_RE.fullmatch(ref)
    assert ref == f"sha256:{canonical.canonical_id({'a': 1})}"


def test_sha256_hex_requires_bytes() -> None:
    with pytest.raises(TypeError):
        canonical.sha256_hex("not-bytes")


def test_float32_le_bytes_little_endian_round_trip() -> None:
    data = canonical.float32_le_bytes([1.0, -1.0, 0.5])
    assert len(data) == 12
    assert canonical.float32_le_round_trip([1.0, -1.0, 0.5]) == [1.0, -1.0, 0.5]


def test_float32_le_bytes_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        canonical.float32_le_bytes([math.nan])


def test_float32_le_bytes_rejects_non_numeric() -> None:
    with pytest.raises(TypeError):
        canonical.float32_le_bytes(["not-a-number"])


def test_float32_le_bytes_rejects_bool() -> None:
    with pytest.raises(TypeError):
        canonical.float32_le_bytes([True])


def test_float32_le_bytes_rejects_negative_zero() -> None:
    with pytest.raises(ValueError):
        canonical.float32_le_bytes([-0.0])


def test_float32_le_bytes_accepts_positive_zero() -> None:
    assert canonical.float32_le_round_trip([0.0]) == [0.0]


def test_float32_le_bytes_rejects_float32_overflow() -> None:
    with pytest.raises(ValueError):
        canonical.float32_le_bytes([1e40])


def test_float32_le_bytes_is_little_endian() -> None:
    data = canonical.float32_le_bytes([1.0])
    assert data == struct.pack("<f", 1.0)
    assert data != struct.pack(">f", 1.0)
