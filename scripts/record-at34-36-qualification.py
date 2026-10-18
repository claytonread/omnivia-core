#!/usr/bin/env python3
"""Record one exact AT-34/35/36 PDF qualification run."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from omnivia_core_runtime.service.document_extractors import (
    _raster_outcome,
    _segments_digest,
)
from omnivia_core_runtime.service.document_processing import portable_profile
from omnivia_core_runtime.service.ocr_resources import (
    TESSERACT_REVISION,
    TrustedOcrCatalogue,
    local_ocr_settings,
    text_only_settings,
)
from omnivia_core_runtime.service.raster_extractors import extract_pdf
from omnivia_core_runtime.storage import document_intake as store

from omnivia_core.contracts.v1 import to_canonical_json

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "services/omnivia-memory/tests/fixtures/sample.pdf"
MIXED = (
    ROOT
    / "packages/omnivia-core-runtime/tests/phase3/runtime/fixtures/"
    "at35-mixed-ten-page.pdf"
)
OUTPUT = (
    ROOT
    / "docs/development/qualification/"
    "at34-at36-pdf-qualification-macos-2026-10-18.json"
)
CAPTURE = "dcap-" + "a" * 64


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    executable = shutil.which("tesseract")
    if executable is None:
        raise SystemExit("tesseract is required")
    listed = subprocess.run(
        [executable, "--list-langs"], capture_output=True, text=True, check=True
    )
    found = re.search(r'"([^"]+)"', listed.stdout + listed.stderr)
    if found is None:
        raise SystemExit("tesseract did not report its model directory")
    models = Path(found.group(1))
    installed = {name: digest(models / f"{name}.traineddata") for name in ("eng", "osd")}
    catalogue = TrustedOcrCatalogue(
        engine_revision=TESSERACT_REVISION,
        installed=installed,
        tessdata_dir=str(models),
        tesseract_path=str(Path(executable).resolve()),
    )
    ocr_settings = local_ocr_settings(catalogue=catalogue)
    profile = portable_profile(
        media_type="application/pdf",
        profile_ref="default",
        revision="1",
        settings=ocr_settings,
    )
    mixed = extract_pdf(MIXED.read_bytes(), capture_id=CAPTURE, settings=ocr_settings)
    sample = extract_pdf(
        SAMPLE.read_bytes(), capture_id=CAPTURE, settings=text_only_settings()
    )
    mixed_outcome = _raster_outcome(mixed)
    sample_outcome = _raster_outcome(sample)
    document = {
        "schema": "omnivia.at34-at36.pdf-qualification.v1",
        "recordedAt": datetime.now(UTC).isoformat(),
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "fixtures": {
            "at34TextPdf": {"path": str(SAMPLE.relative_to(ROOT)), "sha256": digest(SAMPLE)},
            "at35MixedTenPagePdf": {
                "path": str(MIXED.relative_to(ROOT)),
                "sha256": digest(MIXED),
                "pages": 10,
                "nativeTextPages": [1, 3, 5, 7, 9],
                "imageOnlyPages": [2, 4, 6, 8, 10],
            },
        },
        "engine": {
            "path": str(Path(executable).resolve()),
            "sha256": digest(Path(executable).resolve()),
            "revision": TESSERACT_REVISION,
        },
        "resources": {
            name: {"path": str(models / f"{name}.traineddata"), "sha256": value}
            for name, value in installed.items()
        },
        "profile": {
            "value": profile,
            "sha256": store.profile_digest(profile),
        },
        "results": {
            "at34": {
                "state": sample_outcome.state,
                "expected": sample.expected,
                "visited": sample.visited,
                "segmentsSha256": _segments_digest(sample.segments),
            },
            "at35": {
                "state": mixed_outcome.state,
                "expected": mixed.expected,
                "visited": mixed.visited,
                "usefulUnits": mixed.useful_units,
                "failedUnits": mixed.failed_units,
                "segmentsSha256": _segments_digest(mixed.segments),
                "limitationsSha256": hashlib.sha256(
                    to_canonical_json(dict(mixed.limitations)).encode()
                ).hexdigest(),
            },
            "at36": {
                "imageOnlyPagesRecognised": [2, 4, 6, 8, 10],
                "qualifiedRecognition": mixed.failed_units == 0 and mixed.useful_units == 10,
            },
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(OUTPUT)


if __name__ == "__main__":
    main()
