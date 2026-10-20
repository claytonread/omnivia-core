#!/usr/bin/env python3
"""Generate the fixed ten-page mixed PDF used by AT-35 qualification."""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf as fitz

PAGE_WIDTH = 612
PAGE_HEIGHT = 792


def _scan_png(text: str) -> bytes:
    source = fitz.open()
    page = source.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_text((72, 180), text, fontname="helv", fontsize=34, color=(0, 0, 0))
    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    payload = pixmap.tobytes("png")
    source.close()
    return payload


def generate(output: Path) -> None:
    document = fitz.open()
    for number in range(1, 11):
        page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        if number % 2:
            page.insert_text(
                (72, 180),
                f"Native page {number} qualified",
                fontname="helv",
                fontsize=34,
                color=(0, 0, 0),
            )
        else:
            page.insert_image(
                page.rect,
                stream=_scan_png(f"Scanned page {number} qualified"),
                keep_proportion=False,
            )
    document.set_metadata(
        {
            "title": "OmniVia AT-35 mixed ten-page qualification fixture",
            "author": "OmniVia",
            "subject": "Five native-text pages and five image-only pages",
            "creator": "scripts/generate-at35-pdf-fixture.py",
            "producer": f"PyMuPDF {fitz.VersionBind}",
            "creationDate": "D:20261018000000+11'00'",
            "modDate": "D:20261018000000+11'00'",
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    document.save(output, garbage=4, deflate=True, clean=True, no_new_id=True)
    document.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path(
            "packages/omnivia-core-runtime/tests/phase3/runtime/fixtures/"
            "at35-mixed-ten-page.pdf"
        ),
    )
    generate(parser.parse_args().output)


if __name__ == "__main__":
    main()
