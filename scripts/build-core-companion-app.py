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
FINDER_EXTENSION_ID: Final = "com.omnivia.core.finder-action"
FINDER_EXTENSION_EXECUTABLE: Final = "OmniViaFinderAction"
SHARE_EXTENSION_ID: Final = "com.omnivia.core.share"
SHARE_EXTENSION_EXECUTABLE: Final = "OmniViaShare"
FINDER_EXTENSION_MAX_ITEMS: Final = 10
# One bounded production activation rule, never TRUEPREDICATE. It admits
# exactly one extension item, every one of whose attachments conforms to
# public.data (a file's content) or public.directory (a folder, which
# public.folder conforms to), between one and FINDER_EXTENSION_MAX_ITEMS of
# them. That bound is the handoff mailbox's slot bound, so the Quick Action
# cannot offer a selection the companion would refuse.
#
# public.url also conforms to public.data, so the rule alone does not restrict
# activation to file URLs. The extension does: it refuses any provider that
# does not conform to public.file-url before it offers a single slot.
FINDER_EXTENSION_ACTIVATION_RULE: Final = (
    "SUBQUERY ("
    "extensionItems, $extensionItem,"
    " SUBQUERY ("
    "$extensionItem.attachments, $attachment,"
    ' ANY $attachment.registeredTypeIdentifiers UTI-CONFORMS-TO "public.data"'
    ' OR ANY $attachment.registeredTypeIdentifiers UTI-CONFORMS-TO "public.directory"'
    ").@count == $extensionItem.attachments.@count"
    " AND $extensionItem.attachments.@count >= 1"
    f" AND $extensionItem.attachments.@count <= {FINDER_EXTENSION_MAX_ITEMS}"
    ").@count == 1"
)
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class PackagingError(Exception):
    pass


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_executable(path: Path, label: str) -> None:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise PackagingError(f"{label} executable refused")


def _write_extension(
    *,
    plugins: Path,
    source: Path,
    bundle_name: str,
    executable_name: str,
    bundle_id: str,
    version: str,
    extension: dict[str, object],
) -> Path:
    bundle = plugins / f"{bundle_name}.appex"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True, mode=0o755)
    target = macos / executable_name
    shutil.copyfile(source, target, follow_symlinks=False)
    os.chmod(target, 0o755)
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": executable_name,
        "CFBundleIdentifier": bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": bundle_name,
        "CFBundlePackageType": "XPC!",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "13.0",
        "NSExtension": extension,
    }
    plist_path = bundle / "Contents" / "Info.plist"
    plist_path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML, sort_keys=True))
    os.chmod(plist_path, 0o644)
    return target


def build_companion_app(
    *,
    executable: Path,
    output: Path,
    version: str,
    finder_extension_executable: Path | None = None,
    share_extension_executable: Path | None = None,
) -> Path:
    _validate_executable(executable, "companion")
    if (finder_extension_executable is None) != (share_extension_executable is None):
        raise PackagingError("extension pair refused")
    if finder_extension_executable is not None and share_extension_executable is not None:
        _validate_executable(finder_extension_executable, "Finder Action")
        _validate_executable(share_extension_executable, "Share")
    if _SEMVER.fullmatch(version) is None:
        raise PackagingError("companion version refused")
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise PackagingError("companion output refused")

    app = output / f"{BUNDLE_NAME}.app"
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    plugins = app / "Contents" / "PlugIns"
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
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "Documents and folders",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["public.data", "public.folder"],
            }
        ],
        "NSServices": [
            {
                "NSMenuItem": {"default": "Add to OmniVia Core…"},
                "NSMessage": "addToOmniViaCore",
                "NSPortName": BUNDLE_NAME,
                "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
                "NSSendFileTypes": ["public.data", "public.directory"],
            }
        ],
    }
    info = app / "Contents" / "Info.plist"
    info.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))
    os.chmod(info, 0o644)

    extensions: list[dict[str, str]] = []
    if finder_extension_executable is not None and share_extension_executable is not None:
        plugins.mkdir(mode=0o755)
        finder_target = _write_extension(
            plugins=plugins,
            source=finder_extension_executable,
            bundle_name="Add to OmniVia Core",
            executable_name=FINDER_EXTENSION_EXECUTABLE,
            bundle_id=FINDER_EXTENSION_ID,
            version=version,
            extension={
                "NSExtensionPointIdentifier": "com.apple.ui-services",
                "NSExtensionPrincipalClass": "FinderActionViewController",
                "NSExtensionAttributes": {
                    "NSExtensionActivationRule": FINDER_EXTENSION_ACTIVATION_RULE,
                    "NSExtensionServiceAllowsFinderPreviewItem": True,
                    "NSExtensionServiceRoleType": "NSExtensionServiceRoleTypeViewer",
                },
            },
        )
        share_target = _write_extension(
            plugins=plugins,
            source=share_extension_executable,
            bundle_name="Share to OmniVia Core",
            executable_name=SHARE_EXTENSION_EXECUTABLE,
            bundle_id=SHARE_EXTENSION_ID,
            version=version,
            extension={
                "NSExtensionPointIdentifier": "com.apple.share-services",
                "NSExtensionPrincipalClass": "ShareViewController",
                "NSExtensionAttributes": {
                    "NSExtensionActivationRule": {
                        "NSExtensionActivationSupportsFileWithMaxCount": 10,
                        "NSExtensionActivationSupportsImageWithMaxCount": 10,
                    },
                },
            },
        )
        extensions = [
            {
                "bundle_id": FINDER_EXTENSION_ID,
                "executable_sha256": _file_digest(finder_target),
            },
            {
                "bundle_id": SHARE_EXTENSION_ID,
                "executable_sha256": _file_digest(share_target),
            },
        ]

    manifest = {
        "bundle_id": BUNDLE_ID,
        "bundle_name": BUNDLE_NAME,
        "bundle_path": f"{BUNDLE_NAME}.app",
        "candidate_status": "qualification_only",
        "executable": EXECUTABLE_NAME,
        "executable_sha256": _file_digest(target),
        "extensions": extensions,
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
    parser.add_argument("--finder-extension-executable", required=True, type=Path)
    parser.add_argument("--share-extension-executable", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        build_companion_app(
            executable=arguments.executable,
            output=arguments.output,
            version=arguments.version,
            finder_extension_executable=arguments.finder_extension_executable,
            share_extension_executable=arguments.share_extension_executable,
        )
    except (OSError, PackagingError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
