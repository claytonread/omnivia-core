"""AT-09: an exact UTF-8, BOM and mixed-line-ending source, selected and ingested.

The fixture is committed with its SHA-256 recorded beside it. The end-to-end test
drives the macOS companion's production intake and delivery
(`SelectedSourceIngestionTests.swift`) against a real managed Core through the real
`omnivia document capture`, then reads the retained copy and the extraction back
out of the Core workspace. The Swift half proves the selected original's bytes and
filesystem attributes are unchanged by ingestion; this half proves what Core kept
and extracted is those same bytes.

Only the end-to-end test needs macOS and a Swift toolchain; it skips, saying so,
anywhere else.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import test_managed_start as managed
from omnivia_core_runtime.service.document_extractors import extract_document
from omnivia_core_runtime.service.document_intake import (
    owned_workspace,
    read_extraction_projection,
    read_retained_source,
)
from omnivia_core_runtime.service.document_processing import process_capture_outbox
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.storage import document_intake as store

FIXTURES = Path(__file__).parent / "fixtures" / "at09"
FIXTURE = FIXTURES / "utf8-bom-mixed-eol.txt"
RECORDED_SHA256 = (FIXTURES / "utf8-bom-mixed-eol.txt.sha256").read_text().split()[0]
PACKAGE = Path(__file__).resolve().parents[5] / "apps" / "core-status-menu-macos"
SWIFT_TEST = "SelectedSourceIngestionTests/testSelectedOriginalReachesCoreByteForByteAndIsLeftUnchanged"


def test_fixture_is_the_recorded_utf8_bom_mixed_line_ending_source() -> None:
    data = FIXTURE.read_bytes()

    assert hashlib.sha256(data).hexdigest() == RECORDED_SHA256
    assert data.startswith(b"\xef\xbb\xbf")
    assert data.count(b"\r\n") == 2
    assert data.count(b"\r") == data.count(b"\r\n") + 1  # one lone CR
    assert data.count(b"\n") == data.count(b"\r\n") + 2  # two lone LF
    assert not data.endswith((b"\r", b"\n"))
    assert data.decode("utf-8").isascii() is False


def test_extraction_of_the_fixture_keeps_every_character() -> None:
    result = extract_document(
        FIXTURE.read_bytes(), media_type="text/plain", capture_id="dcap-" + "a" * 64
    )

    assert result.state == "succeeded"
    assert [segment.text for segment in result.segments] == [FIXTURE.read_bytes().decode("utf-8")]


@pytest.fixture
def home() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix=managed.HOME_PREFIX, dir="/tmp"))
    managed._bootstrap(root)
    try:
        yield root
    finally:
        for pid in managed._service_pids(root):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
        shutil.rmtree(root, ignore_errors=True)


def _omnivia(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnivia_core_cli.main",
            "--installation-state",
            str(home / "installation-state"),
            "--workspace-id",
            managed.WORKSPACE_ID,
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="the companion is a macOS app")
@pytest.mark.skipif(shutil.which("swift") is None, reason="needs a Swift toolchain")
def test_selected_source_reaches_core_exact_and_is_left_unchanged(home: Path) -> None:
    # The companion runs `omnivia`; point it at this interpreter's CLI, with this
    # interpreter's service first on PATH so no foreign one answers.
    cli = home / "omnivia"
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m omnivia_core_cli.main "$@"\n')
    cli.chmod(0o755)
    report = home / "report.json"
    environment = {
        **os.environ,
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]]),
        "OMNIVIA_AT09_FIXTURE": str(FIXTURE),
        "OMNIVIA_AT09_FIXTURE_SHA256": RECORDED_SHA256,
        "OMNIVIA_AT09_CLI": str(cli),
        "OMNIVIA_AT09_INSTALLATION_STATE": str(home / "installation-state"),
        "OMNIVIA_AT09_WORKSPACE_ID": managed.WORKSPACE_ID,
        "OMNIVIA_AT09_REPORT": str(report),
    }

    swift = subprocess.run(
        ["swift", "test", "--filter", SWIFT_TEST],
        cwd=PACKAGE,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    output = swift.stdout + swift.stderr
    assert swift.returncode == 0, output[-4000:]
    assert report.is_file(), "the Swift half skipped or did not finish:\n" + output[-2000:]
    admitted = json.loads(report.read_text())

    stopped = _omnivia(home, "--timeout-ms", "60000", "service", "stop", "--json")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    with owned_workspace(
        workspace_root=home / "workspace",
        installation_root=home / "installation-state",
        core_version=SERVER_VERSION,
    ) as runner:
        capture = store.read_capture(
            runner.connection, workspace_id=runner.workspace_id, capture_id=admitted["capture_id"]
        )
        assert capture is not None
        assert capture.document_id == admitted["document_id"]
        assert capture.source_digest == "sha256:" + RECORDED_SHA256
        assert capture.source_length_bytes == len(FIXTURE.read_bytes())
        assert read_retained_source(runner, capture_id=capture.capture_id) == FIXTURE.read_bytes()

        # The service may or may not have ticked the extraction before it stopped.
        process_capture_outbox(runner)
        projected = read_extraction_projection(runner, document_id=capture.document_id)
        assert [row.text for row in projected] == [FIXTURE.read_bytes().decode("utf-8")]
