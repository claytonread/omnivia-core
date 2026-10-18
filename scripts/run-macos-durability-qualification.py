#!/usr/bin/env python3
"""Kill real helper processes around the AT-16/17 filesystem commit points."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "apps/core-status-menu-macos"
OUTPUT = (
    ROOT
    / "docs/development/qualification/"
    "at16-at17-process-interruption-macos-2026-10-18.json"
)


def _wait(path: Path) -> str:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if path.is_file():
            return path.read_text()
        time.sleep(0.02)
    raise RuntimeError(f"helper did not reach commit point: {path}")


def _interrupt(binary: Path, mode: str, root: Path) -> str:
    ready = root.parent / f"{mode}.ready"
    process = subprocess.Popen(
        [str(binary), "durability", mode, str(root), str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        identity = _wait(ready)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        if process.returncode != -signal.SIGKILL:
            detail = (process.stderr.read() if process.stderr else b"").decode()
            raise RuntimeError(f"{mode} did not die by SIGKILL: {detail}")
        return identity
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _inspect(binary: Path, kind: str, root: Path) -> dict[str, object]:
    completed = subprocess.run(
        [str(binary), "inspect-durability", kind, str(root)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def main() -> None:
    if platform.system() != "Darwin":
        raise SystemExit("AT-16/17 process interruption qualification requires macOS")
    subprocess.run(
        ["swift", "build", "--package-path", str(PACKAGE), "--product", "omnivia-core-capture-harness"],
        check=True,
    )
    shown = subprocess.run(
        ["swift", "build", "--package-path", str(PACKAGE), "--show-bin-path"],
        capture_output=True,
        text=True,
        check=True,
    )
    binary = Path(shown.stdout.strip()) / "omnivia-core-capture-harness"
    if not binary.is_file():
        raise RuntimeError(f"missing helper binary: {binary}")

    temporary = Path(tempfile.mkdtemp(prefix="omnivia-at16-at17-"))
    try:
        cases: dict[str, dict[str, object]] = {}
        for mode, kind in (
            ("share-before", "share"),
            ("share-after", "share"),
            ("queue-before", "queue"),
            ("queue-after", "queue"),
        ):
            root = temporary / mode
            identity = _interrupt(binary, mode, root)
            observed = _inspect(binary, kind, root)
            cases[mode] = {"identity": identity, "observed": observed}

        assert cases["share-before"]["observed"] == {"complete": True, "pending": []}
        share_after = cases["share-after"]
        assert share_after["observed"] == {
            "complete": True,
            "pending": [share_after["identity"]],
        }
        assert cases["queue-before"]["observed"] == {
            "items": [],
            "payloadsReadable": True,
            "states": [],
        }
        assert cases["queue-after"]["observed"] == {
            "items": ["durability-item"],
            "payloadsReadable": True,
            "states": ["pending"],
        }

        record = {
            "schema": "omnivia.at16-at17.process-interruption.v1",
            "recordedAt": datetime.now(UTC).isoformat(),
            "host": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "binary": {
                "path": str(binary.relative_to(ROOT)),
                "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            },
            "signal": "SIGKILL",
            "cases": cases,
        }
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        print(OUTPUT)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    main()
