"""The frozen OVC1 frame, held to its bytes.

Three parts. The first exercises the format directly: the byte layout, the
inclusive size bound, and every way a frame can be malformed. The second pins
the canonical form to RFC 8785 -- the ordering, number and escaping rules a
second implementation must match, and the I-JSON domain outside which a value
has no canonical bytes at all. The third reads the checked-in vector manifest
and **recomputes** every canonical-JSON and frame hex string in it, because
stored hex that nothing recomputes only proves the file has not been edited --
it proves nothing about the encoder.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_client.errors import ClientError, ProtocolError
from omnivia_core_client.framing import (
    CANONICAL_JSON_ALGORITHM,
    FRAME_FORMAT,
    HEADER_BYTES,
    LENGTH_BYTES,
    MAGIC,
    MAGIC_HEX,
    MAXIMUM_JSON_BYTES,
    MAXIMUM_JSON_NESTING_DEPTH,
    canonical_json_bytes,
    decode_frame,
    encode_frame,
)

from omnivia_core.contracts.v1 import (
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProbeResult,
    codec,
)
from omnivia_core.contracts.v1.canonical_json import canonical_bytes

REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_FIXTURES_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "fixtures"
MANIFEST_PATH = Path(__file__).resolve().parent / "fixtures" / "ovc1-v1.json"

MANIFEST: dict[str, Any] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
VECTORS: list[dict[str, Any]] = MANIFEST["vectors"]
CANONICALIZATION_VECTORS: list[dict[str, Any]] = MANIFEST["canonicalization_vectors"]
REJECTED_VECTORS: list[dict[str, Any]] = MANIFEST["rejected_vectors"]
ENCODER_REJECTED_VECTORS: list[dict[str, Any]] = MANIFEST["encoder_rejected_vectors"]
ALL_ACCEPTED: list[dict[str, Any]] = VECTORS + CANONICALIZATION_VECTORS

#: Every application-traffic vector the frozen format must state, by id.
REQUIRED_VECTOR_IDS = (
    "application.request",
    "application.success",
    "application.error",
    "probe.health.request",
    "probe.health.result",
    "probe.readiness.request",
    "probe.readiness.result",
    "probe.discover.request",
    "probe.discover.result-with-descriptor",
)

#: The canonicalization edge cases the format is pinned on. Each one is a place
#: where a plausible implementation disagrees with RFC 8785, so a manifest that
#: dropped any of them would stop discriminating.
REQUIRED_CANONICALIZATION_VECTOR_IDS = (
    "order.utf16-code-unit",
    "string.escaping",
    "number.negative-zero",
    "number.integral-float",
    "number.positional-upper-bound",
    "number.exponential-lower-bound",
    "number.exact-integer-boundary",
    "number.exact-integer-above-safe-range",
    "nesting.at-maximum",
)

#: The rejections a second implementation must also make.
REQUIRED_REJECTED_VECTOR_IDS = (
    "utf8.invalid-continuation",
    "json.duplicate-member-name",
    "json.nan-literal",
    "number.inexact-integer-above-maximum",
    "number.inexact-integer-below-minimum",
    "number.inexact-large-integer",
    "canonical.member-order-by-code-point",
    "nesting.past-maximum",
)

#: The payloads an encoder must decline to send. The other half of the boundary
#: `number.exact-integer-above-safe-range` states: an implementation that read
#: the admission rule as a safe-integer range would pass one of the two and fail
#: the other, whichever way it got it wrong.
REQUIRED_ENCODER_REJECTED_VECTOR_IDS = ("encode.inexact-canonical-integer",)

#: Named by code point rather than written literally. U+FB33 also has a
#: two-code-point decomposed spelling that renders identically and sorts
#: somewhere else entirely, so a vector that picked it up by accident would
#: stop discriminating while still looking right.
EMOJI = chr(0x1F600)
HEBREW = chr(0xFB33)

#: A 5000-digit integer, built by arithmetic rather than from a string: the
#: interpreter refuses to convert a decimal string of more than 4300 digits, so
#: `int("9" * 5000)` would fail in the test rather than in the code under test.
FIVE_THOUSAND_DIGIT_INTEGER = 10**5000 - 1
FIVE_THOUSAND_DIGIT_LITERAL = b"9" * 5000

#: An exact integer *above* 2**53 whose canonical token is itself, so the
#: transport closure rule leaves it encodable. 2**54 converts to binary64 and
#: back exactly, and the ECMAScript rendering of that binary64 is the same 17
#: digits. The vector that keeps the repair honest: the admission rule is
#: exactness, not a safe-integer range, and closing encode over decode did not
#: quietly turn it into one.
EXACT_INTEGER_ABOVE_SAFE_RANGE = 2**54
EXACT_INTEGER_ABOVE_SAFE_RANGE_TOKEN = b"18014398509481984"

#: An exact integer whose canonical token is *not* itself. 2**60 is admitted by
#: Core's canonicalizer and renders as 1152921504606847000, which lands back on
#: 2**60 and is therefore not an admitted integer in its own right. The frame it
#: would produce cannot be decoded, so no frame is produced.
EXACT_INTEGER_WITH_INADMISSIBLE_TOKEN = 2**60
INADMISSIBLE_TOKEN = b"1152921504606847000"

#: Numbers at and around every boundary the closure rule can turn on: the exact
#: integer edge in both directions, magnitudes whose rendering gains trailing
#: zeros, the positional/exponential switch-over points, and the float cases
#: that have always been closed. Used as a property, not as a verdict -- the
#: test asserts what must hold for *whichever* of them encode.
NUMBERS_AT_THE_BOUNDARIES = (
    0,
    1,
    -1,
    2**53 - 1,
    2**53,
    -(2**53),
    EXACT_INTEGER_ABOVE_SAFE_RANGE,
    -EXACT_INTEGER_ABOVE_SAFE_RANGE,
    2**55,
    EXACT_INTEGER_WITH_INADMISSIBLE_TOKEN,
    2**64,
    10**21,
    10**22,
    -0.0,
    0.1,
    1.5,
    1e20,
    1e21,
    1e-7,
)


def _frame(body: bytes, *, magic: bytes = MAGIC, declared: int | None = None) -> bytes:
    """Assemble a frame by hand, so a test can state exactly one thing wrong."""
    length = len(body) if declared is None else declared
    return magic + length.to_bytes(LENGTH_BYTES, "big") + body


def _nested_objects(count: int) -> dict[str, Any]:
    """`count` nested objects with a number inside, so depth is exactly `count`."""
    value: Any = 1
    for _ in range(count):
        value = {"n": value}
    assert isinstance(value, dict)
    return value


# --------------------------------------------------------------------------
# The frozen constants
# --------------------------------------------------------------------------


def test_magic_is_the_four_ascii_bytes_ovc1() -> None:
    assert MAGIC == b"OVC1"
    assert MAGIC.hex() == MAGIC_HEX == "4f564331"


def test_header_is_four_magic_bytes_and_a_four_byte_length() -> None:
    assert LENGTH_BYTES == 4
    assert HEADER_BYTES == len(MAGIC) + LENGTH_BYTES == 8


def test_maximum_json_payload_is_four_mebibytes() -> None:
    assert MAXIMUM_JSON_BYTES == 4 * 1024 * 1024 == 4194304


def test_frame_format_identifier_is_frozen() -> None:
    assert FRAME_FORMAT == "omnivia.ovc1.v1"


def test_the_canonical_form_is_named_and_is_rfc_8785() -> None:
    assert CANONICAL_JSON_ALGORITHM == "rfc8785"


def test_the_nesting_bound_is_read_from_core_not_restated() -> None:
    """Restating it would let the two drift, and a frame would encode here and
    fail to canonicalize there."""
    from omnivia_core.contracts.v1.canonical_json import JCS_MAX_NESTING_DEPTH

    assert MAXIMUM_JSON_NESTING_DEPTH is JCS_MAX_NESTING_DEPTH


# --------------------------------------------------------------------------
# Byte layout
# --------------------------------------------------------------------------


def test_frame_layout_is_magic_then_big_endian_length_then_json() -> None:
    payload = {"beta": 2, "alpha": 1}
    frame = encode_frame(payload)
    body = b'{"alpha":1,"beta":2}'

    assert frame[: len(MAGIC)] == MAGIC
    assert int.from_bytes(frame[len(MAGIC) : HEADER_BYTES], "big") == len(body)
    assert frame[HEADER_BYTES:] == body
    assert len(frame) == HEADER_BYTES + len(body)


def test_length_field_is_big_endian_not_little() -> None:
    # 258 bytes of JSON is 0x0102: the two orderings differ, so this pins one.
    payload = {"a": "x" * (258 - 8)}
    frame = encode_frame(payload)
    assert frame[len(MAGIC) : HEADER_BYTES] == b"\x00\x00\x01\x02"


def test_encode_then_decode_round_trips() -> None:
    payload = {
        "nested": {"b": [1, 2, 3], "a": None},
        "flag": True,
        "count": 42,
        "text": "hello",
    }
    assert decode_frame(encode_frame(payload)) == payload


def test_canonical_json_sorts_keys_and_uses_compact_separators() -> None:
    assert canonical_json_bytes({"b": 1, "a": {"d": 2, "c": 3}}) == (
        b'{"a":{"c":3,"d":2},"b":1}'
    )


def test_canonical_json_has_no_trailing_newline() -> None:
    body = canonical_json_bytes({"a": 1})
    assert body == b'{"a":1}'
    assert not body.endswith(b"\n")


def test_canonical_json_carries_non_ascii_as_utf8_not_escapes() -> None:
    body = canonical_json_bytes({"k": "café — 😀"})
    assert "\\u" not in body.decode("utf-8")
    assert body == '{"k":"café — 😀"}'.encode()
    assert decode_frame(encode_frame({"k": "café — 😀"})) == {"k": "café — 😀"}


def test_empty_object_is_a_valid_frame() -> None:
    frame = encode_frame({})
    assert frame == MAGIC + (2).to_bytes(LENGTH_BYTES, "big") + b"{}"
    assert decode_frame(frame) == {}


# --------------------------------------------------------------------------
# The canonical form is Core's RFC 8785 implementation, and only that one
# --------------------------------------------------------------------------


def test_the_encoder_is_cores_canonicalizer_and_not_a_second_implementation() -> None:
    """The frame body is exactly what the public contract's canonicalizer emits.

    Stated as a test because the alternative -- a wire-specific canonical form
    that happens to agree on today's payloads -- is invisible until the day a
    payload contains a supplementary member name or a number outside the
    positional window, and then two peers hash the same value differently.
    """
    for payload in (
        {"b": 1, "a": 2},
        {HEBREW: 1, EMOJI: 2},
        {"n": [1.0, -0.0, 1e20, 1e-7, 0.1]},
        {"s": "café\n\t"},
        {},
    ):
        assert canonical_json_bytes(payload) == canonical_bytes(payload)


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        (0, b"0"),
        (-0.0, b"0"),
        (0.0, b"0"),
        (1, b"1"),
        (1.0, b"1"),
        (1.5, b"1.5"),
        (0.1, b"0.1"),
        (1e-6, b"0.000001"),
        (1e-7, b"1e-7"),
        (1e20, b"100000000000000000000"),
        (1e21, b"1e+21"),
        (-1e-7, b"-1e-7"),
        (5e-324, b"5e-324"),
        (1.7976931348623157e308, b"1.7976931348623157e+308"),
        (9007199254740992, b"9007199254740992"),
        (-9007199254740992, b"-9007199254740992"),
    ],
)
def test_numbers_are_rendered_by_the_ecmascript_rule(
    value: float, canonical: bytes
) -> None:
    """The number spellings a `repr`-based or printf-based encoder gets wrong.

    Python writes `1.0`, `1e-07` and `1e+20` where ECMAScript -- and therefore
    RFC 8785, which defers to it -- writes `1`, `1e-7` and the positional
    `100000000000000000000`.
    """
    assert canonical_json_bytes({"v": value}) == b'{"v":' + canonical + b"}"


def test_members_are_ordered_by_utf16_code_unit_not_by_code_point() -> None:
    """The one ordering rule a sorted-keys encoder gets wrong.

    U+1F600 is above U+FB33 by code point, and below it by UTF-16 code unit,
    because its UTF-16 form starts with the high surrogate 0xD83D. Both names
    are written as code points rather than as literal characters: one of them
    also has a decomposed spelling that is a different string entirely, and a
    vector that silently used it would stop discriminating.
    """
    payload = {HEBREW: 3, EMOJI: 2, "a": 1}
    assert canonical_json_bytes(payload) == (
        b'{"a":1,"\xf0\x9f\x98\x80":2,"\xef\xac\xb3":3}'
    )
    # What sorting by code point would have produced, and why it is not enough.
    by_code_point = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    assert by_code_point.encode() != canonical_json_bytes(payload)


def test_escaping_is_the_json_stringify_rule() -> None:
    """Seven short escapes, lowercase `\\u00xx` for the rest of C0, DEL literal."""
    value = "".join(
        (chr(0x00), chr(0x01), chr(0x1F), "\b\f\n\r\t", '"', "\\", chr(0x7F), "é")
    )
    assert canonical_json_bytes({"s": value}) == (
        b'{"s":"\\u0000\\u0001\\u001f\\b\\f\\n\\r\\t\\"\\\\\x7f\xc3\xa9"}'
    )


def test_the_canonical_form_admits_exactly_one_spelling_per_number() -> None:
    """`1` and `1.0` are one JSON number here, not two values.

    Worth stating for a second implementation, and worth stating as a change:
    the canonical form is over the JSON value domain, where a number is a
    binary64 and an integer written without a fraction is the same number as
    one written with `.0`. A decoder therefore accepts exactly one of the two
    spellings on the wire.
    """
    assert (
        canonical_json_bytes({"a": 1}) == canonical_json_bytes({"a": 1.0}) == b'{"a":1}'
    )
    assert decode_frame(_frame(b'{"a":1}')) == {"a": 1}
    with pytest.raises(ProtocolError, match="not in canonical form"):
        decode_frame(_frame(b'{"a":1.0}'))


# --------------------------------------------------------------------------
# The admitted I-JSON value domain
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        pytest.param({1: "x"}, "an integer member name", id="int-key"),
        pytest.param({True: "x"}, "a boolean member name", id="bool-key"),
        pytest.param({None: "x"}, "a null member name", id="none-key"),
        pytest.param({1.5: "x"}, "a float member name", id="float-key"),
        pytest.param({("a",): "x"}, "a tuple member name", id="tuple-key"),
        pytest.param({"a": {2: "x"}}, "a nested integer member name", id="nested-key"),
        pytest.param(
            {"a": [{"b": {3: "x"}}]},
            "an integer member name under an array",
            id="deep-key",
        ),
    ],
)
def test_a_member_name_that_is_not_a_string_is_refused_and_never_coerced(
    payload: dict[Any, Any], why: str
) -> None:
    """The regression this exists for: `json.dumps` renames `{1: "x"}` to `{"1": "x"}`.

    Silently renaming a member is worse than refusing it. The sender wrote one
    document and the wire would carry another, and no later check could tell,
    because the coerced form is perfectly valid.
    """
    with pytest.raises(ProtocolError, match="outside the OVC1 canonical JSON domain"):
        canonical_json_bytes(payload)
    with pytest.raises(ProtocolError):
        encode_frame(payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"a": float("nan")}, id="nan"),
        pytest.param({"a": float("inf")}, id="infinity"),
        pytest.param({"a": float("-inf")}, id="negative-infinity"),
        pytest.param({"a": 2**53 + 1}, id="inexact-integer"),
        pytest.param({"a": -(2**53) - 1}, id="inexact-negative-integer"),
        pytest.param({"a": 2**60 + 1}, id="inexact-large-integer"),
        pytest.param({"a": "\ud800"}, id="lone-surrogate"),
        pytest.param({"\ud800": 1}, id="lone-surrogate-member-name"),
        pytest.param({"a": {1, 2}}, id="set"),
        pytest.param({"a": b"bytes"}, id="bytes"),
        pytest.param({"a": object()}, id="arbitrary-object"),
        pytest.param({"a": [1, {2: 3}]}, id="nested-non-string-key"),
    ],
)
def test_a_value_outside_the_ijson_domain_has_no_canonical_form(
    payload: dict[Any, Any],
) -> None:
    with pytest.raises(ProtocolError, match="outside the OVC1 canonical JSON domain"):
        canonical_json_bytes(payload)


@pytest.mark.parametrize(
    "value",
    [2**53, -(2**53), 2**53 - 1, -(2**53) + 1, 0, 1, -1],
)
def test_an_exactly_representable_integer_is_admitted(value: int) -> None:
    assert decode_frame(encode_frame({"a": value})) == {"a": value}


def test_a_five_thousand_digit_integer_is_refused_on_encode() -> None:
    """The interpreter will not render it, so Core's own diagnostic cannot be built.

    This is the failure that escapes the contract layer as a bare `ValueError`
    rather than as a `ContractSemanticError`: formatting the offending integer
    into a message needs the same conversion that is refused. It has to be
    caught here, or a caller sees an interpreter limit instead of a frame fault.
    """
    with pytest.raises(ProtocolError, match="cannot render at all") as caught:
        encode_frame({"a": FIVE_THOUSAND_DIGIT_INTEGER})
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_five_thousand_digit_integer_is_refused_on_decode() -> None:
    body = b'{"a":' + FIVE_THOUSAND_DIGIT_LITERAL + b"}"
    with pytest.raises(ProtocolError, match="number the parser will not convert"):
        decode_frame(_frame(body))


@pytest.mark.parametrize(
    "literal",
    [b"9007199254740993", b"-9007199254740993", b"1152921504606847000", b"1e400"],
)
def test_a_number_with_no_exact_binary64_value_is_refused_on_decode(
    literal: bytes,
) -> None:
    """The admission rule is exactness, not magnitude, and it runs on the wire too."""
    with pytest.raises(ProtocolError, match="outside the OVC1 canonical JSON domain"):
        decode_frame(_frame(b'{"a":' + literal + b"}"))


# --------------------------------------------------------------------------
# The transport closure rule: an encoder returns only frames that decode
# --------------------------------------------------------------------------


def test_an_integer_whose_canonical_token_is_not_admitted_is_refused_on_encode() -> (
    None
):
    """The regression this repair exists for, at the value that motivated it.

    2**60 is exactly representable, so Core's canonicalizer admits it and
    renders it by the ECMAScript rule as 1152921504606847000. That decimal is
    *not* itself an admitted integer -- it lands back on 2**60, a different
    integer -- so the bytes it produces are refused on arrival. Returning that
    frame would hand a caller something no conforming peer can accept, so no
    frame is returned: the failure happens at the sender, which can carry the
    identity as a string instead.
    """
    with pytest.raises(ProtocolError, match="an encoder may only produce frames") as (
        caught
    ):
        encode_frame({"a": EXACT_INTEGER_WITH_INADMISSIBLE_TOKEN})
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    with pytest.raises(ProtocolError, match="an encoder may only produce frames"):
        canonical_json_bytes({"a": EXACT_INTEGER_WITH_INADMISSIBLE_TOKEN})


def test_the_refusal_is_the_transports_and_not_a_change_to_cores_canonicalizer() -> (
    None
):
    """ADR-039 is frozen, and this repair does not reach into it.

    Core still admits 2**60 and still renders it as 1152921504606847000, which
    is what content-addressing a document requires and what any second
    implementation of the canonical form must keep doing. What OVC1 adds is a
    rule about *frames*: the wire carries only bytes the wire can carry back.
    Asserted together, because a repair that had narrowed the canonicalizer
    would pass every other test in this file.
    """
    assert canonical_bytes({"a": EXACT_INTEGER_WITH_INADMISSIBLE_TOKEN}) == (
        b'{"a":' + INADMISSIBLE_TOKEN + b"}"
    )
    with pytest.raises(ProtocolError):
        canonical_json_bytes({"a": EXACT_INTEGER_WITH_INADMISSIBLE_TOKEN})


def test_an_exact_integer_above_two_to_the_fifty_third_still_frames() -> None:
    """The closure rule is not a safe-integer range, and this is the proof.

    2**54 is above 2**53 and stays encodable, because its canonical token is
    the same 17 digits it started as and those digits are themselves exact.
    Refusing it would be a different, narrower rule than the one Core freezes,
    and would silently drop values that have always been carriable.
    """
    frame = encode_frame({"a": EXACT_INTEGER_ABOVE_SAFE_RANGE})
    assert (
        frame[HEADER_BYTES:] == b'{"a":' + EXACT_INTEGER_ABOVE_SAFE_RANGE_TOKEN + b"}"
    )
    assert decode_frame(frame) == {"a": EXACT_INTEGER_ABOVE_SAFE_RANGE}
    assert decode_frame(encode_frame({"a": -EXACT_INTEGER_ABOVE_SAFE_RANGE})) == {
        "a": -EXACT_INTEGER_ABOVE_SAFE_RANGE
    }


@pytest.mark.parametrize("value", NUMBERS_AT_THE_BOUNDARIES, ids=repr)
def test_every_frame_the_encoder_returns_decodes(value: float) -> None:
    """The closure rule stated as the property it is, over every boundary number.

    There are exactly two outcomes and no third: either encoding refuses, or it
    returns a frame that decodes and re-encodes to the very same bytes. Written
    as an implication rather than as a per-value verdict on purpose -- which
    numbers fall on which side is Core's frozen admission rule to decide, and a
    test that restated the list would be asserting a copy of it.
    """
    try:
        frame = encode_frame({"a": value})
    except ProtocolError:
        return

    decoded = decode_frame(frame)
    assert decoded == {"a": value}
    assert encode_frame(decoded) == frame


def test_a_frame_that_arrives_carrying_an_unadmitted_token_is_still_refused() -> None:
    """Closing the encoder does not let the decoder relax.

    Nothing stops a non-conforming peer from putting 1152921504606847000 on the
    wire; this implementation simply never will. The received-bytes rejection is
    the one the vector manifest pins, and it stays exactly as it was.
    """
    with pytest.raises(ProtocolError, match="outside the OVC1 canonical JSON domain"):
        decode_frame(_frame(b'{"a":' + INADMISSIBLE_TOKEN + b"}"))


# --------------------------------------------------------------------------
# Nesting
# --------------------------------------------------------------------------


def test_nesting_at_the_maximum_is_accepted() -> None:
    payload = _nested_objects(MAXIMUM_JSON_NESTING_DEPTH)
    assert decode_frame(encode_frame(payload)) == payload


def test_one_level_past_the_maximum_has_no_canonical_form() -> None:
    with pytest.raises(ProtocolError, match="outside the OVC1 canonical JSON domain"):
        encode_frame(_nested_objects(MAXIMUM_JSON_NESTING_DEPTH + 1))


def test_a_received_frame_past_the_maximum_is_refused_after_it_parses() -> None:
    """Valid JSON the parser admits and the canonical form does not."""
    body = json.dumps(_nested_objects(MAXIMUM_JSON_NESTING_DEPTH + 1)).replace(" ", "")
    with pytest.raises(ProtocolError, match="outside the OVC1 canonical JSON domain"):
        decode_frame(_frame(body.encode()))


def test_deeply_nested_json_is_refused_before_it_takes_the_parser_down() -> None:
    depth = 5000
    body = b'{"a":' + b"[" * depth + b"]" * depth + b"}"
    with pytest.raises(ProtocolError, match="nests too deeply to parse"):
        decode_frame(_frame(body))


# --------------------------------------------------------------------------
# Size bounds -- the maximum is inclusive
# --------------------------------------------------------------------------


def test_a_payload_of_exactly_the_maximum_is_accepted() -> None:
    # `{"a":"<pad>"}` canonicalizes to 8 bytes of structure plus the padding.
    payload = {"a": "x" * (MAXIMUM_JSON_BYTES - 8)}
    body = canonical_json_bytes(payload)
    assert len(body) == MAXIMUM_JSON_BYTES

    frame = encode_frame(payload)
    assert len(frame) == HEADER_BYTES + MAXIMUM_JSON_BYTES
    assert decode_frame(frame) == payload


def test_a_payload_one_byte_over_the_maximum_is_refused_on_encode() -> None:
    payload = {"a": "x" * (MAXIMUM_JSON_BYTES - 7)}
    assert len(canonical_json_bytes(payload)) == MAXIMUM_JSON_BYTES + 1
    with pytest.raises(ProtocolError, match="above the"):
        encode_frame(payload)


def test_a_declared_length_over_the_maximum_is_refused_before_reading_a_body() -> None:
    # The declared length is refused on its own, so a peer cannot make a reader
    # allocate for a body it never has to send.
    header = MAGIC + (MAXIMUM_JSON_BYTES + 1).to_bytes(LENGTH_BYTES, "big")
    with pytest.raises(ProtocolError, match="above the"):
        decode_frame(header)


def test_a_declared_length_of_zero_is_refused() -> None:
    with pytest.raises(ProtocolError, match="zero-byte"):
        decode_frame(MAGIC + (0).to_bytes(LENGTH_BYTES, "big"))


# --------------------------------------------------------------------------
# Malformed frames
# --------------------------------------------------------------------------


def test_wrong_magic_is_refused() -> None:
    with pytest.raises(ProtocolError, match="magic"):
        decode_frame(_frame(b'{"a":1}', magic=b"XVC1"))


def test_lowercase_magic_is_refused() -> None:
    with pytest.raises(ProtocolError, match="magic"):
        decode_frame(_frame(b'{"a":1}', magic=b"ovc1"))


@pytest.mark.parametrize("size", [0, 1, 7])
def test_a_frame_shorter_than_the_header_is_refused(size: int) -> None:
    with pytest.raises(ProtocolError, match="too short"):
        decode_frame(b"\x00" * size)


def test_a_truncated_body_is_refused() -> None:
    frame = encode_frame({"a": 1})
    with pytest.raises(ProtocolError, match="truncated"):
        decode_frame(frame[:-1])


def test_trailing_bytes_after_the_declared_length_are_refused() -> None:
    frame = encode_frame({"a": 1})
    with pytest.raises(ProtocolError, match="after its declared"):
        decode_frame(frame + b"\n")


def test_a_second_whole_frame_appended_is_refused_rather_than_split() -> None:
    frame = encode_frame({"a": 1})
    with pytest.raises(ProtocolError, match="after its declared"):
        decode_frame(frame + frame)


def test_invalid_utf8_is_refused() -> None:
    with pytest.raises(ProtocolError, match="not valid UTF-8"):
        decode_frame(_frame(b'{"a":"\xff\xfe"}'))


def test_invalid_json_is_refused() -> None:
    with pytest.raises(ProtocolError, match="not valid JSON"):
        decode_frame(_frame(b'{"a":}'))


def test_a_duplicated_member_name_is_refused_at_parse() -> None:
    """The one fault only the parser can see: a mapping has already lost it."""
    with pytest.raises(ProtocolError, match="not an admissible OVC1 document"):
        decode_frame(_frame(b'{"a":1,"a":2}'))


def test_a_duplicated_member_name_nested_in_the_document_is_refused_too() -> None:
    with pytest.raises(ProtocolError, match="not an admissible OVC1 document"):
        decode_frame(_frame(b'{"a":{"b":1,"b":2}}'))


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (b"123", "a JSON number"),
        (b'"text"', "a JSON string"),
        (b"true", "a JSON boolean"),
        (b"null", "JSON null"),
        (b"[1,2]", "a JSON array"),
    ],
)
def test_a_root_that_is_not_an_object_is_refused(body: bytes, kind: str) -> None:
    with pytest.raises(ProtocolError, match="must be a JSON object") as caught:
        decode_frame(_frame(body))
    assert kind in str(caught.value)


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
def test_the_non_standard_number_literals_are_refused(literal: bytes) -> None:
    with pytest.raises(ProtocolError, match="not an admissible OVC1 document"):
        decode_frame(_frame(b'{"a":' + literal + b"}"))


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b'{"b":1,"a":2}', "keys not sorted"),
        (b'{"a":1,"\xef\xac\xb3":3,"\xf0\x9f\x98\x80":2}', "sorted by code point"),
        (b'{"a": 1}', "space after the name separator"),
        (b'{"a":1, "b":2}', "space after the value separator"),
        (b'{"a":1}\n', "trailing newline inside the declared length"),
        (b'{"a":1e2}', "a number spelled in exponent form"),
        (b'{"a":1.50}', "a fraction carrying a trailing zero"),
        (b'{"a":1.0}', "an integral number spelled with a fraction"),
        (b'{"a":-0.0}', "a signed zero"),
        (b'{"a":"\\u0063"}', "an escape where the literal character is canonical"),
        (b'{"a":"\\u00e9"}', "an escaped non-ASCII character"),
        (b'  {"a":1}', "leading whitespace"),
    ],
)
def test_non_canonical_json_is_refused(body: bytes, why: str) -> None:
    """Valid JSON, and still refused: OVC1 admits exactly one spelling per value."""
    assert json.loads(body.decode("utf-8")) is not None, why
    with pytest.raises(ProtocolError, match="not in canonical form"):
        decode_frame(_frame(body))


@pytest.mark.parametrize("payload", ["text", 1, [1, 2], None, True])
def test_encoding_anything_but_a_json_object_is_refused(payload: object) -> None:
    with pytest.raises(ProtocolError, match="must be a JSON object"):
        encode_frame(payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("frame", ["not bytes", 1, None, ["OVC1"]])
def test_decoding_something_that_is_not_bytes_is_refused(frame: object) -> None:
    with pytest.raises(ProtocolError, match="must be bytes"):
        decode_frame(frame)  # type: ignore[arg-type]


def test_bytearray_and_memoryview_frames_decode() -> None:
    frame = encode_frame({"a": 1})
    assert decode_frame(bytearray(frame)) == {"a": 1}
    assert decode_frame(memoryview(frame)) == {"a": 1}


def test_every_framing_failure_is_a_client_error() -> None:
    assert issubclass(ProtocolError, ClientError)


# --------------------------------------------------------------------------
# Diagnostics never carry payload content
# --------------------------------------------------------------------------

SECRET = "sk-live-51H9xQqZzTOPSECRETvalue"


def _rendered(error: BaseException) -> str:
    """Everything about an exception a log, a bug report or a crash dump would show."""
    return "".join(
        (
            str(error),
            repr(error),
            repr(error.args),
            "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
        )
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(SECRET.encode() + b'{"a":1}', id="not-json"),
        pytest.param(b'"' + SECRET.encode() + b'"', id="scalar-root"),
        pytest.param(b'{"b":1,"a":"' + SECRET.encode() + b'"}', id="non-canonical"),
        pytest.param(
            b'{"a":"' + SECRET.encode().replace(b"k", b"\xff") + b'"}', id="bad-utf8"
        ),
        pytest.param(b'{"a":' + SECRET.encode() + b"}", id="invalid-value"),
        pytest.param(
            b'{"' + SECRET.encode() + b'":1,"' + SECRET.encode() + b'":2}',
            id="duplicate",
        ),
        pytest.param(b'{"' + SECRET.encode() + b'":NaN}', id="nan-literal"),
        pytest.param(
            b'{"' + SECRET.encode() + b'":9007199254740993}', id="inexact-integer"
        ),
        pytest.param(
            b'{"' + SECRET.encode() + b'":' + FIVE_THOUSAND_DIGIT_LITERAL + b"}",
            id="huge-integer",
        ),
        pytest.param(
            json.dumps({SECRET: _nested_objects(MAXIMUM_JSON_NESTING_DEPTH + 1)})
            .replace(" ", "")
            .encode(),
            id="too-deep",
        ),
        pytest.param(
            (b'{"a":' + b"[" * 5000 + b"]" * 5000 + b',"' + SECRET.encode() + b'":1}'),
            id="past-parser-depth",
        ),
    ],
)
def test_a_rejected_frame_never_puts_its_payload_in_the_exception(body: bytes) -> None:
    """A frame carries credentials; an exception gets logged. The two must not meet.

    Checked through the whole exception *chain*, not just the raised error: the
    parser and the canonicalizer both name the offending value in their own
    diagnostics on purpose, so a chained cause -- or a context left behind by
    raising inside an ``except`` block -- would leak the payload through a
    traceback nobody thought to redact. Both are asserted absent, rather than
    merely suppressed in the default rendering.
    """
    with pytest.raises(ProtocolError) as caught:
        decode_frame(_frame(body))

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = _rendered(error)
    assert SECRET not in rendered
    assert "TOPSECRET" not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({SECRET: {1: "x"}}, id="non-string-member-name"),
        pytest.param({SECRET: float("nan")}, id="non-finite-number"),
        pytest.param({SECRET: 2**53 + 1}, id="inexact-integer"),
        pytest.param(
            {SECRET: EXACT_INTEGER_WITH_INADMISSIBLE_TOKEN}, id="unframeable-integer"
        ),
        pytest.param({SECRET: FIVE_THOUSAND_DIGIT_INTEGER}, id="huge-integer"),
        pytest.param({SECRET: {1, 2}}, id="not-a-json-value"),
        pytest.param({SECRET: "\ud800"}, id="lone-surrogate"),
        pytest.param(
            {SECRET: _nested_objects(MAXIMUM_JSON_NESTING_DEPTH + 1)}, id="too-deep"
        ),
    ],
)
def test_a_rejected_payload_never_puts_its_content_in_the_exception(
    payload: dict[Any, Any],
) -> None:
    """The same rule on the way out. A payload being sent is the payload most
    likely to hold the credential, and encoding is where it is first inspected."""
    with pytest.raises(ProtocolError) as caught:
        encode_frame(payload)

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert SECRET not in _rendered(error)


def test_a_closure_refusal_names_neither_the_value_nor_what_it_rendered_to() -> None:
    """The closure diagnostic has a second thing to keep quiet about.

    A refusal on this path is the only one that has already computed the
    payload's canonical bytes, so it holds both the value the sender wrote and
    the decimal it rendered to. Neither belongs in an exception: the rendering
    is a faithful transcription of the value, and quoting it would leak the same
    fact by a different spelling.
    """
    with pytest.raises(ProtocolError) as caught:
        encode_frame(
            {"account": SECRET, "balance": EXACT_INTEGER_WITH_INADMISSIBLE_TOKEN}
        )

    rendered = _rendered(caught.value)
    assert SECRET not in rendered
    assert INADMISSIBLE_TOKEN.decode() not in rendered
    assert str(EXACT_INTEGER_WITH_INADMISSIBLE_TOKEN) not in rendered


def test_a_rejected_oversized_frame_reports_sizes_not_content() -> None:
    payload = {"a": SECRET, "b": "x" * MAXIMUM_JSON_BYTES}
    with pytest.raises(ProtocolError) as caught:
        encode_frame(payload)
    assert SECRET not in str(caught.value)
    assert str(MAXIMUM_JSON_BYTES) in str(caught.value)


def test_a_wrong_magic_diagnostic_does_not_echo_the_observed_bytes() -> None:
    """On a misaligned stream those four bytes are payload, not a header."""
    with pytest.raises(ProtocolError) as caught:
        decode_frame(_frame(b'{"a":1}', magic=b"sk-l"))
    assert "sk-l" not in str(caught.value)
    assert MAGIC_HEX in str(caught.value)


# --------------------------------------------------------------------------
# The checked-in vector manifest
# --------------------------------------------------------------------------


def test_manifest_states_the_frozen_format_facts() -> None:
    assert MANIFEST["format"] == FRAME_FORMAT == "omnivia.ovc1.v1"
    assert MANIFEST["magic_hex"] == MAGIC_HEX == "4f564331"
    assert MANIFEST["maximum_json_bytes"] == MAXIMUM_JSON_BYTES == 4194304
    assert MANIFEST["header_bytes"] == HEADER_BYTES
    assert MANIFEST["protocol_version"] == "1.0"


def test_manifest_names_the_canonicalization_rather_than_describing_it_loosely() -> (
    None
):
    """A second implementation reads this file, not this package's docstrings."""
    canonicalization = MANIFEST["canonicalization"]
    assert canonicalization["algorithm"] == CANONICAL_JSON_ALGORITHM == "rfc8785"
    assert canonicalization["encoding"] == "utf-8"
    assert canonicalization["maximum_nesting_depth"] == MAXIMUM_JSON_NESTING_DEPTH
    assert "8785" in canonicalization["specification"]
    assert "UTF-16" in canonicalization["member_order"]
    assert "code point" in canonicalization["member_order"]
    assert "ECMAScript" in canonicalization["number_format"]
    assert "I-JSON" in canonicalization["value_domain"]
    assert "9007199254740992" in canonicalization["integer_round_trip"]


def test_manifest_states_the_transport_closure_rule() -> None:
    """The rule a second implementation would otherwise have to infer from ours.

    Stated on the manifest rather than left to this package's docstrings,
    because the manifest is what another language reads. It has to say three
    things: that an encoder only returns bytes the decode path accepts, that
    the refusal happens at the sender, and that this is a transport rule which
    leaves the canonical form alone -- the last one being what stops a reader
    implementing it as a narrower canonicalizer.
    """
    canonicalization = MANIFEST["canonicalization"]
    closure = canonicalization["transport_closure"]
    assert "accepted as canonical by the OVC1 decode path" in closure
    assert "no frame" in closure or "returns a frame only when" in closure
    assert "changes nothing about the canonical form" in closure
    assert "encoder_rejected_vectors" in closure

    # Stated as the rule it is, not as the defect it replaced.
    assert "not total" not in canonicalization["integer_round_trip"]
    assert "18014398509481984" in canonicalization["integer_round_trip"]
    assert "not a safe-integer range" in canonicalization["integer_round_trip"]


def test_manifest_carries_exactly_the_required_traffic_vectors() -> None:
    assert tuple(vector["id"] for vector in VECTORS) == REQUIRED_VECTOR_IDS


def test_manifest_carries_the_required_canonicalization_edge_vectors() -> None:
    present = {vector["id"] for vector in CANONICALIZATION_VECTORS}
    assert set(REQUIRED_CANONICALIZATION_VECTOR_IDS) <= present


def test_manifest_carries_the_required_rejection_vectors() -> None:
    present = {vector["id"] for vector in REJECTED_VECTORS}
    assert set(REQUIRED_REJECTED_VECTOR_IDS) <= present


def test_manifest_carries_the_required_encoder_rejection_vectors() -> None:
    present = {vector["id"] for vector in ENCODER_REJECTED_VECTORS}
    assert set(REQUIRED_ENCODER_REJECTED_VECTOR_IDS) <= present


def test_every_vector_id_is_unique() -> None:
    ids = [
        vector["id"]
        for vector in ALL_ACCEPTED + REJECTED_VECTORS + ENCODER_REJECTED_VECTORS
    ]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("vector", ALL_ACCEPTED, ids=lambda vector: str(vector["id"]))
def test_vector_canonical_json_hex_is_recomputed_not_trusted(
    vector: dict[str, Any],
) -> None:
    body = canonical_json_bytes(vector["payload"])
    assert body.hex() == vector["canonical_json_hex"]
    assert len(body) == vector["canonical_json_bytes"]


@pytest.mark.parametrize("vector", ALL_ACCEPTED, ids=lambda vector: str(vector["id"]))
def test_vector_frame_hex_is_recomputed_not_trusted(vector: dict[str, Any]) -> None:
    frame = encode_frame(vector["payload"])
    assert frame.hex() == vector["frame_hex"]
    assert len(frame) == vector["frame_bytes"]
    assert frame[:HEADER_BYTES].hex() == (
        MAGIC_HEX + vector["canonical_json_bytes"].to_bytes(LENGTH_BYTES, "big").hex()
    )


@pytest.mark.parametrize("vector", ALL_ACCEPTED, ids=lambda vector: str(vector["id"]))
def test_vector_frame_decodes_back_to_its_payload(vector: dict[str, Any]) -> None:
    assert decode_frame(bytes.fromhex(vector["frame_hex"])) == vector["payload"]


@pytest.mark.parametrize(
    "vector", CANONICALIZATION_VECTORS, ids=lambda vector: str(vector["id"])
)
def test_canonicalization_vector_states_its_bytes_as_readable_text_too(
    vector: dict[str, Any],
) -> None:
    """`canonical_json` is what a person checks by eye; the hex is what a machine
    checks. They must be the same bytes, or the file misleads one of them."""
    assert (
        vector["canonical_json"].encode("utf-8").hex() == vector["canonical_json_hex"]
    )


@pytest.mark.parametrize(
    "vector", REJECTED_VECTORS, ids=lambda vector: str(vector["id"])
)
def test_every_rejected_vector_is_actually_rejected(vector: dict[str, Any]) -> None:
    document = bytes.fromhex(vector["json_hex"])
    assert vector["frame_hex"] == _frame(document).hex()
    assert vector["reason"] in MANIFEST["rejection_reasons"]
    if "json" in vector:
        assert vector["json"].encode("utf-8") == document

    with pytest.raises(ProtocolError) as caught:
        decode_frame(bytes.fromhex(vector["frame_hex"]))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_every_documented_rejection_reason_is_used_by_a_vector() -> None:
    """A reason nothing exercises is a claim about behaviour nothing checks."""
    used = {vector["reason"] for vector in REJECTED_VECTORS}
    assert used == set(MANIFEST["rejection_reasons"])


@pytest.mark.parametrize(
    "vector", ENCODER_REJECTED_VECTORS, ids=lambda vector: str(vector["id"])
)
def test_every_encoder_rejected_vector_is_actually_refused_on_encode(
    vector: dict[str, Any],
) -> None:
    """The closure rule, checked from the sending end.

    Three assertions, and the middle one carries the weight. The payload is
    refused and no frame comes back. Core's canonicalizer nonetheless produces
    exactly the bytes the vector records -- so the refusal is demonstrably the
    transport declining to send a canonical form, not a canonicalizer that
    stopped admitting the value. And where the vector names its decode-side
    twin, those are the same bytes, which is what makes the two arrays one fact
    rather than two that happen to agree.
    """
    payload = vector["payload"]
    assert vector["reason"] in MANIFEST["encoder_rejection_reasons"]

    with pytest.raises(ProtocolError) as caught:
        encode_frame(payload)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    with pytest.raises(ProtocolError):
        canonical_json_bytes(payload)

    assert canonical_bytes(payload).hex() == vector["canonical_json_hex"]
    assert (
        vector["canonical_json"].encode("utf-8").hex() == vector["canonical_json_hex"]
    )

    if "decode_rejection" in vector:
        twin = next(
            rejected
            for rejected in REJECTED_VECTORS
            if rejected["id"] == vector["decode_rejection"]
        )
        assert twin["json_hex"] == vector["canonical_json_hex"]


def test_every_documented_encoder_rejection_reason_is_used_by_a_vector() -> None:
    used = {vector["reason"] for vector in ENCODER_REJECTED_VECTORS}
    assert used == set(MANIFEST["encoder_rejection_reasons"])


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: str(vector["id"]))
def test_vector_payload_is_an_accepted_contract_document(
    vector: dict[str, Any],
) -> None:
    """Decoding through the public DTOs, and re-encoding to the same document.

    A byte vector for a document shape nobody accepts would pin the wrong
    bytes. This is what keeps the vectors honest about being real traffic.
    """
    payload = vector["payload"]
    kind = vector["type"]
    if kind == "RequestEnvelope":
        assert codec.encode_request(codec.decode_request(payload)) == payload
    elif kind == "ResponseEnvelope":
        assert codec.encode_response(codec.decode_response(payload)) == payload
    elif kind == "ServiceProbeRequest":
        assert ServiceProbeRequest.from_wire(payload).to_wire() == payload
    elif kind == "ServiceProbeResult":
        assert ServiceProbeResult.from_wire(payload).to_wire() == payload
    else:  # pragma: no cover - a new kind must be handled, not skipped
        raise AssertionError(f"unhandled vector type {kind!r}")


@pytest.mark.parametrize(
    "vector",
    [vector for vector in VECTORS if "reused_from" in vector],
    ids=lambda vector: str(vector["id"]),
)
def test_reused_vectors_still_equal_the_canonical_fixture(
    vector: dict[str, Any],
) -> None:
    """The application vectors are the canonical fixtures verbatim, and must stay so."""
    source = REPO_ROOT / vector["reused_from"]
    assert source.parent == CANONICAL_FIXTURES_DIR
    assert json.loads(source.read_text(encoding="utf-8")) == vector["payload"]


# --------------------------------------------------------------------------
# The discovery vector's descriptor
# --------------------------------------------------------------------------

DISCOVERY_DESCRIPTOR: dict[str, Any] = next(
    vector
    for vector in VECTORS
    if vector["id"] == "probe.discover.result-with-descriptor"
)["payload"]["descriptor"]

#: Substrings that would name a fact a coordination descriptor must never carry.
FORBIDDEN_FACT_MARKERS = (
    "authority",
    "capabilit",
    "credential",
    "database",
    "directory",
    "lease",
    "password",
    "path",
    "principal",
    "role",
    "secret",
    "token",
)


def _keys(value: object) -> list[str]:
    if isinstance(value, dict):
        return [
            key for name, item in value.items() for key in [str(name), *_keys(item)]
        ]
    if isinstance(value, list):
        return [key for item in value for key in _keys(item)]
    return []


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return [value] if isinstance(value, str) else []


def test_discovery_vector_carries_a_complete_accepted_descriptor() -> None:
    descriptor = ServiceEndpointDescriptor.from_wire(DISCOVERY_DESCRIPTOR)
    assert descriptor.to_wire() == DISCOVERY_DESCRIPTOR
    # Complete: the optional process evidence is present, and complete in turn.
    assert descriptor.process is not None
    assert descriptor.process.pid > 0
    assert descriptor.process.start_time
    assert descriptor.process.boot_id


def test_discovery_vector_descriptor_speaks_protocol_one_zero() -> None:
    assert DISCOVERY_DESCRIPTOR["protocol_version"] == "1.0"


def test_discovery_vector_names_no_authority_credential_or_lease_fact() -> None:
    offending = [
        key
        for key in _keys(DISCOVERY_DESCRIPTOR)
        if any(marker in key.lower() for marker in FORBIDDEN_FACT_MARKERS)
    ]
    assert offending == []


def test_discovery_vector_states_no_filesystem_or_database_location() -> None:
    """An address to dial, never a location to open."""
    uri = DISCOVERY_DESCRIPTOR["endpoint_uri"]
    scheme, separator, remainder = uri.partition("://")
    assert separator, uri
    assert scheme not in {"file", "sqlite"}
    # Authority only: no path component, so the URI names no file or directory.
    assert "/" not in remainder

    for text in _strings(DISCOVERY_DESCRIPTOR):
        if text == uri:
            continue
        assert not text.startswith("/"), text
        assert "\\" not in text, text
        assert "://" not in text, text
