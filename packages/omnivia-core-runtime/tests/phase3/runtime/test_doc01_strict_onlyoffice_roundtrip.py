"""DOC-01, Strict slice: compatible-application round trip.

Each pair is a committed Microsoft Office Strict package (see the Strict provenance
manifest) and the file ONLYOFFICE's x2t rewrote from it. The receipt beside the
fixtures binds the hashes and the conversion. The converter preserved the Microsoft
``docProps/app.xml``, so that metadata is never offered as proof of the outputs'
authorship; the hash-pair receipt is.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.service.ooxml_extractors import (
    extract_docx,
    extract_pptx,
    extract_xlsx,
)

FIXTURES = Path(__file__).resolve().parents[5] / "qualification/wp15/fixtures"
RECEIPT = json.loads((FIXTURES / "doc01-strict-onlyoffice-receipt.json").read_text())
PROVENANCE = json.loads((FIXTURES / "doc01-strict-ooxml-provenance.json").read_text())
PAIRS: dict[str, dict[str, Any]] = {row["format"]: row for row in RECEIPT["pairs"]}
STRICT = b"http://purl.oclc.org/ooxml/"
OFFICE_DOCUMENT = STRICT + b"officeDocument/relationships/officeDocument"
# Any Transitional main-vocabulary namespace; the package-level and
# markup-compatibility namespaces are shared by both families.
TRANSITIONAL = re.compile(
    rb"schemas\.openxmlformats\.org/(?:wordprocessingml|spreadsheetml|"
    rb"presentationml|drawingml|officeDocument)/2006"
)
EXTRACTORS = {"docx": extract_docx, "xlsx": extract_xlsx, "pptx": extract_pptx}
FILES = [(fmt, side) for fmt in sorted(PAIRS) for side in ("input", "output")]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(fmt: str, side: str) -> bytes:
    return (FIXTURES / PAIRS[fmt][side]["file"]).read_bytes()


def app_xml(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        return package.read("docProps/app.xml")


def test_receipt_binds_the_strict_provenance_and_the_converter() -> None:
    assert set(PAIRS) == {"docx", "xlsx", "pptx"}
    assert RECEIPT["strict_provenance"] == "doc01-strict-ooxml-provenance.json"
    strict_inputs = {row["format"]: row for row in PROVENANCE["fixtures"]}
    for fmt, row in PAIRS.items():
        assert row["input"]["file"] == strict_inputs[fmt]["file"]
        assert row["input"]["sha256"] == strict_inputs[fmt]["sha256"]
    converter = RECEIPT["converter"]
    assert converter["application"] == "ONLYOFFICE Desktop Editors"
    assert converter["version"] == "9.4.0"
    assert converter["build"] == "563"
    assert converter["converter"] == "x2t"
    assert re.fullmatch(r"[0-9a-f]{64}", converter["converter_sha256"])
    assert converter["command_shape"].startswith("x2t <input.ext> <output.ext>")
    assert "metadata_caveat" in RECEIPT


@pytest.mark.parametrize(("fmt", "side"), FILES)
def test_fixture_bytes_match_the_receipt(fmt: str, side: str) -> None:
    assert sha256(read(fmt, side)) == PAIRS[fmt][side]["sha256"]


@pytest.mark.parametrize(("fmt", "side"), FILES)
def test_package_is_strict_ooxml(fmt: str, side: str) -> None:
    with zipfile.ZipFile(FIXTURES / PAIRS[fmt][side]["file"]) as package:
        assert OFFICE_DOCUMENT in package.read("_rels/.rels")
        assert STRICT in package.read(PAIRS[fmt]["main_part"])
        offenders = [
            name
            for name in package.namelist()
            if name.endswith((".xml", ".rels"))
            and TRANSITIONAL.search(package.read(name))
        ]
        assert offenders == []


@pytest.mark.parametrize("fmt", sorted(PAIRS))
def test_converter_preserved_the_microsoft_metadata_so_it_proves_nothing(
    fmt: str,
) -> None:
    # The output still names Microsoft: the metadata survived the conversion, so it
    # cannot distinguish a Microsoft-authored file from a converter-rewritten one.
    assert app_xml(read(fmt, "output")) == app_xml(read(fmt, "input"))
    assert "ONLYOFFICE" not in app_xml(read(fmt, "output")).decode("utf-8")


@pytest.mark.parametrize("fmt", sorted(PAIRS))
def test_output_bytes_differ_from_the_paired_input(fmt: str) -> None:
    assert read(fmt, "output") != read(fmt, "input")
    assert PAIRS[fmt]["output"]["sha256"] != PAIRS[fmt]["input"]["sha256"]


@pytest.mark.parametrize(("fmt", "side"), FILES)
def test_extraction_is_deterministic_and_leaves_the_bytes_identical(
    fmt: str, side: str
) -> None:
    path = FIXTURES / PAIRS[fmt][side]["file"]
    before = path.read_bytes()
    expected = PAIRS[fmt][side]["sha256"]
    assert sha256(before) == expected

    first = EXTRACTORS[fmt](before, capture_id="doc01")
    second = EXTRACTORS[fmt](before, capture_id="doc01")

    assert first == second
    assert first.unit_type == PAIRS[fmt]["expected"]["unit_type"]
    assert first.failed_units == 0
    assert first.limit_breach is None
    assert before == path.read_bytes()
    assert sha256(before) == expected
    assert sha256(path.read_bytes()) == expected


@pytest.mark.parametrize("fmt", sorted(PAIRS))
def test_extraction_is_exactly_equal_before_and_after_conversion(fmt: str) -> None:
    expected = PAIRS[fmt]["expected"]
    before = EXTRACTORS[fmt](read(fmt, "input"), capture_id="doc01")
    after = EXTRACTORS[fmt](read(fmt, "output"), capture_id="doc01")

    assert before == after
    assert len(before.segments) == expected["segments"]
    if "texts" in expected:
        assert [s.text for s in before.segments] == expected["texts"]
    if "sheets" in expected:
        assert before.limitations["sheets"] == expected["sheets"]
    if "first_body_text" in expected:
        assert before.segments[0].semantic_role == "body"
        assert before.segments[0].text == expected["first_body_text"]
        assert expected["comment_text"] in {s.text for s in before.segments}
