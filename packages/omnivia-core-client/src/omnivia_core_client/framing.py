"""The frozen OVC1 frame: one JSON object, canonically encoded, length-prefixed.

A frame is exactly three parts, in order:

===============  =====  ==========================================================
part             bytes  content
===============  =====  ==========================================================
magic                4  the ASCII bytes ``OVC1`` (``4f564331``)
length               4  unsigned big-endian ``uint32``: the JSON byte count
JSON             *len*  exactly those canonical UTF-8 bytes, nothing after them
===============  =====  ==========================================================

The JSON payload is at most :data:`MAXIMUM_JSON_BYTES` (4 MiB, inclusive), and
its root is always a JSON object.

**This module is pure.** It turns a mapping into bytes and bytes back into a
mapping. It opens no socket and no pipe, reads from nothing and writes to
nothing, and holds no state between calls. It is therefore *not* a connection
layer and claims none of what one would provide: no pooling, no multiplexing of
concurrent calls over one connection, no streaming or chunked payloads, and no
server-initiated push. One frame in, one frame out.

**The canonical form is RFC 8785 (JCS) over the admitted I-JSON domain.** The
freeze requires a cross-language canonical UTF-8 JSON encoding and does not
leave the algorithm to each implementation, so this package names one and only
one: :func:`omnivia_core.contracts.v1.canonical_json.canonical_bytes`, Core's
public implementation of RFC 8785, together with the admission policy stated in
that module. Nothing here re-implements, wraps, or narrows it -- there is no
second, Python-specific canonical form to disagree with it. Concretely, and this
is the whole list a second implementation must match:

- object members are ordered by their **UTF-16 code-unit** sequence, not by
  Unicode code point. The two disagree for every member name containing a
  supplementary character: a name beginning U+1F600 sorts *below* one
  beginning U+FB33, because its UTF-16 form starts with the high surrogate
  ``0xD83D``;
- numbers are rendered by ECMAScript's ``Number::toString(x, 10)``, which JCS
  defers to. So ``1.0`` and ``-0.0`` are spelled ``1`` and ``0``, ``1e20`` is
  written out positionally as ``100000000000000000000``, and the exponential
  form takes over at ``1e+21`` and at ``1e-7``;
- strings carry the seven short escapes and ``\\u00xx`` for the remaining C0
  controls, and every other Unicode scalar value literally in UTF-8;
- the value domain is I-JSON: no non-finite number, no integer without an exact
  binary64 value, no lone surrogate, no non-string or duplicated member name,
  and no nesting past :data:`MAXIMUM_JSON_NESTING_DEPTH` levels. A value outside
  that domain has no single canonical form, so it is refused rather than guessed
  at -- a member name is never coerced to a string, and a number is never
  silently rounded to one the sender did not write.

Decoding does not merely parse: it re-encodes what it parsed and requires the
result to equal the received bytes exactly, so a frame that is valid JSON but a
different *spelling* of the same value is rejected rather than accepted and
silently re-emitted differently. That is what lets a frame's bytes be quoted,
hashed, or compared across two implementations and mean the same thing in both.

**Encoding is closed under decoding: every frame returned here decodes.** Two
separately conformant halves do not add up to a total composition on their own.
The admitted domain is a rule about a *value* -- an integer is admitted when
converting it to binary64 and back is exact -- while what goes on the wire is
the decimal token that value renders to, and that token has to clear the same
rule as a value in its own right. Usually it does. ``1152921504606846976``
(2**60) is exact, renders as ``1152921504606847000``, and that decimal is not
exact, so those bytes are refused on arrival.

OVC1 closes that gap at the transport boundary rather than in the
canonicalizer, which is frozen and stays as it is: this module's encoder
returns bytes only after the decode path's own admission has accepted them, and
raises :class:`~omnivia_core_client.errors.ProtocolError` otherwise. Nothing
about the admitted domain moves. The rule is still exactness and not magnitude,
and it is not a safe-integer range: ``18014398509481984`` (2**54) is above 2**53
and still encodes, because its canonical token is itself. What changes is only
that a payload with no deliverable frame now fails at the sender, which can do
something about it, instead of at a peer that cannot.

Standard library plus the public ``omnivia_core`` contracts only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1 import ContractSemanticError
from omnivia_core.contracts.v1.canonical_json import (
    JCS_MAX_NESTING_DEPTH,
    canonical_bytes,
    parse_json_document,
)
from omnivia_core_client.errors import ProtocolError

__all__ = [
    "CANONICAL_JSON_ALGORITHM",
    "FRAME_FORMAT",
    "HEADER_BYTES",
    "LENGTH_BYTES",
    "MAGIC",
    "MAGIC_HEX",
    "MAXIMUM_JSON_BYTES",
    "MAXIMUM_JSON_NESTING_DEPTH",
    "canonical_json_bytes",
    "decode_frame",
    "encode_frame",
]

FRAME_FORMAT: Final = "omnivia.ovc1.v1"
"""Identifier of this frozen frame format, and of the cross-language vector
manifest that pins it (``tests/fixtures/ovc1-v1.json``)."""

MAGIC: Final = b"OVC1"
"""The four ASCII bytes every frame starts with. Not a version marker: a later
frame format gets its own magic rather than a different value here."""

MAGIC_HEX: Final = "4f564331"
"""``MAGIC`` in lowercase hex, as the vector manifest states it."""

LENGTH_BYTES: Final = 4
"""Width of the length field: an unsigned big-endian ``uint32``. Big-endian
because that is what "network byte order" means everywhere else on a wire, and
a reader in another language should not have to check."""

HEADER_BYTES: Final = 8
"""``MAGIC`` plus the length field. A reader needs exactly this many bytes
before it knows how many more to expect."""

MAXIMUM_JSON_BYTES: Final = 4 * 1024 * 1024
"""Largest JSON payload a frame may carry, 4194304 bytes, **inclusive**.

A bound rather than no bound, because a length field is a promise from a peer
and a reader that trusts it allocates whatever it is told to. Refusing an
over-large frame is how a malfunctioning or hostile local peer fails as one
bad frame instead of as memory exhaustion somewhere else entirely. The limit is
part of the frozen format: both peers must agree on it, or one will send frames
the other structurally cannot accept."""

CANONICAL_JSON_ALGORITHM: Final = "rfc8785"
"""The name of the frozen canonicalization, as the vector manifest states it.

The same identifier Core's contracts publish for this algorithm, so a reader of
a frame vector and a reader of a Context Pack are told the same thing about the
bytes."""

MAXIMUM_JSON_NESTING_DEPTH: Final = JCS_MAX_NESTING_DEPTH
"""Deepest nesting the canonical form admits, read from Core rather than
restated, so the two can never drift apart.

Part of the frozen format even though it is not an RFC 8785 rule: a document
deeper than this has no canonical bytes here, so both peers must agree on it or
one will send frames the other structurally cannot accept. Far deeper than any
v1 payload, whose depth is fixed by the schemas."""

_JSON_KINDS: Final[Mapping[type, str]] = {
    bool: "a JSON boolean",
    dict: "a JSON object",
    float: "a JSON number",
    int: "a JSON number",
    list: "a JSON array",
    str: "a JSON string",
}

_NOT_ADMISSIBLE_VALUE: Final = (
    "frame payload is outside the OVC1 canonical JSON domain, which is RFC 8785 "
    "over I-JSON: no member name that is not a string, no name repeated in one "
    "object, no lone surrogate, no NaN or infinity, no integer without an exact "
    f"binary64 value, and no nesting past {MAXIMUM_JSON_NESTING_DEPTH} levels"
)
"""Why a value has no canonical bytes. It names the *rule set*, never which rule
the value broke, because naming that would mean naming the value."""

_UNRENDERABLE_VALUE: Final = (
    "frame payload carries a value the canonical encoder cannot render at all, "
    "such as an integer with more decimal digits than the interpreter will convert"
)

_TOO_DEEP_TO_CANONICALIZE: Final = "frame payload nests too deeply to canonicalize"

_TOO_DEEP_TO_PARSE: Final = "frame JSON nests too deeply to parse"

_UNPARSEABLE_NUMBER: Final = (
    "frame JSON carries a number the parser will not convert, such as an integer "
    "written with more decimal digits than the interpreter admits"
)

_NOT_ADMISSIBLE_DOCUMENT: Final = (
    "frame JSON is not an admissible OVC1 document: it names one member twice, or "
    "carries a NaN or Infinity literal, and the OVC1 JSON domain has neither"
)

_NOT_CANONICAL: Final = (
    "frame JSON is not in canonical form: OVC1 carries the RFC 8785 bytes of its "
    "value -- UTF-8, members in UTF-16 code-unit order, compact separators, "
    "ECMAScript number spelling, and no byte after the closing brace"
)

_NOT_DECODABLE_ONCE_ENCODED: Final = (
    "frame payload canonicalizes to bytes the OVC1 decode path does not admit, so "
    "no frame is returned for it: an encoder may only produce frames that decode. "
    "A number is the value that does this -- an integer exactly representable in "
    "binary64 can render to a decimal that is not, and 2**53 is the largest "
    "magnitude for which that never happens. Carry a larger integer identity as a "
    "string"
)
"""Why an admissible payload still has no frame. Names the closure rule and the
one category of value that reaches it, never which value or what it rendered
to."""


def _json_kind(value: object) -> str:
    """Name a decoded value's JSON kind, without ever naming its content."""
    if value is None:
        return "JSON null"
    return _JSON_KINDS.get(type(value), "a non-JSON value")


def _canonicalize(value: object) -> tuple[bytes, str]:
    """Return ``value``'s canonical bytes, or ``(b"", reason)`` if it has none.

    Deliberately returns rather than raises. Every failure below is reported as
    a prepared, payload-free sentence and the offending exception is handled
    *here*, so the :class:`~omnivia_core_client.errors.ProtocolError` the caller
    raises afterwards has neither a ``__cause__`` nor a ``__context__`` to
    carry. That matters because Core's ``ContractSemanticError`` messages name
    the offending value on purpose -- they are written for a developer
    canonicalizing a document they own, not for a peer's untrusted bytes -- and
    an exception is the value most likely to be logged.

    ``b""`` is an unambiguous failure marker: no JSON value canonicalizes to
    nothing, the smallest being ``{}``.
    """
    try:
        return canonical_bytes(value), ""
    except ContractSemanticError:
        # Every I-JSON admission rule Core enforces: a non-string or duplicated
        # member name, a lone surrogate, a non-finite or inexact number, a value
        # JSON cannot carry, or nesting past the canonical depth.
        return b"", _NOT_ADMISSIBLE_VALUE
    except RecursionError:
        # Core's depth guard should reach a value before Python's stack does.
        # Kept because this layer is handed untrusted input and must not depend
        # on that ordering holding for every shape.
        return b"", _TOO_DEEP_TO_CANONICALIZE
    except (TypeError, ValueError):
        # `ContractSemanticError` is itself a `ValueError`, so this is only the
        # failures that escape Core's own translation -- rendering an integer of
        # more than 4300 digits raises a bare `ValueError` from the interpreter
        # while Core is still building its diagnostic.
        return b"", _UNRENDERABLE_VALUE


def _parse_rejection(error: ContractSemanticError) -> str:
    """Name a parse failure from the exception's *structure*, never its message.

    ``parse_json_document`` chains the underlying failure it translated, so its
    *type* says which category this was. Only the type and, for a syntax error,
    the offset are read; the chained exception's message and arguments are never
    touched, and ``json.JSONDecodeError`` keeps the whole document on ``.doc``.

    An unchained ``ContractSemanticError`` is one the parser raised itself, from
    the two checks it makes that a later stage could not: a duplicated member
    name, which the parser silently drops, and the non-standard ``NaN`` and
    ``Infinity`` literals. If Core ever stops chaining, every category collapses
    into that last sentence, which stays true and stays payload-free.
    """
    cause = error.__cause__
    if isinstance(cause, json.JSONDecodeError):
        return f"frame JSON is not valid JSON at character offset {cause.pos}"
    if isinstance(cause, RecursionError):
        return _TOO_DEEP_TO_PARSE
    if isinstance(cause, ValueError):
        return _UNPARSEABLE_NUMBER
    return _NOT_ADMISSIBLE_DOCUMENT


def _parse_document(text: str) -> tuple[object, str]:
    """Parse one untrusted JSON document, or return ``(None, reason)``.

    Returns rather than raises for the same reason :func:`_canonicalize` does:
    the exception is handled here, so nothing chains onto what the caller
    raises. ``reason`` is empty exactly when the parse succeeded -- ``None`` is
    a value a document can legitimately hold, so it is not the failure marker.
    """
    try:
        return parse_json_document(text), ""
    except ContractSemanticError as error:
        return None, _parse_rejection(error)
    except RecursionError:
        return None, _TOO_DEEP_TO_PARSE
    except (TypeError, ValueError):
        return None, "frame JSON is not a JSON document this client can parse"


def _decode_utf8(body: bytes) -> tuple[str, str]:
    """Decode a frame body as strict UTF-8, or return ``("", reason)``.

    Done here rather than left to ``parse_json_document``'s own byte handling so
    the diagnostic can name the offending byte offset, and so ``body``'s
    contents never reach a diagnostic: ``UnicodeDecodeError`` keeps the whole
    undecodable buffer in its ``args``.
    """
    try:
        return body.decode("utf-8", "strict"), ""
    except UnicodeDecodeError as error:
        return "", f"frame JSON is not valid UTF-8 at byte offset {error.start}"


def _admit_body(body: bytes) -> tuple[dict[str, Any], str]:
    """Apply the decode-side admission to one frame body.

    The single implementation of "these bytes are an OVC1 frame body", and both
    halves of the module run it: :func:`decode_frame` over what it received,
    :func:`canonical_json_bytes` over what it just produced. One function rather
    than two, so the encoder's closure test cannot drift from the decoder it is
    a test *of* -- a second copy would be a prediction about the decoder, and
    this is the decoder.

    ``reason`` is the discriminator, as in the helpers above: it is empty
    exactly when the returned mapping is the admitted value, and the mapping is
    meaningless otherwise. It cannot be the failure marker itself, because
    ``{}`` is a payload a frame may legitimately carry.
    """
    text, reason = _decode_utf8(body)
    if reason:
        return {}, reason

    value, reason = _parse_document(text)
    if reason:
        return {}, reason

    if not isinstance(value, dict):
        return {}, f"frame payload must be a JSON object, got {_json_kind(value)}"

    canonical, reason = _canonicalize(value)
    if reason:
        return {}, reason
    if canonical != body:
        return {}, _NOT_CANONICAL

    return value, ""


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the exact canonical JSON bytes of one frame payload.

    The body of a frame, without the header: useful on its own for stating a
    vector, hashing a payload, or comparing two encoders. The root must be a
    mapping, because a frame always carries a JSON object.

    Every value inside it is checked recursively against the admitted I-JSON
    domain described in this module's docstring. Nothing is coerced on the way
    through -- an integer member name is a refusal, not a member named ``"1"``.

    The bytes are then put through :func:`_admit_body`, the decode path's own
    admission, and returned only if it accepts them. That is the transport
    closure rule, and it is what makes this function and :func:`encode_frame`
    total with respect to :func:`decode_frame`: what comes back from here always
    decodes. The rejection is deliberately reported as the closure rule rather
    than as whatever the decode path said, because that sentence would describe
    bytes the caller never wrote and could not have written differently.

    A caller who wants Core's canonicalizer without this wire rule -- to
    content-address a document rather than to send one -- should call
    ``omnivia_core.contracts.v1.canonical_json.canonical_bytes`` directly. The
    closure rule belongs to the transport, not to the canonical form.
    """
    if not isinstance(payload, Mapping):
        raise ProtocolError(
            f"frame payload must be a JSON object, got {type(payload).__name__}"
        )
    body, reason = _canonicalize(payload)
    if reason:
        raise ProtocolError(reason)

    _, reason = _admit_body(body)
    if reason:
        raise ProtocolError(_NOT_DECODABLE_ONCE_ENCODED)
    return body


def encode_frame(payload: Mapping[str, Any]) -> bytes:
    """Render one JSON object as a complete OVC1 frame.

    The returned bytes are the whole frame and nothing more: a caller writes
    them as they are, and appends no delimiter. The length field is what
    delimits a frame, so a newline or any other separator after it would be
    trailing data the reader refuses.

    A frame comes back only when :func:`decode_frame` would accept it, because
    :func:`canonical_json_bytes` enforces that before this ever sees a body. A
    caller therefore never holds a frame that cannot be delivered.
    """
    body = canonical_json_bytes(payload)
    length = len(body)
    if length > MAXIMUM_JSON_BYTES:
        raise ProtocolError(
            f"frame JSON is {length} bytes, above the {MAXIMUM_JSON_BYTES}-byte maximum"
        )
    return MAGIC + length.to_bytes(LENGTH_BYTES, "big") + body


def decode_frame(frame: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Decode one complete OVC1 frame into its JSON object.

    ``frame`` must be exactly one frame: a reader that has taken the header,
    then read the declared number of bytes, has exactly that. Anything left
    over is refused rather than ignored -- extra bytes after a frame mean the
    sender and this reader disagree about where the frame ended, and the next
    read would be misaligned in a way no later check would catch.

    Every rejection below is a :class:`~omnivia_core_client.errors.ProtocolError`
    naming a structural fact only, raised with nothing chained onto it: the
    parser and the canonicalizer both put the offending value in their own
    diagnostics, so their exceptions are handled where they occur rather than
    carried out of here as a ``__cause__`` or a ``__context__``.

    The header and the declared length are checked here; the body is checked by
    :func:`_admit_body`, which is also what :func:`canonical_json_bytes` holds
    its own output to.
    """
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise ProtocolError(f"frame must be bytes, got {type(frame).__name__}")
    raw = bytes(frame)

    if len(raw) < HEADER_BYTES:
        raise ProtocolError(
            f"frame is {len(raw)} bytes, too short for the {HEADER_BYTES}-byte OVC1 header"
        )
    if raw[: len(MAGIC)] != MAGIC:
        # The observed bytes are not echoed: on a misaligned stream they are
        # payload, and payload never appears in a diagnostic.
        raise ProtocolError(
            f"frame does not begin with the OVC1 magic (expected {MAGIC_HEX})"
        )

    length = int.from_bytes(raw[len(MAGIC) : HEADER_BYTES], "big")
    if length == 0:
        raise ProtocolError(
            "frame declares a zero-byte JSON payload; every frame carries one JSON object"
        )
    if length > MAXIMUM_JSON_BYTES:
        raise ProtocolError(
            f"frame declares {length} JSON bytes, above the "
            f"{MAXIMUM_JSON_BYTES}-byte maximum"
        )

    body = raw[HEADER_BYTES:]
    if len(body) < length:
        raise ProtocolError(
            f"frame is truncated: it declares {length} JSON bytes and carries {len(body)}"
        )
    if len(body) > length:
        raise ProtocolError(
            f"frame carries {len(body) - length} byte(s) after its declared "
            f"{length} JSON bytes"
        )

    value, reason = _admit_body(body)
    if reason:
        raise ProtocolError(reason)
    return value
