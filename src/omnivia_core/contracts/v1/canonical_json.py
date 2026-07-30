"""RFC 8785 JSON Canonicalization Scheme (JCS) over the I-JSON value domain (ADR-039).

The Context Pack is a content-addressed artifact: its `pack_id` and
`reproducibility.artifact_checksum` are a SHA-256 over the canonical bytes of the rest of
the result, so two independent implementations must agree byte for byte on how one JSON
value serializes. `json.dumps(sort_keys=True)` is not that rule -- it orders keys by
Unicode code point rather than by UTF-16 code unit, and it renders numbers with Python's
`repr` formatting rather than ECMAScript's -- so this module implements the actual scheme.

Scope, and what is deliberately *not* claimed:

- object members are ordered by the UTF-16 code-unit sequence of their names
  (:func:`utf16_sort_key`), which differs from code-point order for every name containing
  a supplementary character -- the case the RFC's own property-ordering example turns on;
- numbers are rendered by :func:`serialize_number`, an implementation of ECMA-262's
  `Number::toString(x, 10)` abstract operation, which JCS defers to for number
  serialization. The *shortest round-tripping decimal digit string* comes from CPython's
  `repr(float)` (David Gay's shortest-representation algorithm, mode 0), which selects the
  same digits ECMAScript's "k as small as possible" requirement does; the *formatting* of
  those digits -- where the decimal point lands, the 21-digit and 1e-6 thresholds at which
  the exponential form takes over, and the `e+`/`e-` spelling -- is implemented here from
  the specification's own case analysis rather than borrowed from Python's float
  formatting, which differs on all three. No external source text is reproduced here;
- strings are escaped exactly as ECMAScript's `JSON.stringify` escapes them: the seven
  short escapes, `\\u00xx` with lowercase hex for the remaining C0 controls, and every
  other Unicode scalar value literal in UTF-8;
- the input domain is I-JSON (RFC 7493), enforced recursively by :func:`canonicalize`:
  finite binary64 numbers only, Python integers only when the conversion to binary64 is
  lossless, strings that are valid Unicode scalar sequences with no lone surrogate,
  string-keyed objects, and no duplicate member name. A value outside that domain has no
  single canonical form, so it is rejected rather than guessed at.

**OmniVia's application acceptance profile, stated plainly.** This module is an RFC 8785
*serializer* over a deliberately narrower *input domain* than "every JSON document RFC 8785
can be applied to". The two halves are separate claims and only one of them is universal:

- **Output, universal.** Every value this module accepts serializes byte-for-byte as RFC
  8785 specifies, with ECMAScript `Number::toString` number rendering and UTF-16 member
  ordering. An accepted document's canonical form is exactly the canonical form any
  conforming JCS implementation produces for the same value. That is what makes
  `reproducibility.artifact_canonicalization` legitimately `"rfc8785"`, and it does not
  change.
- **Input, stricter.** A JSON numeric literal whose value cannot round-trip through binary64
  is *rejected here rather than rounded*. JCS defers number serialization to ECMAScript, so
  a conforming JCS implementation handed the literal `9007199254740993` (2**53 + 1) emits
  `9007199254740992` -- a different number, silently, and therefore a digest over a value
  the sender never wrote. This contract refuses the input instead. A content-addressed
  artifact whose integrity check depends on a lossy conversion is not integrity-checked.

**The admission rule, stated provider-neutrally and frozen.** Context Pack format 1.0
applies RFC 8785 byte serialization to an admitted I-JSON data model. Two conditions, in
this order:

1. every number must be a finite binary64 before canonicalization -- `1e400` overflows to
   infinity and is refused, as are NaN and Infinity spellings
   (:func:`parse_json_document`);
2. additionally, any JSON number token written in *integer form*, and any direct
   host-language *integral value*, is admitted only if converting it to binary64 and back to
   the mathematical integer is exact.

Decimal and exponent tokens are interpreted as finite binary64 under ordinary JCS rules;
an exact decimal rational representation is *not* required, which is why `0.1` is admitted
and canonicalizes to `0.1` even though no binary64 equals one tenth.

This is an admission rule about integer identities and counts, and deliberately **not** a
"safe integer" range. Exact larger integers stay valid: `1152921504606846976` (2**60) is
admitted and canonicalizes to `1152921504606847000`, while `1152921504606846977` is refused.
The frozen provider-neutral boundaries, in both directions:

===========================  ========  =========================
value                        admitted  canonical form / reason
===========================  ========  =========================
`9007199254740991`           yes       `9007199254740991`
`9007199254740992`           yes       `9007199254740992`
`9007199254740993`           no        not exact in binary64
`-9007199254740993`          no        not exact in binary64
`1152921504606846976`        yes       `1152921504606847000`
`1152921504606846977`        no        not exact in binary64
`0.1`                        yes       `0.1`
`1e+21`                      yes       `1e+21`
`1e400`                      no        not finite
===========================  ========  =========================

The line falls exactly where the JSON parser stops being lossless. Python parses integer
literals exactly, so an out-of-range *integer* reaches :func:`_require_binary64` intact and
is refused; it parses fractional literals by rounding to the nearest double, so
`0.1000000000000000055511151231257827` arrives already as `0.1` and canonicalizes to `0.1`,
which is what JCS specifies for it. The same rule reaches a host-language integral value
handed in directly, which never passed through a parser at all.

The practical consequence, and the sentence to keep in mind when reading any claim about
this module: OmniVia accepts a strict subset of RFC-8785-canonicalizable documents, and
canonicalizes every member of that subset exactly as RFC 8785 requires. It does not accept
every possible RFC 8785 document, and it never claims to.
`reproducibility.artifact_canonicalization` stays exactly `rfc8785` because the *output* rule
is unchanged RFC 8785 for every admitted value. This input policy is frozen -- loosening it
to match a conforming implementation's rounding behaviour would change which documents
verify, which is a contract change, not an implementation detail -- and the boundaries above
are restated as a language-neutral vector fixture at
``tests/contracts/fixtures/context-pack-canonicalization-v1.json`` so a second implementation
can be held to the same line without reading this module.

Standard library only, exactly like every other module under
``omnivia_core.contracts.v1``: nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework, and no third-party canonicalizer is used.

Every function here is a *direct* entry point that may be handed a hand-built value, so a
wrongly typed argument raises `ContractSemanticError` and never a raw
`TypeError`/`AttributeError`/`KeyError`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any, Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError

__all__ = [
    "JCS_MAX_NESTING_DEPTH",
    "NUMBER_ADMISSION_CATEGORIES",
    "canonical_bytes",
    "canonicalize",
    "parse_json_document",
    "serialize_number",
    "utf16_sort_key",
]

NUMBER_ADMISSION_CATEGORIES: Final[Mapping[str, str]] = {
    "non_finite_number": "is not a finite number",
    "inexact_integer": "does not convert losslessly to binary64",
}
"""The two stable reasons a number is refused, and the phrase each diagnostic carries.

Names rather than messages are what a cross-language vector fixture can assert against:
``tests/contracts/fixtures/context-pack-canonicalization-v1.json`` labels every rejected
vector with one of these keys, so a second implementation can be checked against the same
boundary without matching English prose, while this mapping keeps the Python diagnostics
honest about which category they actually report. The keys are frozen; the phrases are the
substring each corresponding `ContractSemanticError` is guaranteed to contain."""

JCS_MAX_NESTING_DEPTH: Final = 64
"""Maximum nesting depth :func:`canonicalize` will descend.

Not a JCS rule: a bound so an adversarially deep document fails as a
`ContractSemanticError` rather than as a `RecursionError` escaping the contract layer.
Far deeper than any v1 payload, whose depth is fixed by the schemas.
"""

_SURROGATE_FIRST: Final = 0xD800
_SURROGATE_LAST: Final = 0xDFFF

#: Exponent bounds of ECMAScript's non-exponential rendering window. A decimal point
#: position `n` (the value being `0.<digits> x 10**n`) inside `-6 < n <= 21` is written
#: positionally; outside it, in exponential form.
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
"""The seven two-character escapes `JSON.stringify` emits. Every other C0 control is
written `\\u00xx` with lowercase hex; `\\u007f` (DEL) is *not* escaped, and neither is any
non-ASCII scalar value -- both are emitted literally and carried by the UTF-8 encoding."""


# --------------------------------------------------------------------------
# UTF-16 ordering
# --------------------------------------------------------------------------


def utf16_sort_key(value: str) -> bytes:
    """Return the sort key that orders `value` by unsigned UTF-16 code unit.

    JCS orders object member names by their UTF-16 code-unit sequences, not by their
    Unicode code points. The two disagree for every name containing a supplementary
    character: U+1F600 is one code point above U+FB33, but its UTF-16 form starts with the
    high surrogate 0xD83D, which sorts *below* 0xFB33. Comparing big-endian UTF-16 bytes
    lexicographically is exactly comparing the unsigned 16-bit units in order, because
    every unit occupies the same two bytes with the more significant byte first.

    The same key is what this contract orders *every* identity-bearing array of arbitrary
    Unicode strings by, so a caller sorting citations, sections, or record identities and
    a caller canonicalizing the enclosing artifact can never disagree on order.

    A lone surrogate has no UTF-16 encoding as a scalar value and is rejected here, which
    is the same I-JSON rule :func:`canonicalize` applies to string contents.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractSemanticError(
            f"utf16_sort_key: expected a string, got {type(value).__name__}"
        )
    try:
        return value.encode("utf-16-be", "strict")
    except UnicodeEncodeError as error:
        raise ContractSemanticError(
            f"utf16_sort_key: {value!r} is not a valid Unicode scalar sequence "
            "(a lone surrogate has no UTF-16 encoding and no canonical JSON form)"
        ) from error


# --------------------------------------------------------------------------
# ECMAScript number serialization
# --------------------------------------------------------------------------


def _require_binary64(value: object, path: str) -> float:
    """Return `value` as a finite binary64, or raise.

    A `bool` is not a JSON number here even though Python makes it an `int`: it is the
    JSON literal `true`/`false` and is handled before this function is ever reached. A
    Python integer is accepted only when the conversion to binary64 is *lossless* --
    `int(float(value)) == value` exactly -- so an integer this contract could not round
    trip through the binary64 number domain JSON actually has is rejected instead of being
    silently rounded to a different value with a different digest.

    This is the one place OmniVia's acceptance profile is deliberately narrower than a
    conforming JCS implementation's (see this module's docstring): JCS defers to
    ECMAScript, which would *round* `9007199254740993` to `9007199254740992` and canonicalize
    that. Rounding is fine for serialization and fatal for a content address, so the input is
    refused here rather than converted. The policy is frozen; the *output* rule for every
    accepted value is unchanged RFC 8785.

    The rule is exactness, not magnitude: `9007199254740992` (2**53) and `1152921504606846976`
    (2**60) are both admitted because both convert to binary64 and back exactly, while
    `9007199254740993` and `1152921504606846977` are refused because neither does. Calling
    this a "safe integer range" would describe a different, narrower rule than the one
    implemented here. A `float` argument has already been through that conversion by the time
    it arrives, so only finiteness is left to check on it; the exactness test applies to the
    integers -- parsed from an integer-form token, or handed in directly by a caller -- that
    still carry their exact value.

    Rejections fall into exactly the two categories :data:`NUMBER_ADMISSION_CATEGORIES` names,
    and each diagnostic below carries that category's frozen phrase.
    """
    if isinstance(value, bool):
        raise ContractSemanticError(f"{path}: a boolean is not a JSON number")
    if isinstance(value, int):
        try:
            number = float(value)
        except OverflowError as error:
            raise ContractSemanticError(
                f"{path}: integer {value!r} is not a finite number once converted to "
                "binary64, and is not expressible in JSON"
            ) from error
        if not isfinite(number) or int(number) != value:
            raise ContractSemanticError(
                f"{path}: integer {value!r} does not convert losslessly to binary64; "
                "the JSON number domain is binary64, so a value that cannot round trip "
                "through it has no canonical form"
            )
        return number
    if isinstance(value, float):
        if not isfinite(value):
            raise ContractSemanticError(
                f"{path}: {value!r} is not a finite number and is not expressible in JSON"
            )
        return value
    raise ContractSemanticError(f"{path}: expected a number, got {type(value).__name__}")


def _shortest_decimal(number: float) -> tuple[str, int]:
    """Decompose a positive finite binary64 into `(digits, point)`.

    `digits` is the shortest decimal digit string that reads back as exactly `number`,
    carrying no leading or trailing zero, and `point` is the position of the decimal point
    relative to it: `number == 0.<digits> x 10**point`. In the specification's terms
    `digits` is `s` written out, `len(digits)` is `k`, and `point` is `n`.

    The digits come from `repr`, which CPython produces with David Gay's
    shortest-representation algorithm in mode 0: the shortest digit string that rounds
    back to the same double, ties broken to even. That is the same selection ECMAScript
    specifies when it requires `k` to be as small as possible -- so only the *placement*
    of those digits is left for :func:`serialize_number` to decide, and `repr`'s own
    formatting decisions (which differ from ECMAScript's) are discarded here.
    """
    text = repr(number)
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    integer_part, _, fraction_part = mantissa.partition(".")
    digits = integer_part + fraction_part
    # `number == int(digits) * 10 ** scale` at every step below.
    scale = exponent - len(fraction_part)
    significant = digits.lstrip("0")
    trimmed = significant.rstrip("0")
    scale += len(significant) - len(trimmed)
    return trimmed, len(trimmed) + scale


def serialize_number(value: float, path: str = "$") -> str:
    """Render `value` exactly as ECMAScript's `Number::toString(value, 10)` does.

    This is the number rule JCS defers to, and it is not Python's: `repr(1e21)` is
    `'1e+21'` where ECMAScript writes `1e+21` too, but `repr(1.0)` is `'1.0'` where
    ECMAScript writes `1`, `repr(1e-7)` is `'1e-07'` where ECMAScript writes `1e-7`, and
    `repr(1e20)` is `'1e+20'` where ECMAScript writes the positional `100000000000000000000`.
    Only the digit *selection* is shared (:func:`_shortest_decimal`).

    Given the shortest digits `s` (length `k`) and the decimal-point position `n`, the
    specification's four cases are, in order: an integer written out with trailing zeros
    when `k <= n <= 21`; a positional fraction when `0 < n <= 21`; a leading `0.` with
    `-n` padding zeros when `-6 < n <= 0`; and otherwise the exponential form, whose
    mantissa carries a decimal point only when there is more than one digit. Negative zero
    renders as `0`, exactly as ECMAScript's string conversion does -- JSON has one zero.
    """
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


# --------------------------------------------------------------------------
# String serialization
# --------------------------------------------------------------------------


def _serialize_string(value: str, path: str) -> str:
    """Render `value` as a JSON string literal with `JSON.stringify`'s exact escaping."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractSemanticError(
            f"{path}: expected a string, got {type(value).__name__}"
        )
    pieces: list[str] = ['"']
    for character in value:
        code_point = ord(character)
        if _SURROGATE_FIRST <= code_point <= _SURROGATE_LAST:
            raise ContractSemanticError(
                f"{path}: contains the lone surrogate U+{code_point:04X}; a canonical "
                "JSON string is a sequence of Unicode scalar values"
            )
        escape = _SHORT_ESCAPES.get(character)
        if escape is not None:
            pieces.append(escape)
        elif code_point < 0x20:
            pieces.append(f"\\u{code_point:04x}")
        else:
            pieces.append(character)
    pieces.append('"')
    return "".join(pieces)


# --------------------------------------------------------------------------
# Canonicalization
# --------------------------------------------------------------------------


def _canonicalize_into(value: object, path: str, depth: int, out: list[str]) -> None:
    """Append the canonical serialization of `value` to `out`."""
    if depth > JCS_MAX_NESTING_DEPTH:
        raise ContractSemanticError(
            f"{path}: nesting exceeds the maximum canonicalization depth of "
            f"{JCS_MAX_NESTING_DEPTH}"
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
        raise ContractSemanticError(f"{path}: binary data is not a JSON value")
    if isinstance(value, Mapping):
        entries: list[tuple[bytes, str, object]] = []
        seen: set[str] = set()
        for key, item in value.items():
            if isinstance(key, bool) or not isinstance(key, str):
                raise ContractSemanticError(
                    f"{path}: object member names must be strings, got "
                    f"{type(key).__name__}"
                )
            if key in seen:
                raise ContractSemanticError(
                    f"{path}: duplicate object member name {key!r}; a duplicated name has "
                    "no canonical form and no single value"
                )
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
    raise ContractSemanticError(
        f"{path}: {type(value).__name__} is not a JSON value"
    )


def canonicalize(value: object, path: str = "$") -> str:
    """Return the RFC 8785 canonical JSON text of `value`.

    Enforces the I-JSON input domain recursively while serializing, because a value
    outside it has no single canonical form: non-finite numbers, integers that do not
    convert losslessly to binary64, lone surrogates, non-string member names, duplicate
    member names, and anything that is not a JSON value at all are all rejected rather
    than coerced. Ordering, escaping, and number rendering are described in this module's
    docstring.
    """
    out: list[str] = []
    _canonicalize_into(value, path, 0, out)
    return "".join(out)


def canonical_bytes(value: object, path: str = "$") -> bytes:
    """Return the UTF-8 encoding of :func:`canonicalize`, which is what a digest hashes.

    JCS defines the canonical form as UTF-8 bytes; hashing the `str` under any other
    encoding would produce a digest no other implementation reproduces.
    """
    return canonicalize(value, path).encode("utf-8")


# --------------------------------------------------------------------------
# Strict parsing
# --------------------------------------------------------------------------


def _reject_duplicate_member_names(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object from its raw member pairs, refusing any repeated name.

    The one thing a parser cannot report after the fact: `json.loads` keeps the last
    occurrence of a duplicated name and drops the rest, so by the time a mapping exists
    the duplicate is unrecoverable. An integrity check that hashed such a mapping would be
    signing a document the sender never wrote, so duplicates are refused at parse time --
    the only point where they are still visible.
    """
    seen: dict[str, Any] = {}
    for name, item in pairs:
        if name in seen:
            raise ContractSemanticError(
                f"duplicate object member name {name!r}; a document that names one member "
                "twice has no single value and cannot be integrity-checked"
            )
        seen[name] = item
    return seen


def _reject_json_constant(name: str) -> object:
    raise ContractSemanticError(
        f"{name!r} is not a JSON value; the JSON number domain carries no NaN or Infinity"
    )


def parse_json_document(document: str | bytes) -> object:
    """Parse a raw JSON document, refusing anything an integrity check could not verify.

    Distinct from the tolerant DTO decoders in :mod:`generated`, and deliberately so:
    those decode a *value*, this one preserves a *document*. It rejects invalid UTF-8, the
    non-standard `NaN`/`Infinity` literals Python's parser otherwise accepts, and -- the
    reason it exists -- any duplicated object member name, which no later stage can detect
    because the parser has already discarded it.

    The value domain itself (finite binary64 numbers, scalar-value strings) is enforced by
    :func:`canonicalize`, which the integrity path always runs next.
    """
    if isinstance(document, (bytes, bytearray)):
        try:
            text = bytes(document).decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise ContractSemanticError(
                f"document is not valid UTF-8: {error}"
            ) from error
    elif isinstance(document, str):
        text = document
    else:
        raise ContractSemanticError(
            f"document: expected JSON text or UTF-8 bytes, got {type(document).__name__}"
        )
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_member_names,
            parse_constant=_reject_json_constant,
        )
    except ContractSemanticError:
        raise
    except (ValueError, RecursionError) as error:
        raise ContractSemanticError(f"document is not valid JSON: {error}") from error
