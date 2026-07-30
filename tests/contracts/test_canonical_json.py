"""Tests for the RFC 8785 JSON Canonicalization Scheme implementation (ADR-039).

A Context Pack's identity is a hash of its own canonical bytes, so this module is the one
place where "provider-neutral" has to mean *byte-identical across implementations*, not
merely "same fields". These tests are written accordingly: the number vectors below are the
IEEE 754 bit patterns RFC 8785's own number-serialization table publishes, checked as bit
patterns rather than as decimal literals so a Python parsing difference cannot quietly
change which double is under test, and the property-ordering vector is the RFC's own
worked example -- the one whose expected output distinguishes UTF-16 code-unit ordering
from Unicode code-point ordering.

Nothing here shells out to a JavaScript engine: the expected strings are checked in, so
the gate runs offline and a regression is a diff rather than an environment difference.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.contracts.v1.canonical_json import (
    JCS_MAX_NESTING_DEPTH,
    NUMBER_ADMISSION_CATEGORIES,
    canonical_bytes,
    canonicalize,
    parse_json_document,
    serialize_number,
    utf16_sort_key,
)
from omnivia_core.contracts.v1.compatibility import ContractSemanticError


def _double(bit_pattern: str) -> float:
    """The binary64 value with this big-endian hexadecimal bit pattern."""
    return struct.unpack(">d", bytes.fromhex(bit_pattern))[0]


# --------------------------------------------------------------------------
# 1. Number serialization: ECMAScript Number::toString, which JCS defers to
# --------------------------------------------------------------------------

# The number-serialization vectors RFC 8785 publishes, given as `(IEEE 754 bit pattern,
# expected canonical JSON text)`. They are the interesting cases precisely because a naive
# formatter gets them wrong: negative zero collapsing to `0`, the subnormal extremes, the
# largest exact integer, the 21-digit boundary either side of which the exponential form
# takes over, and the 1e-6 boundary below which it takes over again.
RFC_8785_NUMBER_VECTORS: tuple[tuple[str, str], ...] = (
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("41b3de4355555556", "333333333.3333334"),
)

# Everyday values whose Python `repr` differs from ECMAScript's rendering, which is the
# whole reason `serialize_number` exists rather than a call to `repr` or `json.dumps`.
PYTHON_REPR_DIVERGENCE_VECTORS: tuple[tuple[float, str, str], ...] = (
    (1.0, "1.0", "1"),
    (2.0, "2.0", "2"),
    (1e-7, "1e-07", "1e-7"),
    (1e20, "1e+20", "100000000000000000000"),
    (1e21, "1e+21", "1e+21"),
    (123456789.0, "123456789.0", "123456789"),
    (-0.0, "-0.0", "0"),
)


@pytest.mark.parametrize("bit_pattern,expected", RFC_8785_NUMBER_VECTORS)
def test_rfc_8785_number_vectors(bit_pattern: str, expected: str) -> None:
    assert serialize_number(_double(bit_pattern)) == expected


@pytest.mark.parametrize("value,python_repr,expected", PYTHON_REPR_DIVERGENCE_VECTORS)
def test_serialization_is_ecmascript_not_python(
    value: float, python_repr: str, expected: str
) -> None:
    assert repr(value) == python_repr, "vector no longer demonstrates a divergence"
    assert serialize_number(value) == expected


def test_every_serialized_number_round_trips_to_the_same_double() -> None:
    """Whatever the rendering, it must read back as the identical binary64 value."""
    for bit_pattern, _ in RFC_8785_NUMBER_VECTORS:
        value = _double(bit_pattern)
        # `float(...)` because Python parses an integral JSON literal as an exact `int`,
        # while JSON's number domain is binary64 -- the comparison must be made there.
        assert float(json.loads(serialize_number(value))) == value


def test_integers_render_without_a_decimal_point() -> None:
    assert canonicalize(0) == "0"
    assert canonicalize(1) == "1"
    assert canonicalize(-1) == "-1"
    assert canonicalize(2**53 - 1) == "9007199254740991"


def test_integer_and_float_spellings_of_one_value_canonicalize_identically() -> None:
    """JSON has one number domain, binary64, so `1` and `1.0` are the same value."""
    assert canonicalize({"n": 1}) == canonicalize({"n": 1.0})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(ContractSemanticError, match="finite"):
        canonicalize(value)


def test_integers_outside_binary64_are_rejected() -> None:
    """An integer that cannot round trip through binary64 has no canonical form.

    `2**53 + 1` is the smallest positive integer a double cannot represent: converting it
    yields `2**53`, a different value, which would be signed under the original's name.
    """
    with pytest.raises(ContractSemanticError, match="losslessly"):
        canonicalize(2**53 + 1)
    with pytest.raises(ContractSemanticError, match="binary64"):
        canonicalize(10**400)


def test_integers_that_survive_the_conversion_are_accepted() -> None:
    """The rule is losslessness, not magnitude: a large power of two is exact."""
    assert canonicalize(2**53) == "9007199254740992"


def test_a_large_exact_integer_renders_as_its_shortest_round_tripping_form() -> None:
    """Exactly representable is not the same as exactly spelled out.

    `2**60` is an exact binary64 value, but its neighbouring doubles are 256 apart, so a
    shorter decimal reads back as the very same double -- and ECMAScript's "fewest digits"
    rule picks that one. Rendering all 19 digits would be a different string with a
    different digest for the identical value.
    """
    assert canonicalize(2**60) == "1152921504606847000"
    assert float("1152921504606847000") == float(2**60)


def test_booleans_are_json_literals_not_numbers() -> None:
    assert canonicalize(True) == "true"
    assert canonicalize(False) == "false"
    assert canonicalize(None) == "null"
    with pytest.raises(ContractSemanticError, match="boolean is not a JSON number"):
        serialize_number(True)


# --------------------------------------------------------------------------
# 2. Object member ordering: unsigned UTF-16 code units
# --------------------------------------------------------------------------

# RFC 8785's own property-ordering example. Its point is the last two members: U+1F600
# is a *higher* code point than U+FB33, but its UTF-16 form begins with the high surrogate
# 0xD83D, which sorts *below* 0xFB33 -- so code-point ordering would put them the other way
# round and produce a different document with a different digest.
RFC_8785_ORDERING_INPUT: dict[str, str] = {
    "€": "Euro Sign",
    "\r": "Carriage Return",
    "דּ": "Hebrew Letter Dalet With Dagesh",
    "1": "One",
    "\U0001f600": "Emoji: Grinning Face",
    "": "Control",
    "ö": "Latin Small Letter O With Diaeresis",
}

RFC_8785_ORDERING_EXPECTED: str = (
    '{"\\r":"Carriage Return","1":"One","":"Control",'
    '"ö":"Latin Small Letter O With Diaeresis","€":"Euro Sign",'
    '"\U0001f600":"Emoji: Grinning Face","דּ":"Hebrew Letter Dalet With Dagesh"}'
)


def test_rfc_8785_property_ordering_vector() -> None:
    assert canonicalize(RFC_8785_ORDERING_INPUT) == RFC_8785_ORDERING_EXPECTED


def test_ordering_is_utf16_not_code_point() -> None:
    """The one comparison that separates the two orderings, isolated."""
    emoji, hebrew = "\U0001f600", "דּ"
    assert utf16_sort_key(emoji) < utf16_sort_key(hebrew)
    assert emoji > hebrew, "Python's own code-point ordering disagrees, which is the point"
    assert canonicalize({hebrew: 1, emoji: 2}) == '{"\U0001f600":2,"דּ":1}'


def test_member_order_does_not_change_the_canonical_form() -> None:
    """Reordering an object's members is not a change to the document."""
    forward = canonicalize({"a": 1, "b": 2, "c": 3})
    reverse = canonicalize({"c": 3, "b": 2, "a": 1})
    assert forward == reverse == '{"a":1,"b":2,"c":3}'


def test_array_order_does_change_the_canonical_form() -> None:
    """An array is ordered by definition, so reordering it is a different document."""
    assert canonicalize([1, 2, 3]) != canonicalize([3, 2, 1])


def test_nested_objects_and_arrays_are_ordered_at_every_depth() -> None:
    document = {"z": [{"b": 1, "a": 2}, {"d": [3, {"f": 4, "e": 5}]}], "y": {"x": None}}
    assert canonicalize(document) == (
        '{"y":{"x":null},"z":[{"a":2,"b":1},{"d":[3,{"e":5,"f":4}]}]}'
    )


def test_utf16_sort_key_rejects_a_lone_surrogate() -> None:
    with pytest.raises(ContractSemanticError, match="Unicode scalar sequence"):
        utf16_sort_key("\ud800")


def test_this_layer_is_where_utf16_ordering_is_actually_exercised() -> None:
    """The UTF-16 rule is generic, and this is the only layer that can put it under load.

    Every identity domain built on top of this canonicalizer -- `EvidenceId`, `RecordId`,
    `RecordVersion`, `EvidenceChecksum` -- is ASCII or printable ASCII, where a code unit is
    a code point and the two orderings cannot disagree. So the rule is normative for those
    preimages without being *testable* through them, and the only honest way to hold it to
    account is here, over the arbitrary Unicode member names RFC 8785 actually admits.
    Widening an identity domain to manufacture a divergence would test the rule by breaking
    the thing the rule exists to protect.
    """
    emoji, hebrew = "\U0001f600", "\ufb33"
    by_code_unit = canonicalize({hebrew: 1, emoji: 2})
    by_code_point = "{" + ",".join(
        f'"{name}":{value}'
        for name, value in sorted({hebrew: 1, emoji: 2}.items())
    ) + "}"
    assert by_code_unit != by_code_point
    assert by_code_unit == '{"\U0001f600":2,"\ufb33":1}'
    # The same divergence is what the RFC 8785 vector above turns on, so a canonicalizer
    # that regressed to code-point order would fail both, not merely this one.
    assert utf16_sort_key(emoji) < utf16_sort_key(hebrew)
    assert emoji > hebrew


# --------------------------------------------------------------------------
# 3. String escaping: exactly what JSON.stringify emits
# --------------------------------------------------------------------------

STRING_ESCAPE_VECTORS: tuple[tuple[str, str], ...] = (
    ("", '""'),
    ("plain", '"plain"'),
    ('"', '"\\""'),
    ("\\", '"\\\\"'),
    ("\b", '"\\b"'),
    ("\f", '"\\f"'),
    ("\n", '"\\n"'),
    ("\r", '"\\r"'),
    ("\t", '"\\t"'),
    ("\x00", '"\\u0000"'),
    ("\x1f", '"\\u001f"'),
    ("\x0b", '"\\u000b"'),
    # Not escaped: DEL is not a C0 control, and no non-ASCII scalar is ever escaped -- the
    # UTF-8 encoding carries them.
    ("\x7f", '"\x7f"'),
    ("", '""'),
    ("é", '"é"'),
    ("\U0001f600", '"\U0001f600"'),
    ("/", '"/"'),
)


@pytest.mark.parametrize("value,expected", STRING_ESCAPE_VECTORS)
def test_string_escaping(value: str, expected: str) -> None:
    assert canonicalize(value) == expected


def test_control_escapes_use_lowercase_hex() -> None:
    assert canonicalize("\x1a") == '"\\u001a"'


def test_strings_with_a_lone_surrogate_are_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="lone surrogate"):
        canonicalize("bad\ud800value")
    with pytest.raises(ContractSemanticError, match="lone surrogate"):
        canonicalize({"key": "bad\udfffvalue"})


def test_canonical_bytes_are_utf8() -> None:
    """JCS defines the canonical form as UTF-8 bytes, and a non-ASCII scalar is carried by
    that encoding rather than by an escape."""
    assert canonical_bytes({"k": "€"}) == b'{"k":"\xe2\x82\xac"}'


def test_canonical_bytes_encode_the_canonical_text() -> None:
    document: dict[str, Any] = {"k": "€", "n": 1.5, "a": [True, None]}
    assert canonical_bytes(document) == canonicalize(document).encode("utf-8")


# --------------------------------------------------------------------------
# 4. The I-JSON input domain
# --------------------------------------------------------------------------


def test_non_string_member_names_are_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="member names must be strings"):
        canonicalize({1: "one"})


def test_binary_and_unsupported_values_are_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="binary data"):
        canonicalize(b"bytes")
    with pytest.raises(ContractSemanticError, match="not a JSON value"):
        canonicalize(object())


def test_a_mapping_yielding_a_duplicated_name_is_rejected() -> None:
    """A dict cannot hold a duplicate, but an arbitrary Mapping can yield one."""

    class DuplicateMapping(dict[str, int]):
        def items(self) -> Any:
            return [("a", 1), ("a", 2)]

    with pytest.raises(ContractSemanticError, match="duplicate object member name"):
        canonicalize(DuplicateMapping())


def test_nesting_beyond_the_bound_is_rejected_as_a_contract_error() -> None:
    document: Any = "leaf"
    for _ in range(JCS_MAX_NESTING_DEPTH + 2):
        document = [document]
    with pytest.raises(ContractSemanticError, match="nesting exceeds"):
        canonicalize(document)


# --------------------------------------------------------------------------
# 5. Strict parsing: what an integrity check needs and a tolerant decoder does not
# --------------------------------------------------------------------------


def test_parse_accepts_text_and_utf8_bytes_identically() -> None:
    assert parse_json_document('{"a":1}') == {"a": 1}
    assert parse_json_document(b'{"a":1}') == {"a": 1}


def test_parse_rejects_a_duplicated_member_name() -> None:
    """The one ambiguity no later stage can detect: `json.loads` keeps the last occurrence
    and silently discards the rest, so a digest computed afterwards attests to a document
    nobody sent."""
    assert json.loads('{"a":1,"a":2}') == {"a": 2}, "baseline: the stdlib parser drops it"
    with pytest.raises(ContractSemanticError, match="duplicate object member name"):
        parse_json_document('{"a":1,"a":2}')


def test_parse_rejects_a_nested_duplicated_member_name() -> None:
    with pytest.raises(ContractSemanticError, match="duplicate object member name"):
        parse_json_document('{"outer":{"a":1,"a":2}}')


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_parse_rejects_the_non_standard_constants_python_otherwise_accepts(
    literal: str,
) -> None:
    assert isinstance(json.loads(literal), float), "baseline: the stdlib parser accepts it"
    with pytest.raises(ContractSemanticError, match="not a JSON value"):
        parse_json_document(literal)


def test_parse_rejects_invalid_utf8() -> None:
    with pytest.raises(ContractSemanticError, match="not valid UTF-8"):
        parse_json_document(b'{"a":"\xff"}')


def test_parse_rejects_malformed_json() -> None:
    with pytest.raises(ContractSemanticError, match="not valid JSON"):
        parse_json_document('{"a":}')


def test_parse_rejects_a_wrongly_typed_document() -> None:
    with pytest.raises(ContractSemanticError, match="expected JSON text or UTF-8 bytes"):
        parse_json_document(42)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 6. Cross-language agreement, stated as a checked-in expectation
# --------------------------------------------------------------------------

# One document exercising every rule at once: member ordering across the BMP/supplementary
# boundary, every escape family, the number cases, nesting, and the empty container forms.
# The expected string was produced by this implementation and independently reproduced by
# ECMAScript's `JSON.stringify` under JCS's ordering rule; it is checked in so the gate
# needs no JavaScript engine.
CROSS_LANGUAGE_DOCUMENT: dict[str, Any] = {
    "דּ": "dalet",
    "\U0001f600": "grin",
    "€": 1e21,
    "b": [1, 1.5, -0.0, 2**53 - 1, 1e-7],
    "a": {"nested": {"deep": [True, False, None]}},
    "\r\n\t": "\x00\x1f\x7f\\\"",
    "empty_object": {},
    "empty_array": [],
}

CROSS_LANGUAGE_EXPECTED: str = (
    '{"\\r\\n\\t":"\\u0000\\u001f\x7f\\\\\\"",'
    '"a":{"nested":{"deep":[true,false,null]}},'
    '"b":[1,1.5,0,9007199254740991,1e-7],'
    '"empty_array":[],'
    '"empty_object":{},'
    '"€":1e+21,'
    '"\U0001f600":"grin",'
    '"דּ":"dalet"}'
)


def test_cross_language_canonicalization_vector() -> None:
    assert canonicalize(CROSS_LANGUAGE_DOCUMENT) == CROSS_LANGUAGE_EXPECTED


def test_the_cross_language_vector_round_trips_through_a_json_parser() -> None:
    """The canonical form is still ordinary JSON: re-parsing it yields the same value."""
    assert parse_json_document(CROSS_LANGUAGE_EXPECTED) == json.loads(
        json.dumps(CROSS_LANGUAGE_DOCUMENT)
    )


def test_canonicalizing_the_canonical_form_is_a_fixed_point() -> None:
    reparsed = parse_json_document(CROSS_LANGUAGE_EXPECTED)
    assert canonicalize(reparsed) == CROSS_LANGUAGE_EXPECTED


# --------------------------------------------------------------------------
# 7. OmniVia's application acceptance profile, stated as a test
# --------------------------------------------------------------------------
#
# Two separate claims, and only one is universal. Output: every value this module accepts
# canonicalizes byte-for-byte as RFC 8785 specifies -- which is what makes
# `reproducibility.artifact_canonicalization == "rfc8785"` an honest statement. Input:
# OmniVia accepts a *strict subset* of the documents RFC 8785 could be applied to, rejecting
# numeric literals that would need rounding rather than rounding them.
#
# These tests exist so no reader concludes from `"rfc8785"` that this layer accepts every
# possible RFC 8785 document. It does not, deliberately, and the policy is frozen.

#: Literals a conforming JCS implementation would canonicalize by *rounding* through
#: ECMAScript's number rules, and which this profile refuses instead. The third column is
#: what a rounding implementation would emit -- the value a digest would then attest to.
ROUNDING_WOULD_BE_REQUIRED: tuple[tuple[str, str, str], ...] = (
    ("2**53 + 1", '{"n":9007199254740993}', "9007199254740992"),
    ("2**53 + 3", '{"n":9007199254740995}', "9007199254740996"),
    ("a 30-digit integer", '{"n":123456789012345678901234567890}', "1.2345678901234568e+29"),
)


@pytest.mark.parametrize(
    "description,document,rounded", ROUNDING_WOULD_BE_REQUIRED, ids=[c[0] for c in ROUNDING_WOULD_BE_REQUIRED]
)
def test_a_literal_needing_rounding_is_refused_not_rounded(
    description: str, document: str, rounded: str
) -> None:
    """The frozen input policy: rounding is fine for serialization and fatal for a content
    address, so the document is refused before canonicalization ever runs."""
    value = parse_json_document(document)  # valid JSON; the parser is not the gate
    with pytest.raises(ContractSemanticError, match="losslessly|binary64"):
        canonicalize(value)
    # And the refusal is not vacuous: rounding really would have produced a different number
    # under this same module's ECMAScript rules, which is the value a digest would sign.
    assert serialize_number(float(value["n"])) == rounded  # type: ignore[index,call-overload]
    assert serialize_number(float(value["n"])) != str(value["n"])  # type: ignore[index,call-overload]


def test_a_fractional_literal_needing_rounding_canonicalizes_as_jcs_specifies() -> None:
    """Where the profile is *not* stricter, and why.

    The line falls exactly where the JSON parser stops being lossless. An integer literal
    arrives exact, so an out-of-range one is visible here and refused. A fractional literal
    is already rounded to the nearest double by the parser, so it arrives as the binary64
    value JCS also operates on -- and canonicalizes to precisely what JCS specifies for it.
    Nothing is refused that a conforming implementation would have handled differently.
    """
    value = parse_json_document('{"n":0.1000000000000000055511151231257827}')
    assert canonicalize(value) == '{"n":0.1}'


def test_every_accepted_value_still_canonicalizes_as_rfc_8785() -> None:
    """The universal half of the claim, over the same document the cross-language vector
    uses: acceptance is narrower, serialization is unchanged RFC 8785."""
    assert canonicalize(CROSS_LANGUAGE_DOCUMENT) == CROSS_LANGUAGE_EXPECTED
    for value in (0, -0.0, 1, 1.5, 2**53 - 1, 2**53, 1e21, 1e-7, -1e-7):
        rendered = serialize_number(float(value))
        # ECMAScript-number equivalence: the rendered form reads back as the same double.
        assert float(rendered) == float(value)
        # And it is the form JCS emits, not Python's `repr`.
        assert canonicalize(value) == rendered


def test_the_acceptance_profile_is_a_strict_subset_not_a_superset() -> None:
    """Stated as one assertion so it cannot be read the other way round: there exists a
    valid JSON document RFC 8785 can canonicalize and this module refuses, and there exists
    no document this module accepts whose canonical form departs from RFC 8785."""
    refused = parse_json_document('{"n":9007199254740993}')
    with pytest.raises(ContractSemanticError):
        canonicalize(refused)
    accepted = parse_json_document(CROSS_LANGUAGE_EXPECTED)
    assert canonicalize(accepted) == CROSS_LANGUAGE_EXPECTED


def test_the_context_pack_still_declares_rfc8785() -> None:
    """The narrower acceptance profile does not rename the canonicalization: what a verifier
    must reproduce for an *accepted* artifact is RFC 8785 and nothing else."""
    from omnivia_core.contracts.v1 import semantics_knowledge as sem_knowledge

    assert sem_knowledge.CONTEXT_PACK_ARTIFACT_CANONICALIZATION == "rfc8785"


# --------------------------------------------------------------------------
# 8. The frozen numeric admission boundaries, as a language-neutral fixture
#
# The rule is stated once, in `tests/contracts/fixtures/context-pack-canonicalization-v1.json`,
# as raw JSON *text* rather than as host-language values: a fixture that stored `0.1` as a
# Python float would have had a Python parser round it before any implementation saw it, and
# the whole point of these boundaries is what happens during parsing. A second implementation
# in another language reads the same file and is held to the same nine verdicts.
# --------------------------------------------------------------------------

CANONICALIZATION_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "context-pack-canonicalization-v1.json"
)
CANONICALIZATION_FIXTURE: dict[str, Any] = json.loads(
    CANONICALIZATION_FIXTURE_PATH.read_text(encoding="utf-8")
)
CANONICALIZATION_VECTORS: tuple[dict[str, Any], ...] = tuple(
    CANONICALIZATION_FIXTURE["vectors"]
)


def test_canonicalization_fixture_declares_the_frozen_boundaries() -> None:
    """The vector set itself is frozen: a boundary silently dropped from the file would make
    every test below pass while checking less."""
    assert tuple(vector["id"] for vector in CANONICALIZATION_VECTORS) == (
        "two-pow-53-minus-one",
        "two-pow-53",
        "two-pow-53-plus-one",
        "negative-two-pow-53-plus-one",
        "two-pow-60",
        "two-pow-60-plus-one",
        "one-tenth",
        "one-e-plus-twenty-one",
        "overflowing-exponent",
    )
    assert CANONICALIZATION_FIXTURE["artifact_canonicalization"] == "rfc8785"
    assert CANONICALIZATION_FIXTURE["pack_format_version"] == "1.0"
    assert set(CANONICALIZATION_FIXTURE["error_categories"]) == set(
        NUMBER_ADMISSION_CATEGORIES
    )


@pytest.mark.parametrize(
    "vector",
    CANONICALIZATION_VECTORS,
    ids=[vector["id"] for vector in CANONICALIZATION_VECTORS],
)
def test_canonicalization_vector(vector: dict[str, Any]) -> None:
    """Parse the raw token, then canonicalize: accepted vectors must produce exactly the
    stated canonical text, rejected ones must fail in the stated category."""
    if vector["admitted"]:
        value = parse_json_document(vector["input_json"])
        assert canonicalize(value) == vector["canonical_json"]
        return
    category = vector["error_category"]
    assert category in NUMBER_ADMISSION_CATEGORIES
    with pytest.raises(ContractSemanticError) as raised:
        canonicalize(parse_json_document(vector["input_json"]))
    assert NUMBER_ADMISSION_CATEGORIES[category] in str(raised.value)


@pytest.mark.parametrize(
    "vector",
    [vector for vector in CANONICALIZATION_VECTORS if vector.get("host_integral")],
    ids=[
        vector["id"] for vector in CANONICALIZATION_VECTORS if vector.get("host_integral")
    ],
)
def test_canonicalization_vector_reaches_the_same_verdict_for_a_host_integer(
    vector: dict[str, Any],
) -> None:
    """The same rule reaches a host-language integer handed in directly, which never passed
    through a parser at all -- otherwise an in-process caller could smuggle past a boundary
    the wire path refuses."""
    value = int(vector["input_json"])
    if vector["admitted"]:
        assert canonicalize(value) == vector["canonical_json"]
        assert canonicalize({"n": value}) == '{"n":' + vector["canonical_json"] + "}"
        return
    with pytest.raises(ContractSemanticError) as raised:
        canonicalize(value)
    assert NUMBER_ADMISSION_CATEGORIES[vector["error_category"]] in str(raised.value)


def test_the_admission_rule_is_exactness_not_a_safe_integer_range() -> None:
    """Restated as the property, not the examples: every exact power of two up to the
    binary64 exponent range is admitted, far above 2**53, while its immediate successor is
    refused as soon as the spacing exceeds one."""
    for exponent in range(53, 200):
        exact = 2**exponent
        assert canonicalize(exact) == serialize_number(float(exact))
        with pytest.raises(ContractSemanticError, match="does not convert losslessly"):
            canonicalize(exact + 1)


def test_the_admission_rule_does_not_require_exact_decimal_representation() -> None:
    """A decimal or exponent token is read as the nearest finite binary64 under ordinary JCS
    rules. Requiring an exact decimal rational would refuse almost every fraction there is."""
    for token, expected in (("0.1", "0.1"), ("0.3", "0.3"), ("1e+21", "1e+21"), ("1.5", "1.5")):
        assert canonicalize(parse_json_document(token)) == expected


def test_the_context_pack_schema_exposes_only_the_literal_rfc8785() -> None:
    """The admission rule narrows the input domain and changes no output byte, so the stated
    canonicalization stays exactly `rfc8785` -- one value, in the schema as in the module."""
    from omnivia_core.contracts.v1 import semantics_knowledge as sem_knowledge

    schema = json.loads(
        (
            Path(__file__).resolve().parent.parent.parent
            / "contracts"
            / "application"
            / "v1"
            / "schemas"
            / "context-pack.schema.json"
        ).read_text(encoding="utf-8")
    )
    field = schema["$defs"]["ContextPackReproducibility"]["properties"][
        "artifact_canonicalization"
    ]
    assert field["$ref"].endswith("common.schema.json#/$defs/OpenCode")
    assert "`rfc8785`" in field["description"]
    assert sem_knowledge.CONTEXT_PACK_ARTIFACT_CANONICALIZATION == "rfc8785"
    # No other canonicalization name is offered anywhere in the document.
    assert "json_sorted_keys" not in json.dumps(schema)
