"""Edge-of-the-bound coverage for the RTF lexical scanner.

``test_document_extractors.py`` covers what the scan reports for a well-formed
source and that a malformed one is refused before the reader runs. This module
covers the cases either side of each bound: the largest input the scan must
still accept, the smallest one it must refuse, and the control words whose
parameter is absent or signed rather than merely out of range.

It lives beside that module rather than inside it so the scanner stays
verifiable on a host with no local OCR engine, which that module's fixtures
import at collection time.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest
from omnivia_core_runtime.service import rtf_extractor
from omnivia_core_runtime.service.rtf_extractor import (
    MAX_CONTROL_WORD_LETTERS,
    MAX_GROUP_DEPTH,
    MAX_PARAMETER_DIGITS,
    MAX_SOURCE_BYTES,
    RTFExtractionInvalid,
    extract_rtf,
)

CAPTURE = "dcap-" + "a" * 64
PANDOC_ARGUMENTS = ["pandoc", "--from=rtf", "--to=json", "--sandbox"]
FIXED_AST = json.dumps(
    {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [{"t": "Para", "c": [{"t": "Str", "c": "Body"}]}],
    }
).encode()


@pytest.fixture
def reader() -> Iterator[list[tuple[Any, dict[str, Any]]]]:
    """Fix the pinned reader's result and record how it was called.

    Every test here is about the scan, which runs first, so the reader returns
    the same AST for every source and the fixture is the record of whether it
    ran at all.
    """
    calls: list[tuple[Any, dict[str, Any]]] = []
    original = subprocess.run

    def run(arguments: Any, **options: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((arguments, options))
        return subprocess.CompletedProcess(arguments, 0, FIXED_AST, b"")

    rtf_extractor.subprocess.run = run  # type: ignore[assignment]
    try:
        yield calls
    finally:
        rtf_extractor.subprocess.run = original  # type: ignore[assignment]


def scan(content: bytes) -> dict[str, object]:
    return dict(extract_rtf(content, capture_id=CAPTURE).limitations)


def test_the_group_depth_bound_is_reachable_and_exclusive(
    reader: list[tuple[Any, dict[str, Any]]],
) -> None:
    def nested(depth: int) -> bytes:
        return rb"{\rtf1\ansi " + b"{" * depth + b"x" + b"}" * depth + b"}"

    # The root group is depth one, so the bound is reached with one less nesting.
    accepted = scan(nested(MAX_GROUP_DEPTH - 1))
    assert accepted["maximumGroupDepth"] == MAX_GROUP_DEPTH
    assert accepted["groups"] == MAX_GROUP_DEPTH

    with pytest.raises(RTFExtractionInvalid, match="nesting exceeds"):
        scan(nested(MAX_GROUP_DEPTH))


def test_a_binary_section_without_a_declared_length_is_refused(
    reader: list[tuple[Any, dict[str, Any]]],
) -> None:
    # \bin with no parameter has no payload end. Reading it as a zero-length
    # section accepts the source and then reads the payload's own braces and
    # backslashes as document structure, which is the case this refuses.
    with pytest.raises(RTFExtractionInvalid, match="binary section is malformed"):
        scan(rb"{\rtf1\ansi{\*\objdata \bin {\}\\}}")
    with pytest.raises(RTFExtractionInvalid, match="binary section is malformed"):
        scan(rb"{\rtf1\ansi{\*\objdata \bin}}")

    # An explicit length is what makes the same payload safe to skip.
    payload = b"{\\}\\{}"
    declared = scan(rb"{\rtf1\ansi{\*\objdata \bin%d %s}}" % (len(payload), payload))
    assert declared["binarySections"] == 1
    assert declared["binaryBytes"] == len(payload)
    assert declared["groups"] == 2
    assert declared["escapedLiterals"] == 0
    assert not reader[0][1]["input"].startswith(b"x")  # the source reached the reader


def test_the_unicode_fallback_run_follows_the_scoped_uc_count(
    reader: list[tuple[Any, dict[str, Any]]],
) -> None:
    # Each fixture carries the same two escaped literals after one \u. Whether
    # they are the character's fallback run or text of their own is decided
    # entirely by \uc, so the literal count is the observable.
    assert scan(rb"{\rtf1\ansi\uc2\u233 \\\{ tail}")["escapedLiterals"] == 0
    assert scan(rb"{\rtf1\ansi\uc0\u233 \\\{ tail}")["escapedLiterals"] == 2
    # A negative count is not a reverse skip; it floors at no fallback at all.
    assert scan(rb"{\rtf1\ansi\uc-5\u233 \\\{ tail}")["escapedLiterals"] == 2
    # A signed code point is the same character spelled as a signed 16-bit value.
    assert scan(rb"{\rtf1\ansi\uc2\u-3913 \\\{ tail}")["escapedLiterals"] == 0
    # The count is scoped: the inner group inherits it, and the outer one keeps
    # its own after the inner group closes.
    assert (
        scan(rb"{\rtf1\ansi\uc2{\uc0\u233 \\\{}\u233 \\\{ tail}")["escapedLiterals"]
        == 2
    )
    # A group boundary ends a fallback run early, as a reader honouring the
    # character would also stop there.
    assert scan(rb"{\rtf1\ansi\uc9\u233 a{\b \\x}}")["escapedLiterals"] == 1

    with pytest.raises(RTFExtractionInvalid, match="ends in an escape"):
        scan(b"{\\rtf1\\ansi\\uc2\\u65 a\\")


def test_control_word_and_parameter_bounds_are_reachable_before_they_refuse(
    reader: list[tuple[Any, dict[str, Any]]],
) -> None:
    longest = b"a" * MAX_CONTROL_WORD_LETTERS
    assert scan(rb"{\rtf1\ansi\%s x}" % longest)["groups"] == 1
    with pytest.raises(RTFExtractionInvalid, match="control word exceeds"):
        scan(rb"{\rtf1\ansi\%sa x}" % longest)

    widest = b"9" * MAX_PARAMETER_DIGITS
    assert scan(rb"{\rtf1\ansi\f%s x}" % widest)["groups"] == 1
    assert scan(rb"{\rtf1\ansi\f-%s x}" % widest)["groups"] == 1
    # A digit run past the bound is refused as a malformed source. Past about
    # 4300 digits the integer conversion itself fails, and that must not be the
    # error that escapes: it is neither this exception type nor this message.
    for digits in (MAX_PARAMETER_DIGITS + 1, 5_000):
        with pytest.raises(RTFExtractionInvalid, match="parameter exceeds"):
            scan(rb"{\rtf1\ansi\f%s x}" % (b"9" * digits))
        with pytest.raises(RTFExtractionInvalid, match="parameter exceeds"):
            scan(rb"{\rtf1\ansi\bin%s x}" % (b"9" * digits))


def test_only_the_field_keyword_is_read_out_of_an_instruction(
    reader: list[tuple[Any, dict[str, Any]]],
) -> None:
    operand = b"https://attacker.example/steal?token=" + b"q" * 400
    risky = scan(
        rb"{\rtf1\ansi{\field{\*\fldinst HYPERLINK \"%s\"}{\fldrslt r}}}" % operand
    )

    # The operand is far longer than the prefix the scan keeps, and the keyword
    # is still recognised: the kept budget is spent on the keyword alone.
    assert risky["fieldRisks"] == {"HYPERLINK": 1}
    assert "attacker" not in json.dumps(risky)
    assert "token" not in json.dumps(risky)

    # An unrecognised keyword is reported by kind, never by its own text, so a
    # document cannot put a chosen string into the inventory through a field.
    unknown = scan(rb"{\rtf1\ansi{\*\fldinst MERGEFIELD Customer.Name}}")
    assert unknown["fieldRisks"] == {"other": 1}
    assert "MERGEFIELD" not in json.dumps(unknown)
    overlong = scan(rb"{\rtf1\ansi{\*\fldinst %s arg}}" % (b"X" * 200))
    assert overlong["fieldRisks"] == {"other": 1}

    # One process ran, the pinned reader, and it was fed the bytes on stdin.
    assert {tuple(arguments) for arguments, _ in reader} == {tuple(PANDOC_ARGUMENTS)}


def test_the_kept_field_prefix_holds_no_operand_bytes() -> None:
    """The operand is not read at all, rather than read and then withheld.

    Classifying the keyword only needs the keyword. Asserted here rather than
    through the inventory because the two are indistinguishable in the result:
    an operand buffered and dropped reports the same keyword as one never read.
    """
    group = rtf_extractor._Group(uc=1, ignorable=True, field_prefix=bytearray())

    for byte in b'  HYPERLINK  "https://attacker.example/steal?token=abc"':
        rtf_extractor._keep_field_byte(group, byte)

    assert bytes(group.field_prefix or b"") == b"HYPERLINK "


def test_the_reader_invocation_is_pinned_in_full(
    reader: list[tuple[Any, dict[str, Any]]],
) -> None:
    source = rb"{\rtf1\ansi Hello}"

    extraction = extract_rtf(source, capture_id=CAPTURE)

    arguments, options = reader[0]
    assert arguments == PANDOC_ARGUMENTS
    # The source is passed on stdin, never as a path, and the call cannot wait
    # or succeed on a non-zero exit without the caller seeing it.
    assert options["input"] is source
    assert options["capture_output"] is True
    assert options["timeout"] == 120
    assert options["check"] is False
    assert set(options) == {"input", "capture_output", "timeout", "check"}
    assert extraction.blocks == 1
    assert [segment.text for segment in extraction.segments] == ["Body"]


def test_the_source_size_bound_is_reachable_and_checked_first(
    reader: list[tuple[Any, dict[str, Any]]],
) -> None:
    opening = rb"{\rtf1\ansi "
    filler = MAX_SOURCE_BYTES - len(opening) - 1

    assert scan(opening + b"a" * filler + b"}")["groups"] == 1
    with pytest.raises(RTFExtractionInvalid, match="source exceeds"):
        scan(opening + b"a" * (filler + 1) + b"}")
    # The size check precedes the signature check, so an oversize source is
    # never scanned to find out whether it was RTF at all.
    with pytest.raises(RTFExtractionInvalid, match="source exceeds"):
        scan(b"a" * (MAX_SOURCE_BYTES + 1))
    assert len(reader) == 1
