from __future__ import annotations

import importlib.util
import json
import plistlib
import stat
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-core-companion-app.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_core_companion_app", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _executable(tmp_path: Path) -> Path:
    executable = tmp_path / "omnivia-core-status-menu"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return executable


def test_companion_bundle_has_stable_identity_and_unsigned_qualification_manifest(
    tmp_path: Path,
) -> None:
    module = _module()
    output = tmp_path / "candidate"
    app = module.build_companion_app(
        executable=_executable(tmp_path), output=output, version="0.6.5"
    )
    plist = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert plist == {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": "omnivia-core-status-menu",
        "CFBundleIdentifier": "com.omnivia.core.status",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "OmniVia Core",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.6.5",
        "CFBundleVersion": "0.6.5",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }
    executable = app / "Contents" / "MacOS" / "omnivia-core-status-menu"
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755
    manifest = json.loads((output / "companion-manifest.json").read_text(encoding="ascii"))
    assert manifest["bundle_id"] == "com.omnivia.core.status"
    assert manifest["signature_status"] == "unsigned"
    assert manifest["production_release_eligible"] is False
    assert len(manifest["executable_sha256"]) == 64


@pytest.mark.parametrize("version", ["0.6", "v0.6.5", "01.2.3", "0.6.5-rc1"])
def test_companion_packaging_refuses_non_release_versions(
    tmp_path: Path, version: str
) -> None:
    module = _module()
    with pytest.raises(module.PackagingError, match="version refused"):
        module.build_companion_app(
            executable=_executable(tmp_path),
            output=tmp_path / "candidate",
            version=version,
        )


def test_companion_packaging_refuses_overwrite_and_symlinked_input(tmp_path: Path) -> None:
    module = _module()
    output = tmp_path / "candidate"
    output.mkdir()
    with pytest.raises(module.PackagingError, match="output refused"):
        module.build_companion_app(
            executable=_executable(tmp_path), output=output, version="0.6.5"
        )

    real = _executable(tmp_path)
    linked = tmp_path / "linked-companion"
    linked.symlink_to(real)
    with pytest.raises(module.PackagingError, match="executable refused"):
        module.build_companion_app(
            executable=linked, output=tmp_path / "other", version="0.6.5"
        )
