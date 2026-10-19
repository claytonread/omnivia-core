from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from omnivia_core_runtime.storage import document_intake as store

ROOT = Path(__file__).resolve().parents[1]
RECORD = (
    ROOT / "docs/development/qualification/"
    "doc05-doc09-document-qualification-macos-2026-10-19.json"
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_doc05_doc09_record_binds_real_fixtures_engines_profiles_and_results() -> None:
    recorded: dict[str, Any] = json.loads(RECORD.read_text())

    assert recorded["schema"] == "omnivia.doc05-doc09.document-qualification.v1"
    for fixture in recorded["fixtures"].values():
        assert digest(ROOT / fixture["path"]) == fixture["sha256"]
    assert recorded["fixtures"]["doc05KeynotePptx"]["authoredBy"].startswith("Keynote")
    assert recorded["fixtures"]["doc09ImageIoHeic"]["authoredBy"].startswith("macOS")

    for engine in recorded["engines"].values():
        assert digest(ROOT / engine["path"]) == engine["sha256"]
        assert engine["build"]
    for profile in recorded["profiles"].values():
        assert store.profile_digest(profile["value"]) == profile["sha256"]

    assert recorded["results"]["doc05"]["state"] == "succeeded"
    assert recorded["results"]["doc05"]["expected"] == 1
    assert recorded["results"]["doc05"]["failedUnits"] == 0
    assert {"body", "chart", "comment", "note"} <= set(
        recorded["results"]["doc05"]["roles"]
    )
    assert (
        recorded["results"]["doc09"]
        | {
            "state": "succeeded",
            "expected": 1,
            "visited": 1,
            "failedUnits": 0,
            "format": "HEIF",
            "primaryFrames": 1,
        }
        == recorded["results"]["doc09"]
    )
    assert recorded["resources"]["external"] == []
    assert recorded["resources"]["peakRssBytes"] > 0
    assert recorded["resources"]["cpuSeconds"] >= 0
    assert recorded["resources"]["wallSeconds"] >= 0
