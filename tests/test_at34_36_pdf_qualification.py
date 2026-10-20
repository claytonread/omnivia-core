from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from omnivia_core_runtime.storage import document_intake as store

ROOT = Path(__file__).resolve().parents[1]
RECORD = (
    ROOT
    / "docs/development/qualification/"
    "at34-at36-pdf-qualification-macos-2026-10-18.json"
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_at34_at36_record_binds_the_exact_fixtures_profile_and_result() -> None:
    recorded: dict[str, Any] = json.loads(RECORD.read_text())

    assert recorded["schema"] == "omnivia.at34-at36.pdf-qualification.v1"
    for fixture in recorded["fixtures"].values():
        assert _digest(ROOT / fixture["path"]) == fixture["sha256"]
    mixed = recorded["fixtures"]["at35MixedTenPagePdf"]
    assert mixed["pages"] == 10
    assert mixed["nativeTextPages"] == [1, 3, 5, 7, 9]
    assert mixed["imageOnlyPages"] == [2, 4, 6, 8, 10]

    profile = recorded["profile"]
    assert store.profile_digest(profile["value"]) == profile["sha256"]
    assert profile["value"]["processingMode"] == "text_and_local_ocr"
    assert profile["value"]["ocr"]["languages"] == ["eng"]

    assert recorded["results"]["at34"]["state"] == "succeeded"
    assert recorded["results"]["at35"] | {
        "state": "succeeded",
        "expected": 10,
        "visited": 10,
        "usefulUnits": 10,
        "failedUnits": 0,
    } == recorded["results"]["at35"]
    assert recorded["results"]["at36"] == {
        "imageOnlyPagesRecognised": [2, 4, 6, 8, 10],
        "qualifiedRecognition": True,
    }
    assert len(recorded["engine"]["sha256"]) == 64
    assert all(len(resource["sha256"]) == 64 for resource in recorded["resources"].values())
