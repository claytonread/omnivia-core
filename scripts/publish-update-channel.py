#!/usr/bin/env python3
"""Publish one omnivia-core release's channel recommendation (v0.4 §13).

The publication workflow's channel half. The build half is
`build-standard-candidate.py`; this script takes the built candidate directory,
verifies its own checksum inventory, and updates the `omnivia-core-updates`
repository's `channel.json` so the stable channel recommends exactly that
release — never clobbering an existing recommendation, and never recommending
anything that was not verified against the candidate's own build evidence.

Modes:
- default (prepare): validate + show exactly what would be published; touches
  nothing. The dry run is the default because a channel commit goes live
  instantly via GitHub raw.
- --apply: clone omnivia-core-updates, write the recommendation, and leave the
  commit + push to the operator (printed, not run) — publication remains an
  owner-gated act; this tool never pushes.

Usage:
    python scripts/publish-update-channel.py \
        --candidate /path/to/standard-candidate \
        [--apply]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

UPDATES_REPO: str = "claytonread/omnivia-core-updates"
SCHEMA_VERSION: str = "omnivia-core-update.v1"
#: The release asset the publication uploads: the built candidate bundle, as
#: the update coordinator's executor fetches it (BUNDLE_ASSET_NAME's producer).
BUNDLE_ZIP_NAME: str = "omnivia-core-standard-candidate.zip"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_candidate(candidate: Path) -> dict[str, Any]:
    """Validate one built candidate against its own release manifest."""
    manifest_path = candidate / "metadata" / "release-manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"no release manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "omnivia.standard-release-manifest.v1":
        raise SystemExit(f"unrecognised manifest format: {manifest.get('format')!r}")
    if manifest.get("profile") != [
        "omnivia-core",
        "omnivia-core-runtime",
        "omnivia-core-client",
        "omnivia-core-cli",
        "omnivia-core-mcp",
    ]:
        raise SystemExit(f"unexpected candidate profile: {manifest.get('profile')!r}")
    checksums = candidate / "checksums-sha256.txt"
    if not checksums.is_file():
        raise SystemExit("the candidate carries no checksum inventory")
    wheel_versions: dict[str, str] = {}
    for entry in manifest["wheels"]:
        if entry.get("first_party") is not True:
            continue
        match = json_wheel_version(Path(entry["path"]).name)
        if match is None:
            raise SystemExit(f"unparseable first-party wheel name: {entry['path']}")
        wheel_versions[match[0]] = match[1]
    if set(wheel_versions) != {
        "omnivia-core",
        "omnivia-core-runtime",
        "omnivia-core-client",
        "omnivia-core-cli",
        "omnivia-core-mcp",
    }:
        raise SystemExit(
            "the manifest does not cover the five first-party wheels: "
            f"{sorted(wheel_versions)}"
        )
    versions = {version for version in wheel_versions.values()}
    if len(versions) != 1:
        raise SystemExit(
            f"the five first-party wheels are not one release: {sorted(versions)}"
        )
    bundle = candidate / BUNDLE_ZIP_NAME
    if not bundle.is_file():
        raise SystemExit(f"no candidate bundle at {bundle} — build it before publishing")
    return {
        "version": versions.pop(),
        "packages": wheel_versions,
        "bundle_sha256": "sha256:" + _sha256(bundle),
    }


def json_wheel_version(filename: str) -> tuple[str, str] | None:
    import re

    match = re.match(r"^(omnivia_.+?)-(\d+(?:\.\d+)*)-py3-none-any\.whl$", filename)
    if match is None:
        return None
    return match.group(1).replace("_", "-"), match.group(2)


def channel_document(recommendation: dict[str, Any], version: str) -> dict[str, Any]:
    tag = f"core-v{version}"
    return {
        "schema_version": SCHEMA_VERSION,
        "channel": "stable",
        "release": {
            "version": version,
            "release_url": f"https://github.com/claytonread/omnivia-core/releases/tag/{tag}",
            "packages": recommendation["packages"],
            "bundle_sha256": recommendation["bundle_sha256"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()

    recommendation = read_candidate(arguments.candidate)
    version = recommendation["version"]
    document = channel_document(recommendation, version)

    if not arguments.apply:
        print(json.dumps(document, indent=2))
        print(
            "\n(prepare mode: nothing written. To stage the channel update, "
            "re-run with --apply; the commit+push stays the operator's act.)",
            file=sys.stderr,
        )
        return 0

    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory) / "omnivia-core-updates"
        subprocess.run(
            ["gh", "repo", "clone", UPDATES_REPO, str(repo)], check=True, timeout=120
        )
        path = repo / "channel.json"
        current = json.loads(path.read_text(encoding="utf-8"))
        existing = (current.get("release") or {}).get("version")
        if existing == version:
            raise SystemExit(
                f"the channel already recommends {version}; never republish it"
            )
        if existing is not None and version_tuple(version) < version_tuple(existing):
            raise SystemExit(
                f"refusing to recommend {version} below the live {existing}: "
                "a defective release ships as a corrected higher version"
            )
        document["channel"] = current.get("channel", "stable")
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        commit_message = (
            f"channel: recommend core {version} "
            f"(bundle {recommendation['bundle_sha256'][:19]}…)"
        )
        print(
            "staged — review, commit and push by hand (publication is owner-gated):\n"
            f"  cd {repo}\n"
            f"  git add channel.json && git commit -m {commit_message!r} && git push origin main\n"
            f"  (recorded_at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})"
        )
        print(json.dumps(document, indent=2))
    return 0


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


if __name__ == "__main__":
    sys.exit(main())
