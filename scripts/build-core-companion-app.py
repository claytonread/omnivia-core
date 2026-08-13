#!/usr/bin/env python3
"""Build the deterministic unsigned OmniVia Core status companion app bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
from pathlib import Path
from typing import Final

BUNDLE_ID: Final = "com.omnivia.core.status"
BUNDLE_NAME: Final = "OmniVia Core"
EXECUTABLE_NAME: Final = "omnivia-core-status-menu"
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class PackagingError(Exception):
    pass


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_companion_app(*, executable: Path, output: Path, version: str) -> Path:
    if not executable.is_absolute() or not executable.is_file() or executable.is_symlink():
        raise PackagingError("companion executable refused")
    if _SEMVER.fullmatch(version) is None:
        raise PackagingError("companion version refused")
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise PackagingError("companion output refused")

    app = output / f"{BUNDLE_NAME}.app"
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True, mode=0o755)
    resources.mkdir(mode=0o755)
    target = macos / EXECUTABLE_NAME
    shutil.copyfile(executable, target, follow_symlinks=False)
    os.chmod(target, 0o755)

    major, minor, patch = (int(member) for member in version.split("."))
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": BUNDLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": f"{major}.{minor}.{patch}",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }
    info = app / "Contents" / "Info.plist"
    info.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))
    os.chmod(info, 0o644)

    manifest = {
        "bundle_id": BUNDLE_ID,
        "bundle_name": BUNDLE_NAME,
        "bundle_path": f"{BUNDLE_NAME}.app",
        "candidate_status": "qualification_only",
        "executable": EXECUTABLE_NAME,
        "executable_sha256": _file_digest(target),
        "production_release_eligible": False,
        "schema_version": 1,
        "signature_status": "unsigned",
        "version": version,
    }
    (output / "companion-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )
    return app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    try:
        build_companion_app(
            executable=arguments.executable,
            output=arguments.output,
            version=arguments.version,
        )
    except (OSError, PackagingError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
