"""The DOC-13 packaged-runtime lane: its gate set and its offline verifier.

`produce` needs a real macOS host, real engine inputs and several minutes, so
what is asserted here is everything that does not: the gate set is closed and
starts closed, the record's shape is fixed, and `verify` refuses a record that
claims less than a pass -- including one that claims release eligibility, which
this lane never establishes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-doc13-packaged-runtime.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_doc13_packaged_runtime", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: `@dataclass` under `from __future__ import
    # annotations` resolves string annotations through `sys.modules`, so an
    # unregistered module cannot define one.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _passing_record(module: ModuleType) -> dict[str, object]:
    return {
        "format": module.RECORD_FORMAT,
        "candidate": {"release_eligible": False},
        "findings": [],
        "gates": dict.fromkeys(module.GATES, True),
        "verdict": "pass",
    }


def test_the_lane_names_every_gate_its_claim_needs() -> None:
    """The gate set is the claim, written down once.

    DOC-13 is "English works offline ... absent model refuses"; DOC-16 adds that
    only admitted input, packaged models and private scratch are in scope. Each
    half has a gate, and the tamper gate is what stops a run that only ever saw
    good bytes from claiming it would have noticed bad ones.
    """
    module = _module()

    assert module.GATES == (
        "candidate_assembled",
        "bundle_coherent",
        "payload_verified",
        "payload_resolves_inside_itself",
        "engines_run_without_package_manager_path",
        "english_recognised_offline",
        "rtf_read_offline",
        "tampered_payload_refused",
    )
    # The prefixes that must appear nowhere, and the environment the engines are
    # held to: no `DYLD_*`, no package-manager directory, nothing inherited.
    assert module.PACKAGE_MANAGER_PREFIXES == (
        "/opt/homebrew/",
        "/opt/local/",
        "/usr/local/",
    )
    assert module.OFFLINE_PATH == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert module.SYSTEM_PREFIXES == ("/usr/lib/", "/System/")


def test_verify_accepts_only_a_record_in_which_every_gate_passed(
    tmp_path: Path,
) -> None:
    module = _module()
    record = tmp_path / "record.json"
    record.write_text(json.dumps(_passing_record(module)), encoding="utf-8")

    assert module.main(["verify", "--record", str(record)]) == 0


@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param(
            lambda record: record["gates"].update(english_recognised_offline=False),
            id="a gate did not pass",
        ),
        pytest.param(
            lambda record: record["gates"].pop("tampered_payload_refused"),
            id="a gate is missing from the set",
        ),
        pytest.param(
            lambda record: record["gates"].update(extra_gate=True),
            id="a gate this lane does not define",
        ),
        pytest.param(
            lambda record: record.update(findings=["something was wrong"]),
            id="the record carries findings",
        ),
        pytest.param(
            lambda record: record.update(verdict="fail"),
            id="the verdict is not a pass",
        ),
        pytest.param(
            lambda record: record["candidate"].update(release_eligible=True),
            id="the record claims release eligibility",
        ),
        pytest.param(
            lambda record: record.update(format="omnivia.something-else.v1"),
            id="the record names another format",
        ),
    ],
)
def test_verify_refuses_a_record_that_claims_less_than_a_pass(
    tmp_path: Path, spoil: object
) -> None:
    """Every way a record can fall short is a refusal, including a truthful one.

    A gate the run never reached stays false in the record, so a lane that
    produced partial evidence is refused here rather than read as a pass over
    the gates it did reach.
    """
    module = _module()
    record = _passing_record(module)
    spoil(record)  # type: ignore[operator]
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    assert module.main(["verify", "--record", str(path)]) == 1


def test_verify_refuses_an_unreadable_record(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "record.json"
    path.write_text("{ not json", encoding="utf-8")

    assert module.main(["verify", "--record", str(path)]) == 1
    assert module.main(["verify", "--record", str(tmp_path / "absent.json")]) == 1


@pytest.mark.parametrize("stamp", ["2026-10-16", "2026-10-20"])
def test_the_committed_record_of_this_host_still_verifies(stamp: str) -> None:
    """The evidence behind the ledger entry is checked, not merely stored.

    A record nobody re-reads is a claim with no gate behind it. This is the
    offline half of the lane run over each committed file, so a record that was
    edited after the run -- or a gate set that has since grown -- fails here.
    The second run reproduces the first: same host class, same engine builds,
    same source digests, same packaged model digests.
    """
    module = _module()
    record = (
        ROOT
        / "docs"
        / "development"
        / "qualification"
        / f"doc13-packaged-runtime-macos-{stamp}.json"
    )

    assert module.main(["verify", "--record", str(record)]) == 0
    stored = json.loads(record.read_text(encoding="utf-8"))
    # The record pins what actually ran: the engines' own reported builds, the
    # packaged model digests the production path resolved, and the absence of
    # any release claim.
    assert stored["engine_versions"] == {
        "pandoc": "pandoc 3.12.1",
        "tesseract": "tesseract 5.5.3",
    }
    assert stored["environment"]["system"] == "Darwin"
    assert stored["environment"]["machine"] == "arm64"
    assert stored["source_inputs"] == {
        "pandoc": "692dfdcaf61fd1b9dcbf287f254c4ff0ef9a2f2283f4119125e54c0ff86be199",
        "tessdata_eng": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
        "tessdata_osd": "9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff",
        "tesseract": "9fd3da5637e8d490182708c3689f15921cddb9beb469c1f128bc210033714ef9",
    }
    assert stored["production"]["admitted_kind"] == "frozen"
    assert stored["production"]["packaged_languages"] == ["eng", "osd"]
    assert stored["production"]["recognised"] is True
    assert stored["production"]["rtf_state"] == "succeeded"
    assert stored["candidate"]["signature_status"] == "unsigned"
    assert stored["candidate"]["release_eligible"] is False


def test_produce_is_refused_off_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The lane produces evidence only where the subject actually runs."""
    module = _module()
    monkeypatch.setattr(module.sys, "platform", "linux")

    assert module.main(
        [
            "produce",
            "--installation-state",
            str(tmp_path / "state"),
            "--output",
            str(tmp_path / "out"),
            "--version",
            "0.6.5",
            "--companion-executable",
            str(tmp_path / "companion"),
            "--finder-extension-executable",
            str(tmp_path / "finder"),
            "--share-extension-executable",
            str(tmp_path / "share"),
            "--pandoc",
            str(tmp_path / "pandoc"),
            "--tesseract",
            str(tmp_path / "tesseract"),
            "--tessdata-eng",
            str(tmp_path / "eng.traineddata"),
            "--tessdata-osd",
            str(tmp_path / "osd.traineddata"),
            "--license",
            str(tmp_path / "NOTICE.txt"),
        ]
    ) == 1
    assert not (tmp_path / "out").exists()
