#!/usr/bin/env python3
"""Emit a signed trusted-runtime v1 manifest for a prepared payload."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from omnivia_core_runtime.distribution.runtime_payload_signing import (
    PayloadSigningError,
    sign_runtime_payload,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--os", required=True, choices=("macos", "linux", "windows"))
    parser.add_argument("--arch", required=True, choices=("arm64", "x86_64"))
    parser.add_argument("--minimum-bootstrap-contract", default="1.0")
    parser.add_argument("--maximum-bootstrap-contract", default="1.0")
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--private-key-file", required=True, type=Path)
    parser.add_argument("--not-before", required=True)
    parser.add_argument("--not-after", required=True)
    parser.add_argument("--retired-at")
    arguments = parser.parse_args()
    try:
        result = sign_runtime_payload(
            arguments.payload,
            release_version=arguments.release_version,
            operating_system=arguments.os,
            architecture=arguments.arch,
            minimum_bootstrap_contract=arguments.minimum_bootstrap_contract,
            maximum_bootstrap_contract=arguments.maximum_bootstrap_contract,
            key_id=arguments.key_id,
            private_key_path=arguments.private_key_file,
            not_before=arguments.not_before,
            not_after=arguments.not_after,
            retired_at=arguments.retired_at,
        )
    except (OSError, ValueError, PayloadSigningError):
        print("runtime payload signing refused", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"payload_identity": result.payload_identity, "trust_anchor": result.trust_anchor},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
