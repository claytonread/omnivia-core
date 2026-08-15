"""Standalone RFC 8785 helpers for the Q1 semantic-index campaign.

Q1 is deliberately independent of product packages, so this module implements
the required JSON Canonicalization Scheme with the Python standard library
only.  It admits a strict I-JSON subset: finite binary64 numbers, integers that
convert losslessly to binary64, scalar Unicode strings, string-keyed objects,
and duplicate-free parsed documents.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any, Final

JCS_MAX_NESTING_DEPTH: Final = 64

_SURROGATE_FIRST: Final = 0xD800
_SURROGATE_LAST: Final = 0xDFFF
_MAX_POSITIONAL_POINT: Final = 21
_MIN_POSITIONAL_POINT: Final = -6
_SHORT_ESCAPES: Final[Mapping[str, str]] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def utf16_sort_key(value: str) -> bytes:
    """Return the unsigned UTF-16 code-unit ordering key RFC 8785 requires."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise TypeError(f"utf16_sort_key requires str, got {type(value)!r}")
    try:
        return value.encode("utf-16-be", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"lone surrogate has no canonical JSON form: {value!r}"
        ) from exc


def _require_binary64(value: object, path: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{path}: boolean is not a JSON number")
    if isinstance(value, int):
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError(f"{path}: integer is not finite binary64") from exc
        if not isfinite(number) or int(number) != value:
            raise ValueError(f"{path}: integer does not convert losslessly to binary64")
        return number
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path}: non-finite number has no canonical JSON form")
        return value
    raise TypeError(f"{path}: expected a number, got {type(value)!r}")


def _shortest_decimal(number: float) -> tuple[str, int]:
    """Decompose a positive binary64 into shortest digits and decimal point."""
    text = repr(number)
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    integer_part, _, fraction_part = mantissa.partition(".")
    digits = integer_part + fraction_part
    scale = exponent - len(fraction_part)
    significant = digits.lstrip("0")
    trimmed = significant.rstrip("0")
    scale += len(significant) - len(trimmed)
    return trimmed, len(trimmed) + scale


def serialize_number(value: float, path: str = "$") -> str:
    """Render a number as ECMAScript ``Number::toString`` for RFC 8785."""
    number = _require_binary64(value, path)
    if number == 0.0:
        return "0"
    if number < 0.0:
        return "-" + serialize_number(-number, path)

    digits, point = _shortest_decimal(number)
    count = len(digits)
    if count <= point <= _MAX_POSITIONAL_POINT:
        return digits + "0" * (point - count)
    if 0 < point <= _MAX_POSITIONAL_POINT:
        return f"{digits[:point]}.{digits[point:]}"
    if _MIN_POSITIONAL_POINT < point <= 0:
        return f"0.{'0' * -point}{digits}"
    exponent = point - 1
    mantissa = digits if count == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{mantissa}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"


def _serialize_string(value: str, path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise TypeError(f"{path}: expected str, got {type(value)!r}")
    pieces = ['"']
    for character in value:
        code_point = ord(character)
        if _SURROGATE_FIRST <= code_point <= _SURROGATE_LAST:
            raise ValueError(f"{path}: lone surrogate has no canonical JSON form")
        escape = _SHORT_ESCAPES.get(character)
        if escape is not None:
            pieces.append(escape)
        elif code_point < 0x20:
            pieces.append(f"\\u{code_point:04x}")
        else:
            pieces.append(character)
    pieces.append('"')
    return "".join(pieces)


def _canonicalize_into(value: object, path: str, depth: int, out: list[str]) -> None:
    if depth > JCS_MAX_NESTING_DEPTH:
        raise ValueError(
            f"{path}: nesting exceeds maximum depth {JCS_MAX_NESTING_DEPTH}"
        )
    if value is None:
        out.append("null")
        return
    if isinstance(value, bool):
        out.append("true" if value else "false")
        return
    if isinstance(value, (int, float)):
        out.append(serialize_number(value, path))
        return
    if isinstance(value, str):
        out.append(_serialize_string(value, path))
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{path}: binary data is not a JSON value")
    if isinstance(value, Mapping):
        entries: list[tuple[bytes, str, object]] = []
        seen: set[str] = set()
        for key, item in value.items():
            if isinstance(key, bool) or not isinstance(key, str):
                raise TypeError(f"{path}: object member names must be str")
            if key in seen:
                raise ValueError(f"{path}: duplicate object member name {key!r}")
            seen.add(key)
            entries.append((utf16_sort_key(key), key, item))
        entries.sort(key=lambda entry: entry[0])
        out.append("{")
        for index, (_, key, item) in enumerate(entries):
            if index:
                out.append(",")
            member_path = f"{path}.{key}"
            out.append(_serialize_string(key, member_path))
            out.append(":")
            _canonicalize_into(item, member_path, depth + 1, out)
        out.append("}")
        return
    if isinstance(value, Sequence):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _canonicalize_into(item, f"{path}[{index}]", depth + 1, out)
        out.append("]")
        return
    raise ValueError(f"{path}: {type(value).__name__} is not a JSON value")


def canonicalize(value: object, path: str = "$") -> str:
    """Return RFC 8785 canonical JSON text for an admitted I-JSON value."""
    out: list[str] = []
    _canonicalize_into(value, path, 0, out)
    return "".join(out)


def canonical_json_bytes(obj: object) -> bytes:
    """Return the RFC 8785 canonical UTF-8 bytes hashed by Q1 identities."""
    return canonicalize(obj).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Stable SHA-256 hex digest of raw bytes."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"sha256_hex requires bytes, got {type(data)!r}")
    return hashlib.sha256(data).hexdigest()


def canonical_id(obj: object) -> str:
    """SHA-256 hex digest of an object's RFC 8785 canonical bytes."""
    return sha256_hex(canonical_json_bytes(obj))


def canonical_sha256_ref(obj: object) -> str:
    """Canonical ``sha256:<64 lowercase hex>`` reference for an object."""
    return f"sha256:{canonical_id(obj)}"


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(name: str) -> object:
    raise ValueError(f"{name!r} is not a JSON value")


def parse_json_document(document: str | bytes) -> object:
    """Strictly parse UTF-8 JSON, rejecting duplicate members and constants."""
    if isinstance(document, bytes):
        try:
            text = document.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ValueError("document is not valid UTF-8") from exc
    elif isinstance(document, str):
        text = document
    else:
        raise TypeError(f"document must be str or bytes, got {type(document)!r}")
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_members,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"document is not valid JSON: {exc}") from exc


def float32_le_bytes(values: Sequence[float]) -> bytes:
    """Pack values as explicit little-endian float32 with strict admission."""
    import math
    import struct

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(
            f"float32_le_bytes requires a sequence of floats, got {type(values)!r}"
        )
    packed: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"float32_le_bytes requires numeric values, got {value!r}")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"float32_le_bytes rejects non-finite value: {numeric!r}")
        if numeric == 0.0 and math.copysign(1.0, numeric) < 0:
            raise ValueError(f"float32_le_bytes rejects negative zero: {numeric!r}")
        packed.append(numeric)
    try:
        return struct.pack(f"<{len(packed)}f", *packed)
    except OverflowError as exc:
        raise ValueError(
            f"float32_le_bytes value overflows float32 range: {exc}"
        ) from exc


def float32_le_round_trip(values: Sequence[float]) -> list[float]:
    """Round-trip values through little-endian float32 for exact held bytes."""
    import struct

    data = float32_le_bytes(values)
    return list(struct.unpack(f"<{len(values)}f", data))
