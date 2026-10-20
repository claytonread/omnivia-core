"""DOC-01, Strict slice: real Microsoft Office Strict OOXML packages.

The three fixtures are unmodified Open XML SDK test assets (see the provenance
manifest beside them). This proves only the Microsoft Office Strict-family slice;
the Transitional multi-application corpus and the before/after round trip are
not covered here.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest
from omnivia_core_runtime.service.ooxml_extractors import (
    extract_docx,
    extract_pptx,
    extract_xlsx,
)

FIXTURES = Path(__file__).resolve().parents[5] / "qualification/wp15/fixtures"
MANIFEST = json.loads((FIXTURES / "doc01-strict-ooxml-provenance.json").read_text())
STRICT = "http://purl.oclc.org/ooxml/"
# Any Transitional main-vocabulary namespace; the package-level and
# markup-compatibility namespaces are shared by both families.
TRANSITIONAL = re.compile(
    rb"schemas\.openxmlformats\.org/(?:wordprocessingml|spreadsheetml|"
    rb"presentationml|drawingml|officeDocument)/2006"
)
EXTRACTORS = {"docx": extract_docx, "xlsx": extract_xlsx, "pptx": extract_pptx}
FIXTURE_ROWS = {row["format"]: row for row in MANIFEST["fixtures"]}


def test_manifest_pins_the_three_formats_and_the_upstream_revision() -> None:
    assert set(FIXTURE_ROWS) == {"docx", "xlsx", "pptx"}
    assert (
        MANIFEST["upstream"]["revision"] == "431ab05cf160248cc3885a4a766026d4f8243792"
    )
    assert (
        (FIXTURES / MANIFEST["upstream"]["licence_file"])
        .read_bytes()
        .startswith(b"\xef\xbb\xbfThe MIT License (MIT)")
    )


@pytest.mark.parametrize("fmt", sorted(FIXTURE_ROWS))
def test_fixture_is_strict_namespace_ooxml(fmt: str) -> None:
    with zipfile.ZipFile(FIXTURES / FIXTURE_ROWS[fmt]["file"]) as package:
        rels = package.read("_rels/.rels")
        assert STRICT.encode() + b"officeDocument/relationships/officeDocument" in rels
        parts = [n for n in package.namelist() if n.endswith((".xml", ".rels"))]
        offenders = [n for n in parts if TRANSITIONAL.search(package.read(n))]
        assert offenders == []
        main = {
            "docx": "word/document.xml",
            "xlsx": "xl/workbook.xml",
            "pptx": "ppt/presentation.xml",
        }[fmt]
        assert STRICT.encode() in package.read(main)


@pytest.mark.parametrize("fmt", sorted(FIXTURE_ROWS))
def test_extraction_is_stable_and_leaves_the_source_bytes_identical(fmt: str) -> None:
    path = FIXTURES / FIXTURE_ROWS[fmt]["file"]
    before = path.read_bytes()
    assert hashlib.sha256(before).hexdigest() == FIXTURE_ROWS[fmt]["sha256"]

    first = EXTRACTORS[fmt](before, capture_id="doc01")
    second = EXTRACTORS[fmt](before, capture_id="doc01")

    assert first == second
    assert first.segments
    assert first.failed_units == 0
    assert first.limit_breach is None
    assert hashlib.sha256(before).hexdigest() == FIXTURE_ROWS[fmt]["sha256"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == FIXTURE_ROWS[fmt]["sha256"]


def test_strict_docx_extracts_body_and_comment() -> None:
    result = extract_docx(
        (FIXTURES / "doc01-strict-word.docx").read_bytes(), capture_id="doc01"
    )
    assert result.unit_type == "document_block"
    assert len(result.segments) == 6
    assert result.segments[0].semantic_role == "body"
    assert result.segments[0].text == "This is some text."
    assert "some test comment" in {s.text for s in result.segments}


def test_strict_xlsx_extracts_cells_from_three_sheets() -> None:
    result = extract_xlsx(
        (FIXTURES / "doc01-strict-excel.xlsx").read_bytes(), capture_id="doc01"
    )
    assert result.unit_type == "workbook_cell"
    assert result.limitations["sheets"] == 3
    assert len(result.segments) == 212
    first = result.segments[0]
    assert first.source_locator["cell"] == "A1"
    assert first.structured_value is not None
    assert first.structured_value["decoded"] == "Internet Sales Amount"


def test_strict_pptx_extracts_slide_text() -> None:
    result = extract_pptx(
        (FIXTURES / "doc01-strict-powerpoint.pptx").read_bytes(), capture_id="doc01"
    )
    assert result.unit_type == "slide"
    assert [s.text for s in result.segments] == ["Strikethrough"]
