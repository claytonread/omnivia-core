"""One real signed runtime payload on disk, for the tests that need a tree.

The conformance corpus under ``contracts/runtime/v1/fixtures`` is the cross-language
authority and is materialised from JSON. This is the other half: the tests here need
a *source* directory to hand to ``install_candidate``, which the corpus cannot
provide because a case describes an already-installed tree.

The key is test-only and its seed is a digest of a published label, exactly like the
corpus's. Nothing signed with it means anything outside this suite.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from omnivia_core_runtime.distribution.trusted_runtime import (
    EXECUTABLE_LAYOUT,
    HOST_ARCHITECTURE,
    HOST_OPERATING_SYSTEM,
    PAYLOAD_MANIFEST_VERSION,
    RELEASE_SIGNATURE_VERSION,
    RUNTIME_MANIFEST_NAME,
    RUNTIME_SIGNATURE_NAME,
    SIGNATURE_ALGORITHM,
    TrustAnchor,
    canonical_json,
    payload_identity,
    signed_manifest_bytes,
)

TEST_KEY_ID = "test-only-runtime-suite"
OTHER_KEY_ID = "test-only-runtime-suite-b"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

_LABELS = {
    TEST_KEY_ID: b"omnivia trusted-runtime unit-test key a",
    OTHER_KEY_ID: b"omnivia trusted-runtime unit-test key b",
}


def private_key(key_id: str = TEST_KEY_ID) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(_LABELS[key_id]).digest())


def anchor(
    key_id: str = TEST_KEY_ID,
    *,
    not_before: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    not_after: datetime = datetime(2027, 1, 1, tzinfo=UTC),
    retired_at: datetime | None = None,
) -> TrustAnchor:
    raw = private_key(key_id).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return TrustAnchor(
        key_id=key_id,
        public_key=raw,
        not_before=not_before,
        not_after=not_after,
        retired_at=retired_at,
    )


def build_manifest(
    files: dict[str, bytes],
    *,
    release_version: str,
    operating_system: str,
    key_id: str,
    architecture: str = HOST_ARCHITECTURE,
    compatibility: tuple[str, str] = ("1.0", "1.0"),
) -> dict[str, Any]:
    cli_path, service_path = EXECUTABLE_LAYOUT[operating_system]
    manifest: dict[str, Any] = {
        "manifest_version": PAYLOAD_MANIFEST_VERSION,
        "release_version": release_version,
        "platform": {"os": operating_system, "arch": architecture},
        "executables": {
            "cli": {"path": cli_path, "sha256": hashlib.sha256(files[cli_path]).hexdigest()},
            "service": {
                "path": service_path,
                "sha256": hashlib.sha256(files[service_path]).hexdigest(),
            },
        },
        "inventory": [
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "executable": path in (cli_path, service_path),
            }
            for path, content in sorted(files.items(), key=lambda item: item[0].encode("utf-8"))
        ],
        "compatibility": {
            "minimum_bootstrap_contract": compatibility[0],
            "maximum_bootstrap_contract": compatibility[1],
        },
        "signing": {"key_id": key_id, "algorithm": SIGNATURE_ALGORITHM},
    }
    manifest["payload_identity"] = payload_identity(manifest)
    return manifest


def write_payload(
    root: Path,
    *,
    release_version: str = "0.6.5",
    operating_system: str = HOST_OPERATING_SYSTEM,
    architecture: str = HOST_ARCHITECTURE,
    key_id: str = TEST_KEY_ID,
    sign_with: str | None = None,
    compatibility: tuple[str, str] = ("1.0", "1.0"),
    marker: str = "a",
    extra_files: dict[str, bytes] | None = None,
) -> str:
    """Write one complete signed payload under `root` and return its identity."""
    cli_path, service_path = EXECUTABLE_LAYOUT[operating_system]
    label = f"{release_version}-{marker}".encode("ascii")
    files: dict[str, bytes] = {
        cli_path: b"#!/bin/sh\n# omnivia " + label + b"\n",
        service_path: b"#!/bin/sh\n# omnivia-core-service " + label + b"\n",
        "lib/omnivia/release.txt": b"payload " + label + b"\n",
    }
    files.update(extra_files or {})
    manifest = build_manifest(
        files,
        release_version=release_version,
        operating_system=operating_system,
        architecture=architecture,
        key_id=key_id,
        compatibility=compatibility,
    )
    identity: str = manifest["payload_identity"]
    signature = {
        "signature_version": RELEASE_SIGNATURE_VERSION,
        "key_id": key_id,
        "algorithm": SIGNATURE_ALGORITHM,
        "payload_identity": identity,
        "signature": base64.b64encode(
            private_key(sign_with or key_id).sign(signed_manifest_bytes(manifest, identity))
        ).decode("ascii"),
    }

    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (root / RUNTIME_MANIFEST_NAME).write_bytes(canonical_json(manifest) + b"\n")
    (root / RUNTIME_SIGNATURE_NAME).write_bytes(canonical_json(signature) + b"\n")
    return identity
