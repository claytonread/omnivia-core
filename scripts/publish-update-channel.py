#!/usr/bin/env python3
"""Publish one omnivia-core release's channel recommendation (v0.4 §13).

The publication workflow's channel half. The build half is
`build-standard-candidate.py`; this script takes the built candidate directory,
verifies its own checksum inventory, and prepares the `omnivia-core-updates`
repository's `channel.json` so the stable channel recommends exactly that
release -- never clobbering an existing recommendation, and never recommending
anything that was not verified against the candidate's own build evidence.

**It emits the signed envelope v2**, which is what a Core installation now
reads. An unsigned v1 document is refused outright rather than written: every
production consumer refuses it, so writing one would publish a channel nobody
can act on.

**This script never holds a signing key.** `--signing-key` names a file outside
the repository, read through the owner-private reader the product already uses,
and the key material lives in one local variable for the length of one `sign`
call. Without it the script prepares the envelope *body* and prints the exact
signing input digest, so the owner can sign offline on a machine this tool never
touches and paste the signature back with `--signature`. `--apply` refuses an
unsigned document in either case.

Modes:
- default (prepare): validate + show exactly what would be published; touches
  nothing. The dry run is the default because a channel commit goes live
  instantly via GitHub raw.
- --apply: clone omnivia-core-updates, write the recommendation, and leave the
  commit + push to the operator (printed, not run) -- publication remains an
  owner-gated act; this tool never pushes.

Usage:
    python scripts/publish-update-channel.py \
        --candidate /path/to/standard-candidate \
        --sequence 8 \
        [--signing-key /path/to/key --key-id core-channel-2026a] \
        [--signature <base64url> --key-id core-channel-2026a] \
        [--valid-days 30] [--apply]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(REPO_ROOT / "src"),
    str(REPO_ROOT / "packages" / "omnivia-core-client" / "src"),
]

from omnivia_core_client.owner_private import read_owner_private  # noqa: E402
from omnivia_core_client.update_trust import (  # noqa: E402
    ENVELOPE_SCHEMA_VERSION,
    SIGNATURE_ALGORITHM,
    decode_base64url,
    signing_input,
)

from omnivia_core.contracts.v1.canonical_json import canonical_bytes  # noqa: E402

UPDATES_REPO: str = "claytonread/omnivia-core-updates"
#: The release asset the publication uploads: the built candidate bundle, as
#: the update coordinator's executor fetches it (BUNDLE_ASSET_NAME's producer).
BUNDLE_ZIP_NAME: str = "omnivia-core-standard-candidate.zip"
CHECKSUMS_FILE: str = "checksums-sha256.txt"
PRODUCT_ID: str = "omnivia-core"
CHANNEL: str = "stable"

FIRST_PARTY: Final = (
    "omnivia-core",
    "omnivia-core-runtime",
    "omnivia-core-client",
    "omnivia-core-cli",
    "omnivia-core-mcp",
)

#: An Ed25519 private key is 32 bytes; a PEM wrapping one is a few hundred. The
#: bound is what stops a mistyped path turning into an unbounded read.
MAX_KEY_BYTES: Final = 4096

#: One inventory row: the digest, exactly two spaces, the path. The same grammar
#: `compose-standard-compatibility-matrix.py` states and the updater enforces.
_CHECKSUM_ROW: Final = re.compile(r"\A([0-9a-f]{64})  (\S.*)\Z")

_KEY_ID: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _safe_relative(value: str) -> Path:
    """One relative inventory path, or a refusal.

    An inventory that can name ``../`` is an inventory that can point a digest
    check at a file outside the candidate and pass. The same policy the updater
    applies to an archive member, applied here to the row that names it.
    """
    if value.startswith("/") or "\\" in value:
        raise SystemExit(f"the checksum index names a non-relative path: {value}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SystemExit(f"the checksum index names a path that escapes: {value}")
    return Path(*parts)


def verify_checksums(candidate: Path) -> None:
    """Hold the candidate against its own inventory, in both directions.

    This script's docstring has always said it verifies the inventory and it
    never did: it tested that the file existed. Coverage runs both ways because
    one way is not a check -- a file the inventory does not mention is a file
    nothing vouched for, and the bundle built from this directory is what
    ``pip --find-links`` is later pointed at.
    """
    index = candidate / CHECKSUMS_FILE
    if not index.is_file():
        raise SystemExit("the candidate carries no checksum inventory")
    covered: set[str] = {CHECKSUMS_FILE, BUNDLE_ZIP_NAME}
    for row in index.read_text(encoding="utf-8").splitlines():
        if not row:
            continue
        match = _CHECKSUM_ROW.match(row)
        if match is None:
            raise SystemExit(f"the checksum index carries a malformed row: {row!r}")
        digest, relative = match.group(1), match.group(2)
        target = candidate / _safe_relative(relative)
        if not target.is_file():
            raise SystemExit(f"the checksum index names a missing file: {relative}")
        if _sha256(target) != digest:
            raise SystemExit(f"checksum verification failed for {relative}")
        covered.add(relative)
    actual = {
        path.relative_to(candidate).as_posix()
        for path in candidate.rglob("*")
        if path.is_file()
    }
    uncovered = sorted(actual - covered)
    if uncovered:
        raise SystemExit(
            "the checksum index does not cover the whole candidate; "
            f"{len(uncovered)} file(s) uncovered, the first being {uncovered[0]}"
        )


def read_candidate(candidate: Path) -> dict[str, Any]:
    """Validate one built candidate against its own release manifest."""
    manifest_path = candidate / "metadata" / "release-manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"no release manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "omnivia.standard-release-manifest.v1":
        raise SystemExit(f"unrecognised manifest format: {manifest.get('format')!r}")
    if manifest.get("profile") != list(FIRST_PARTY):
        raise SystemExit(f"unexpected candidate profile: {manifest.get('profile')!r}")
    verify_checksums(candidate)
    wheel_versions: dict[str, str] = {}
    for entry in manifest["wheels"]:
        if entry.get("first_party") is not True:
            continue
        match = json_wheel_version(Path(entry["path"]).name)
        if match is None:
            raise SystemExit(f"unparseable first-party wheel name: {entry['path']}")
        wheel_versions[match[0]] = match[1]
    if set(wheel_versions) != set(FIRST_PARTY):
        raise SystemExit(
            "the manifest does not cover the five first-party wheels: "
            f"{sorted(wheel_versions)}"
        )
    versions = set(wheel_versions.values())
    if len(versions) != 1:
        # The release *tag* is one version, so a candidate whose five wheels
        # disagree has no single tag to point at. The per-package map is still
        # carried through verbatim rather than collapsed to this one value: the
        # channel schema admits five different versions, and the updater
        # verifies each wheel against its own, so the day a candidate ships an
        # uneven set only this check has to change.
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
        "candidate_status": manifest.get("candidate_status"),
        "production_release_eligible": manifest.get("production_release_eligible"),
        "signing_status": (manifest.get("signing") or {}).get("status"),
    }


def json_wheel_version(filename: str) -> tuple[str, str] | None:
    match = re.match(r"^(omnivia_.+?)-(\d+(?:\.\d+)*)-py3-none-any\.whl$", filename)
    if match is None:
        return None
    return match.group(1).replace("_", "-"), match.group(2)


def envelope_body(
    recommendation: dict[str, Any],
    version: str,
    *,
    sequence: int,
    key_id: str,
    issued_at: str,
    not_after: str,
) -> dict[str, Any]:
    """The signed envelope v2 body: everything the signature will cover."""
    tag = f"core-v{version}"
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "product_id": PRODUCT_ID,
        "channel": CHANNEL,
        "sequence": sequence,
        "issued_at": issued_at,
        "not_before": issued_at,
        "not_after": not_after,
        "release": {
            "version": version,
            "release_url": f"https://github.com/claytonread/omnivia-core/releases/tag/{tag}",
            # Verbatim, never synthesised: the updater installs and then verifies
            # these exact per-package versions.
            "packages": recommendation["packages"],
            "bundle_sha256": recommendation["bundle_sha256"],
        },
        "signing": {"key_id": key_id, "algorithm": SIGNATURE_ALGORITHM},
    }


def load_signing_key(path: Path) -> Any:
    """One Ed25519 private key from a file this process proves it alone can read.

    Read through the product's own owner-private reader rather than
    ``Path.read_bytes``: no-follow, descriptor-pinned, bounded, and refused
    outright if the group or the world can reach it. A signing key in a
    world-readable file is not a signing key.

    The bytes are either a PEM-wrapped private key or the 32 raw seed bytes.
    Nothing about a failure quotes the path or the material.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    material = read_owner_private(path.resolve(), maximum_bytes=MAX_KEY_BYTES + 1)
    if material is None:
        raise SystemExit(
            "the signing key could not be read as an owner-private file "
            "(absent, a symlink, not owned by this user, or readable by others)"
        )
    if len(material) > MAX_KEY_BYTES:
        raise SystemExit("the signing key file is larger than a key")
    if material.lstrip().startswith(b"-----"):
        key = load_pem_private_key(material, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit("the signing key is not an Ed25519 private key")
        return key
    if len(material) != 32:
        raise SystemExit(
            "a raw Ed25519 private key is 32 bytes; use a PEM for anything else"
        )
    return Ed25519PrivateKey.from_private_bytes(material)


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _instant(offset_seconds: int = 0) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_seconds)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument(
        "--sequence",
        required=True,
        type=int,
        help="monotonic per channel; a consumer holding a floor refuses one at or below it",
    )
    parser.add_argument(
        "--valid-days",
        type=int,
        default=30,
        help="how long the envelope stays usable; bounds a captured one",
    )
    parser.add_argument("--key-id", help="the trust anchor this envelope is signed under")
    parser.add_argument(
        "--signing-key",
        type=Path,
        help="an owner-private file holding the Ed25519 private key, outside this repository",
    )
    parser.add_argument(
        "--signature",
        help="a signature produced offline over the printed signing input (canonical base64url)",
    )
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()

    if arguments.sequence < 1:
        raise SystemExit("a channel sequence starts at 1")
    if arguments.valid_days < 1:
        raise SystemExit("an envelope that is never valid is not worth publishing")
    if arguments.signing_key is not None and arguments.signature is not None:
        raise SystemExit("sign here or sign offline, not both")
    if (arguments.signing_key or arguments.signature) and not arguments.key_id:
        raise SystemExit("--key-id names the anchor a signature is verified under")
    if arguments.key_id and _KEY_ID.match(arguments.key_id) is None:
        raise SystemExit("the key id is malformed")

    recommendation = read_candidate(arguments.candidate)
    version = recommendation["version"]
    issued_at = _instant()
    body = envelope_body(
        recommendation,
        version,
        sequence=arguments.sequence,
        key_id=arguments.key_id or "unbound",
        issued_at=issued_at,
        not_after=_instant(arguments.valid_days * 86400),
    )
    message = signing_input(body, product_id=PRODUCT_ID)

    signature: str | None = None
    if arguments.signing_key is not None:
        signature = _b64url(load_signing_key(arguments.signing_key).sign(message))
    elif arguments.signature is not None:
        try:
            decode_base64url(arguments.signature, expected_bytes=64)
        except ValueError as failure:
            raise SystemExit(f"the signature is not canonical base64url: {failure}") from failure
        signature = arguments.signature

    if signature is None:
        if arguments.apply:
            raise SystemExit(
                "--apply refuses an unsigned channel: every production consumer "
                "refuses the unsigned shape, so writing one would publish a "
                "channel nobody can act on. Sign with --signing-key, or sign the "
                "printed input offline and pass --signature."
            )
        print(json.dumps(body, indent=2, sort_keys=True))
        print(
            "\n(prepare mode: nothing written, and nothing signed.)\n"
            f"  candidate_status: {recommendation['candidate_status']!r}\n"
            f"  production_release_eligible: "
            f"{recommendation['production_release_eligible']!r}\n"
            f"  candidate signing status: {recommendation['signing_status']!r}\n"
            "\nTo sign offline, sign exactly these bytes with the channel key:\n"
            f"  signing input sha256: {hashlib.sha256(message).hexdigest()}\n"
            f"  signing input bytes:  {len(message)}\n"
            "then re-run with --key-id and --signature.",
            file=sys.stderr,
        )
        return 0

    document = {**body, "signature": signature}

    if not arguments.apply:
        print(canonical_bytes(document).decode("ascii"))
        print(
            "\n(prepare mode: nothing written. To stage the channel update, "
            "re-run with --apply; the commit+push stays the operator's act.)\n"
            f"  candidate_status: {recommendation['candidate_status']!r}\n"
            f"  production_release_eligible: "
            f"{recommendation['production_release_eligible']!r}",
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
        live_sequence = current.get("sequence")
        if isinstance(live_sequence, int) and arguments.sequence <= live_sequence:
            # The floor is the whole point of the sequence: a new envelope at or
            # below the live one is one a consumer holding a floor would refuse,
            # and one an attacker could swap for the live document unnoticed.
            raise SystemExit(
                f"refusing sequence {arguments.sequence} at or below the live "
                f"{live_sequence}: the channel sequence is monotonic"
            )
        # Canonical bytes, exactly: the consumer requires the document to be the
        # canonical form of what it parses to, and a pretty-printed one is not.
        # One trailing newline is tolerated by the consumer and expected by git.
        path.write_bytes(canonical_bytes(document) + b"\n")
        commit_message = (
            f"channel: recommend core {version} seq {arguments.sequence} "
            f"(bundle {recommendation['bundle_sha256'][:19]}…)"
        )
        print(
            "staged — review, commit and push by hand (publication is owner-gated):\n"
            f"  cd {repo}\n"
            f"  git add channel.json && git commit -m {commit_message!r} && git push origin main\n"
            f"  (recorded_at {_instant()})"
        )
        print(canonical_bytes(document).decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
