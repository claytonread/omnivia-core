#!/usr/bin/env python3
"""Record real PPTX and HEIC decoder qualification evidence for DOC-05/09."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import platform
import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnivia_core_runtime.service import ooxml_extractors, raster_extractors
from omnivia_core_runtime.service.document_processing import portable_profile
from omnivia_core_runtime.service.ocr_resources import text_only_settings
from omnivia_core_runtime.storage import document_intake as store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "qualification/wp15/fixtures"
OUTPUT = (
    ROOT / "docs/development/qualification/"
    "doc05-doc09-document-qualification-macos-2026-10-19.json"
)
CAPTURE = "dcap-" + "d" * 64
TEXT_ONLY_REF = "document-text-only"
REVISION = "1"


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_path(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def result_digest(value: Any) -> str:
    encoded = json.dumps(
        dataclasses.asdict(value), sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + digest_bytes(encoded)


def peak_rss_bytes() -> int:
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024


def module_record(module: Any, build: str) -> dict[str, str]:
    path = Path(module.__file__).resolve()
    return {
        "build": build,
        "path": str(path.relative_to(ROOT)),
        "sha256": digest_path(path),
    }


def main() -> None:
    pptx_path = FIXTURES / "doc05-keynote-authored.pptx"
    heic_path = FIXTURES / "doc09-imageio-authored.heic"
    pptx_bytes = pptx_path.read_bytes()
    heic_bytes = heic_path.read_bytes()
    settings = text_only_settings()
    pptx_profile = portable_profile(
        media_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        profile_ref=TEXT_ONLY_REF,
        revision=REVISION,
        settings=settings,
    )
    heic_profile = portable_profile(
        media_type="image/heic",
        profile_ref=TEXT_ONLY_REF,
        revision=REVISION,
        settings=settings,
    )

    started_cpu = time.process_time()
    started_wall = time.perf_counter()
    pptx = ooxml_extractors.extract_pptx(pptx_bytes, capture_id=CAPTURE)
    heic = raster_extractors.extract_image(
        heic_bytes,
        capture_id=CAPTURE,
        media_type="image/heic",
        settings=settings,
    )
    elapsed_wall = time.perf_counter() - started_wall
    elapsed_cpu = time.process_time() - started_cpu

    roles = sorted({segment.semantic_role for segment in pptx.segments})
    texts = {segment.text for segment in pptx.segments if segment.text is not None}
    required_text = {
        "OmniVia DOC-05 Qualification",
        "Keynote-authored PPTX compatibility fixture",
        "Presenter notes are intentionally present for DOC-05 qualification.",
        "DOC-05 authored fixture comment",
        "Region 1",
        "April",
        "17.000000",
    }
    if not required_text <= texts:
        raise RuntimeError("the Keynote fixture did not produce its qualification text")
    if pptx.failed_units or heic.failed_units:
        raise RuntimeError("a qualification fixture reported a failed unit")
    if heic.expected != 1 or heic.visited != 1:
        raise RuntimeError("the Image I/O HEIC fixture was not visited exactly once")

    record = {
        "schema": "omnivia.doc05-doc09.document-qualification.v1",
        "recordedAt": datetime.now(UTC).isoformat(),
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "fixtures": {
            "doc05KeynotePptx": {
                "path": str(pptx_path.relative_to(ROOT)),
                "sha256": digest_bytes(pptx_bytes),
                "authoredBy": "Keynote export to Microsoft PowerPoint",
                "privacy": "comment author normalised to OmniVia QA",
            },
            "doc09ImageIoHeic": {
                "path": str(heic_path.relative_to(ROOT)),
                "sha256": digest_bytes(heic_bytes),
                "authoredBy": "macOS sips/Image I/O HEIC encoder",
            },
        },
        "engines": {
            "pptx": module_record(ooxml_extractors, "pptx-ooxml-2"),
            "raster": module_record(
                raster_extractors, raster_extractors.IMAGE_DECODER_BUILD
            ),
        },
        "profiles": {
            "pptx": {
                "value": pptx_profile,
                "sha256": store.profile_digest(pptx_profile),
            },
            "heic": {
                "value": heic_profile,
                "sha256": store.profile_digest(heic_profile),
            },
        },
        "resources": {
            "external": [],
            "reason": "text-only decoder qualification invokes no OCR model",
            "peakRssBytes": peak_rss_bytes(),
            "cpuSeconds": elapsed_cpu,
            "wallSeconds": elapsed_wall,
            "scope": "whole recorder process high-water mark and measured extraction interval",
        },
        "results": {
            "doc05": {
                "state": "succeeded",
                "expected": pptx.expected,
                "failedUnits": pptx.failed_units,
                "segments": len(pptx.segments),
                "roles": roles,
                "segmentsSha256": result_digest(pptx),
            },
            "doc09": {
                "state": "succeeded",
                "expected": heic.expected,
                "visited": heic.visited,
                "failedUnits": heic.failed_units,
                "format": heic.limitations["format"],
                "primaryFrames": heic.limitations["primaryFrames"],
                "segmentsSha256": result_digest(heic),
            },
        },
    }
    OUTPUT.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(OUTPUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
