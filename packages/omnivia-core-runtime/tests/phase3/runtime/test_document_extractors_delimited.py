"""The delimited walk: what a field keeps, what a record reports, what is refused.

Every case here is driven through the public ``extract_document`` entry point,
so what is asserted is the adapter's output and not a parser internal. The
bound cases shrink the module-level bound instead of growing the source, which
keeps the fixtures small enough to read and proves the refusal comes from the
bound rather than from anything the source happens to contain.
"""

from __future__ import annotations

import builtins
import socket
import subprocess
from typing import Any, Final

import pytest
from omnivia_core_runtime.service import document_extractors
from omnivia_core_runtime.service.document_extractors import (
    DocumentExtractionInvalid,
    extract_document,
)
from omnivia_core_runtime.service.document_intake import ExtractionOutcome

CAPTURE: Final = "dcap-" + "c" * 64
CSV: Final = "text/csv"
TSV: Final = "text/tab-separated-values"
#: DOC-08's spreadsheet payloads. Every one is a string the source wrote, and
#: none of them is a thing to evaluate, resolve or run.
FORMULA_LIKE: Final = (
    b"=1+1\n"
    b"=cmd|'/c calc'!A0\n"
    b"+SUM(A1)\n"
    b"-2+3\n"
    b"@SUM(1)\n"
    b'"=HYPERLINK(""http://x.invalid"",""c"")"\n'
)


def delimited(source: bytes, media_type: str = CSV) -> ExtractionOutcome:
    return extract_document(source, media_type=media_type, capture_id=CAPTURE)


def values(result: ExtractionOutcome) -> list[str]:
    return [item.structured_value["raw"] for item in result.segments]


def locators(result: ExtractionOutcome) -> list[dict[str, Any]]:
    return [dict(item.source_locator) for item in result.segments]


def test_the_delimiter_comes_from_the_media_type_and_is_never_sniffed() -> None:
    source = b"a,b\tc;d|e\n"

    # The same bytes read one way as CSV and another as TSV, and in both cases
    # the delimiter is the admitted media type's and not the character the
    # source happens to use most.
    assert values(delimited(source, CSV)) == ["a", "b\tc;d|e"]
    assert values(delimited(source, TSV)) == ["a,b", "c;d|e"]
    assert delimited(source, CSV).limitations["delimiterSource"] == "media_type"


def test_the_first_record_is_data_whatever_its_labels_look_like() -> None:
    result = delimited(b'id,id,,"id"\n1,2,3,4\n')

    # Duplicate and empty labels are values, not a header to repair: nothing is
    # deduplicated, renamed, filled in or deleted.
    assert values(result) == ["id", "id", "", "id", "1", "2", "3", "4"]
    assert result.limitations["headerTreatment"] == "preserved_as_record"
    assert result.limitations["records"] == 2
    assert result.coverage["units"][0]["unitType"] == "record"
    assert result.coverage["units"][0]["expected"] == 2


def test_field_text_is_carried_through_verbatim() -> None:
    result = delimited(
        b'007,0.50, padded ,"two\nlines","say ""hi""",\xc3\xa9t\xc3\xa9,\n'
    )

    # A leading zero survives, a quoted newline stays inside its field, an
    # escaped quote becomes one quote, padding is not stripped and the empty
    # trailing field after the last delimiter is a field.
    assert values(result) == [
        "007",
        "0.50",
        " padded ",
        "two\nlines",
        'say "hi"',
        "été",
        "",
    ]


def test_locators_name_the_record_the_column_and_the_physical_lines() -> None:
    result = delimited(b'a,b\r\n"x\ny",z\r\n')

    # The record ordinal counts records, the physical lines count lines, and a
    # quoted newline is what makes the two disagree.
    assert locators(result) == [
        {
            "captureId": CAPTURE,
            "unit": "record",
            "record": 1,
            "column": 1,
            "fieldCount": 2,
            "physicalLineStart": 1,
            "physicalLineEnd": 1,
        },
        {
            "captureId": CAPTURE,
            "unit": "record",
            "record": 1,
            "column": 2,
            "fieldCount": 2,
            "physicalLineStart": 1,
            "physicalLineEnd": 1,
        },
        {
            "captureId": CAPTURE,
            "unit": "record",
            "record": 2,
            "column": 1,
            "fieldCount": 2,
            "physicalLineStart": 2,
            "physicalLineEnd": 3,
        },
        {
            "captureId": CAPTURE,
            "unit": "record",
            "record": 2,
            "column": 2,
            "fieldCount": 2,
            "physicalLineStart": 2,
            "physicalLineEnd": 3,
        },
    ]


def test_ragged_records_keep_their_own_field_counts_and_are_warned_about() -> None:
    result = delimited(b"a,b,c\n1,2\n3,4,5,6\n")

    assert values(result) == ["a", "b", "c", "1", "2", "3", "4", "5", "6"]
    # Every locator carries the count of the record it came from, so a short or
    # long record is readable as such without padding or trimming it.
    assert [
        (item["record"], item["column"], item["fieldCount"])
        for item in locators(result)
    ] == [
        (1, 1, 3),
        (1, 2, 3),
        (1, 3, 3),
        (2, 1, 2),
        (2, 2, 2),
        (3, 1, 4),
        (3, 2, 4),
        (3, 3, 4),
        (3, 4, 4),
    ]
    assert result.limitations["fieldCountBaseline"] == 3
    assert result.limitations["raggedRecords"] == 2
    assert result.limitations["warnings"] == ["ragged_records"]
    assert result.state == "succeeded"


def test_a_blank_line_is_a_record_with_no_fields_and_renumbers_nothing() -> None:
    result = delimited(b"a\n\nb\n")

    # The blank line keeps its ordinal, so the record after it is the third
    # record and not the second: no record is deleted to tidy the numbering.
    assert [(item["record"], item["column"]) for item in locators(result)] == [
        (1, 1),
        (3, 1),
    ]
    assert values(result) == ["a", "b"]
    assert result.limitations["blankRecords"] == 1
    assert result.limitations["raggedRecords"] == 0
    assert result.limitations["warnings"] == ["blank_records"]
    # A one-field record is not ragged against a blank one: the baseline is the
    # first record that has a field.
    assert result.limitations["fieldCountBaseline"] == 1


def test_an_empty_source_is_a_known_empty_success() -> None:
    result = delimited(b"")

    assert result.state == "succeeded" and result.segments == ()
    assert result.limitations["records"] == 0
    assert result.limitations["warnings"] == []
    assert result.coverage["units"][0]["expected"] == 0


@pytest.mark.parametrize(
    ("source", "encoding"),
    [
        (b"a,b\n1,2\n", "utf-8"),
        (b"\xef\xbb\xbfa,b\n1,2\n", "utf-8-bom"),
        (b"\xff\xfe" + "a,b\n1,2\n".encode("utf-16-le"), "utf-16-le-bom"),
        (b"\xfe\xff" + "a,b\n1,2\n".encode("utf-16-be"), "utf-16-be-bom"),
    ],
)
def test_declared_encodings_read_the_same_fields_and_consume_the_mark(
    source: bytes, encoding: str
) -> None:
    result = delimited(source)

    # The declared mark is not left in the first field, so the first label is
    # "a" in all four encodings and the digest is the same output identity.
    assert values(result) == ["a", "b", "1", "2"]
    assert result.limitations["encoding"] == encoding


# --------------------------------------------------------------------------- #
# DOC-08: adversarial delimited sources
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (b"a,\xffb\n", "unsupported encoding"),
        # UTF-16 with no mark to declare it: valid UTF-8 that is not text.
        ("a,b\n".encode("utf-16-le"), "NUL characters"),
        ("a,b\n".encode("utf-16-be"), "NUL characters"),
        (b"\xff\xfe\x00\x00a\x00\x00\x00", "NUL characters"),
        (b"\xff\xfea", "unsupported encoding"),
        # A high surrogate with no low surrogate to pair with.
        (b"\xff\xfe\x00\xd8", "unsupported encoding"),
        (b"a,\xed\xa0\x80\n", "unsupported encoding"),
    ],
)
def test_undeclared_and_malformed_encodings_are_refused(
    source: bytes, reason: str
) -> None:
    with pytest.raises(DocumentExtractionInvalid, match=reason):
        delimited(source)


@pytest.mark.parametrize(
    "source",
    [
        # A character after a closing quote: the record's shape is ambiguous.
        b'"a"b,c\n',
        # A quoted field the source never terminated, which would otherwise
        # swallow every remaining record into one field.
        b'a,"b,c\nd,e\n',
        b'a,"\n',
    ],
)
def test_mis_terminated_quoting_is_refused_rather_than_guessed(source: bytes) -> None:
    with pytest.raises(DocumentExtractionInvalid, match="malformed"):
        delimited(source)


def test_a_quote_inside_an_unquoted_field_stays_literal_text() -> None:
    # The field never claimed to be quoted, so its quote is a character and not
    # a mis-terminated field: the bytes are kept rather than refused.
    assert values(delimited(b'a"b,5"\n')) == ['a"b', '5"']


def test_formula_like_strings_are_values_and_nothing_is_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*arguments: object, **keywords: object) -> Any:
        raise AssertionError("the delimited walk must take no external action")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(builtins, "eval", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)

    result = delimited(FORMULA_LIKE)

    # Each payload is published exactly as written: not evaluated, not
    # resolved, not run, and not escaped into something else.
    assert values(result) == [
        "=1+1",
        "=cmd|'/c calc'!A0",
        "+SUM(A1)",
        "-2+3",
        "@SUM(1)",
        '=HYPERLINK("http://x.invalid","c")',
    ]
    assert all(item.semantic_role == "table_cell" for item in result.segments)


@pytest.mark.parametrize(
    ("bound", "value", "source", "reason"),
    [
        ("MAX_RECORDS", 2, b"a\nb\nc\n", "too many records"),
        ("MAX_COLUMNS", 2, b"a,b,c\n", "too many columns"),
        ("MAX_FIELDS", 3, b"a,b\nc,d\n", "too many fields"),
        ("MAX_FIELD_BYTES", 4, b"abcde,b\n", "field exceeds"),
        ("MAX_DELIMITED_CHARS", 4, b"a,b,c\n", "text exceeds"),
    ],
)
def test_delimited_bounds_refuse_the_source_rather_than_truncate_it(
    bound: str,
    value: int,
    source: bytes,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(document_extractors, bound, value)

    with pytest.raises(DocumentExtractionInvalid, match=reason):
        delimited(source)

    # The same bytes are accepted at the packaged bound, so the refusal is the
    # bound speaking and not a defect in the fixture.
    monkeypatch.undo()
    assert delimited(source).state == "succeeded"


def test_the_packaged_bounds_are_the_ones_the_slice_was_qualified_at() -> None:
    assert (
        document_extractors.MAX_RECORDS,
        document_extractors.MAX_COLUMNS,
        document_extractors.MAX_FIELD_BYTES,
    ) == (250_000, 1_024, 1_048_576)


def test_a_field_at_the_bound_is_accepted_and_one_byte_over_is_refused() -> None:
    at_bound = b"x" * document_extractors.MAX_FIELD_BYTES

    assert values(delimited(at_bound + b"\n")) == [at_bound.decode()]
    with pytest.raises(DocumentExtractionInvalid):
        delimited(at_bound + b"x\n")


def test_a_multi_byte_field_is_bounded_by_its_decoded_bytes() -> None:
    # Half a mebibyte of two-byte characters is under the bound in characters
    # and over it in bytes, which is the unit the bound is stated in.
    over = "é" * (document_extractors.MAX_FIELD_BYTES // 2 + 1)

    with pytest.raises(DocumentExtractionInvalid, match="field exceeds"):
        delimited(over.encode() + b"\n")
