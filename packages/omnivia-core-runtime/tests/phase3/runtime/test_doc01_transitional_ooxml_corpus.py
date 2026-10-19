"""DOC-01, Transitional slice: Microsoft Office packages and compatible-converter outputs.

Each pair is an unmodified Open XML SDK test asset (Microsoft Office-authored) and
the file ONLYOFFICE's x2t rewrote from it. The receipt beside the fixtures binds the
hashes and the conversion. The converter preserved the Microsoft ``docProps/app.xml``,
so that metadata is asserted on the inputs only and is never offered as proof of the
outputs' authorship.
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
RECEIPT = json.loads((FIXTURES / "doc01-transitional-ooxml-receipt.json").read_text())
PAIRS: dict[str, dict[str, Any]] = {row["format"]: row for row in RECEIPT["pairs"]}
TRANSITIONAL = b"http://schemas.openxmlformats.org/"
STRICT = b"http://purl.oclc.org/ooxml/"
OFFICE_DOCUMENT = b"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
EXTRACTORS = {"docx": extract_docx, "xlsx": extract_xlsx, "pptx": extract_pptx}
FILES = [(fmt, side) for fmt in sorted(PAIRS) for side in ("input", "output")]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(fmt: str, side: str) -> bytes:
    return (FIXTURES / PAIRS[fmt][side]["file"]).read_bytes()


def app_xml(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        return package.read("docProps/app.xml")


def test_receipt_pins_the_upstream_revision_licence_and_converter() -> None:
    assert set(PAIRS) == {"docx", "xlsx", "pptx"}
    assert RECEIPT["upstream"]["revision"] == "431ab05cf160248cc3885a4a766026d4f8243792"
    strict = json.loads((FIXTURES / "doc01-strict-ooxml-provenance.json").read_text())
    assert RECEIPT["upstream"]["revision"] == strict["upstream"]["revision"]
    assert RECEIPT["upstream"]["licence_file"] == strict["upstream"]["licence_file"]
    assert (
        (FIXTURES / RECEIPT["upstream"]["licence_file"])
        .read_bytes()
        .startswith(b"\xef\xbb\xbfThe MIT License (MIT)")
    )
    for row in PAIRS.values():
        assert row["input"]["upstream_url"].startswith(
            "https://github.com/dotnet/Open-XML-SDK/blob/"
            + RECEIPT["upstream"]["revision"]
            + "/test/DocumentFormat.OpenXml.Tests.Assets/assets/TestFiles/"
        )
    assert RECEIPT["converter"]["version"] == "9.4.0"
    assert RECEIPT["converter"]["build"] == "563"
    assert re.fullmatch(r"[0-9a-f]{64}", RECEIPT["converter"]["converter_sha256"])


@pytest.mark.parametrize(("fmt", "side"), FILES)
def test_fixture_bytes_match_the_receipt(fmt: str, side: str) -> None:
    assert sha256(read(fmt, side)) == PAIRS[fmt][side]["sha256"]


@pytest.mark.parametrize(("fmt", "side"), FILES)
def test_package_is_transitional_ooxml(fmt: str, side: str) -> None:
    with zipfile.ZipFile(FIXTURES / PAIRS[fmt][side]["file"]) as package:
        rels = package.read("_rels/.rels")
        assert OFFICE_DOCUMENT in rels
        assert STRICT not in rels
        main = package.read(PAIRS[fmt]["main_part"])
        assert TRANSITIONAL in main
        assert STRICT not in main
        offenders = [
            name
            for name in package.namelist()
            if name.endswith((".xml", ".rels")) and STRICT in package.read(name)
        ]
        assert offenders == []


@pytest.mark.parametrize("fmt", sorted(PAIRS))
def test_inputs_carry_microsoft_office_metadata(fmt: str) -> None:
    metadata = PAIRS[fmt]["input"]["app_xml"]
    xml = app_xml(read(fmt, "input")).decode("utf-8")
    for tag, value in metadata.items():
        assert f"<{tag}>{value}</{tag}>" in xml


@pytest.mark.parametrize("fmt", sorted(PAIRS))
def test_converter_preserved_the_microsoft_metadata_so_it_proves_nothing(
    fmt: str,
) -> None:
    # The output still names Microsoft: the metadata survived the conversion, so it
    # cannot distinguish a Microsoft-authored file from a converter-rewritten one.
    assert app_xml(read(fmt, "output")) == app_xml(read(fmt, "input"))
    assert "ONLYOFFICE" not in app_xml(read(fmt, "output")).decode("utf-8")
    assert "metadata_caveat" in RECEIPT


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
