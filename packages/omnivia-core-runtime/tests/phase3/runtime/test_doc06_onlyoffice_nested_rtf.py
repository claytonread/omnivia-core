"""DOC-06, one application slice: an ONLYOFFICE-produced RTF with real nested tables.

ONLYOFFICE Desktop Editors converted a generated DOCX to the RTF fixture (see the
receipt beside it); it did not author the DOCX. This proves one application's
nested-table RTF through the pinned reader and the production ``extract_rtf``
path. It is not the full external RTF corpus.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from _trusted_ocr import HOST_PANDOC, reader_settings
from omnivia_core_runtime.service.rtf_extractor import extract_rtf

FIXTURES = Path(__file__).resolve().parents[5] / "qualification/wp15/fixtures"
RECEIPT = json.loads((FIXTURES / "doc06-onlyoffice-nested-receipt.json").read_text())
RTF = (FIXTURES / RECEIPT["rtf_fixture"]["file"]).read_bytes()
EXPECTED = RECEIPT["extraction"]["expected"]


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def pandoc_3_12_1() -> str | None:
    if HOST_PANDOC is None:
        return None
    first = subprocess.run(
        [HOST_PANDOC, "--version"], capture_output=True, text=True, check=False
    ).stdout.splitlines()[:1]
    return HOST_PANDOC if first == ["pandoc 3.12.1"] else None


PANDOC = pandoc_3_12_1()
needs_pinned_reader = pytest.mark.skipif(
    PANDOC is None, reason="no host Pandoc 3.12.1 to bind"
)


def extract() -> Any:
    return extract_rtf(
        RTF,
        capture_id=RECEIPT["extraction"]["capture_id"],
        settings=reader_settings(pandoc_path=PANDOC),
    )


def test_receipt_binds_both_fixtures_the_converter_and_the_reader() -> None:
    source = FIXTURES / RECEIPT["source_fixture"]["file"]
    assert sha256(source.read_bytes()) == RECEIPT["source_fixture"]["sha256"]
    assert sha256(RTF) == RECEIPT["rtf_fixture"]["sha256"]
    assert len(RTF) == RECEIPT["rtf_fixture"]["size_bytes"]
    assert "not authored by ONLYOFFICE" in RECEIPT["source_fixture"]["note"]
    assert RECEIPT["authoring"]["application"] == "ONLYOFFICE Desktop Editors"
    assert (RECEIPT["authoring"]["version"], RECEIPT["authoring"]["build"]) == (
        "9.4.0",
        "563",
    )
    assert len(RECEIPT["authoring"]["converter_sha256"]) == 64
    assert RECEIPT["reader"]["version"] == "3.12.1"
    assert RECEIPT["extraction"]["profile"] == "text_only"
    assert len(EXPECTED["result_sha256"]) == 64
    assert all(url.startswith("https://github.com/") for url in RECEIPT["sources"])


def test_the_rtf_holds_real_nested_table_control_markers() -> None:
    for marker in RECEIPT["rtf_control_markers"]:
        assert b"\\" + marker.encode() in RTF
    assert RTF.startswith(b"{\\rtf1")


@needs_pinned_reader
def test_pinned_reader_extracts_the_nested_tables_deterministically() -> None:
    first = extract()
    second = extract()

    assert first == second
    assert first.failed_units == 0
    assert first.limit_breach is None
    assert (first.blocks, len(first.segments)) == (
        EXPECTED["blocks"],
        EXPECTED["segments"],
    )
    assert first.limitations["inventory"] == EXPECTED["scanner_inventory"]
    assert first.limitations["astInventory"] == EXPECTED["ast_inventory"]
    assert first.limitations["readerBuild"] == RECEIPT["reader"]["reader_build"]

    texts = {item.text for item in first.segments}
    assert {
        "Outer A1",
        "Outer B1",
        "Nested 1",
        "Nested 4",
        "After nested table",
    } <= texts
    assert "Unicode: café — Ελληνικά — 日本語" in texts
    assert "OmniVia DOC-06 ONLYOFFICE RTF Qualification" in texts


@needs_pinned_reader
def test_canonical_result_hash_matches_the_receipt() -> None:
    canonical = json.dumps(
        dataclasses.asdict(extract()),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert sha256(canonical.encode()) == EXPECTED["result_sha256"]
