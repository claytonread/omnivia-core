"""ADR-040 OVC1 framing for Core Runtime.

Runtime owns this implementation independently of Client.  It uses the public
Core RFC 8785 canonical-JSON contract, then applies the OVC1 transport closure:
bytes are returned by the encoder only when Runtime's own decoder admission
accepts them unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1 import ContractSemanticError
from omnivia_core.contracts.v1.canonical_json import (
    canonical_bytes,
    parse_json_document,
)

MAGIC: Final = b"OVC1"
LENGTH_BYTES: Final = 4
HEADER_BYTES: Final = 8
MAXIMUM_JSON_BYTES: Final = 4 * 1024 * 1024


class OVC1Error(Exception):
    """A frame was structurally invalid or outside OVC1 admission."""


_JSON_KINDS: Final[Mapping[type, str]] = {
    bool: "a JSON boolean",
    dict: "a JSON object",
    float: "a JSON number",
    int: "a JSON number",
    list: "a JSON array",
    str: "a JSON string",
}


def _kind(value: object) -> str:
    if value is None:
        return "JSON null"
    return _JSON_KINDS.get(type(value), "a non-JSON value")


def _canonicalize(value: object) -> tuple[bytes, str]:
    try:
        return canonical_bytes(value), ""
    except ContractSemanticError:
        return b"", "frame payload is outside the OVC1 canonical JSON domain"
    except RecursionError:
        return b"", "frame payload nests too deeply to canonicalize"
    except (TypeError, ValueError):
        return b"", "frame payload contains a value the canonical encoder cannot render"


def _parse(text: str) -> tuple[object, str]:
    try:
        return parse_json_document(text), ""
    except ContractSemanticError as error:
        cause = error.__cause__
        if isinstance(cause, json.JSONDecodeError):
            return None, f"frame JSON is not valid JSON at character offset {cause.pos}"
        if isinstance(cause, RecursionError):
            return None, "frame JSON nests too deeply to parse"
        if isinstance(cause, ValueError):
            return None, "frame JSON contains a number the parser cannot convert"
        return None, "frame JSON is not an admissible OVC1 document"
    except RecursionError:
        return None, "frame JSON nests too deeply to parse"
    except (TypeError, ValueError):
        return None, "frame JSON is not a document Runtime can parse"


def _admit_body(body: bytes) -> tuple[dict[str, Any], str]:
    try:
        text = body.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        return {}, f"frame JSON is not valid UTF-8 at byte offset {error.start}"

    value, reason = _parse(text)
    if reason:
        return {}, reason
    if not isinstance(value, dict):
        return {}, f"frame payload must be a JSON object, got {_kind(value)}"

    canonical, reason = _canonicalize(value)
    if reason:
        return {}, reason
    if canonical != body:
        return {}, "frame JSON is not in canonical form"
    return value, ""


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return one admitted canonical body, enforcing encoder closure."""
    if not isinstance(payload, Mapping):
        raise OVC1Error(
            f"frame payload must be a JSON object, got {type(payload).__name__}"
        )
    body, reason = _canonicalize(payload)
    if reason:
        raise OVC1Error(reason)
    _, reason = _admit_body(body)
    if reason:
        raise OVC1Error(
            "frame payload canonicalizes to bytes the OVC1 decoder does not admit; "
            "an encoder may only produce frames that decode"
        )
    return body


def encode_frame(payload: Mapping[str, Any]) -> bytes:
    """Encode one complete OVC1 frame with no delimiter."""
    body = canonical_json_bytes(payload)
    length = len(body)
    if length > MAXIMUM_JSON_BYTES:
        raise OVC1Error(
            f"frame JSON is {length} bytes, above the {MAXIMUM_JSON_BYTES}-byte maximum"
        )
    return MAGIC + length.to_bytes(LENGTH_BYTES, "big") + body


def decode_frame(frame: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Decode exactly one complete OVC1 frame and refuse trailing bytes."""
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise OVC1Error(f"frame must be bytes, got {type(frame).__name__}")
    raw = bytes(frame)
    if len(raw) < HEADER_BYTES:
        raise OVC1Error(
            f"frame is {len(raw)} bytes, too short for the {HEADER_BYTES}-byte OVC1 header"
        )
    if raw[: len(MAGIC)] != MAGIC:
        raise OVC1Error("frame does not begin with the OVC1 magic")

    length = int.from_bytes(raw[len(MAGIC) : HEADER_BYTES], "big")
    if length == 0:
        raise OVC1Error("frame declares a zero-byte JSON payload")
    if length > MAXIMUM_JSON_BYTES:
        raise OVC1Error(
            f"frame declares {length} JSON bytes, above the {MAXIMUM_JSON_BYTES}-byte maximum"
        )

    body = raw[HEADER_BYTES:]
    if len(body) < length:
        raise OVC1Error(
            f"frame is truncated: it declares {length} JSON bytes and carries {len(body)}"
        )
    if len(body) > length:
        raise OVC1Error(f"frame carries {len(body) - length} trailing byte(s)")

    value, reason = _admit_body(body)
    if reason:
        raise OVC1Error(reason)
    return value


__all__ = [
    "HEADER_BYTES",
    "LENGTH_BYTES",
    "MAGIC",
    "MAXIMUM_JSON_BYTES",
    "OVC1Error",
    "canonical_json_bytes",
    "decode_frame",
    "encode_frame",
]
