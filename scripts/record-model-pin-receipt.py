#!/usr/bin/env python3
"""Record a model-distribution pin receipt (D-8 scope item 1; G-2 evidence).

Reads one digest-only model manifest (`model-manifest.v1`, the shape
`distribution.model_manifest_trust` verifies once signing lands), fetches every
declared artifact from the publisher's immutable address, re-derives each
SHA-256 from the fetched bytes, re-derives the payload tree digest with the
upstream rule (sorted relative paths + raw per-file digest bytes over the
``model.mlpackage`` root), and writes a receipt document.

The receipt is evidence, not authority: it records what was fetched and what
matched. Gate closure remains acceptance through the existing gate authority.
Signing is deliberately out of scope here — the digest-only pin is D-1's first
step, and the signature slot activates later without changing the artifact
digests the pin records.

Usage:
    python scripts/record-model-pin-receipt.py \
        --manifest docs/distribution/laya-typed-decisions-manifest-v1.json \
        --repo aac6fef/laya-typed-decisions-coreml \
        --revision 28d24fa8d67a3264556b23391ec6c3fd98573056 \
        --tree-root model.mlpackage \
        --expected-tree-digest 517a8071… \
        --out benchmarks/reports/engineering-memory/pin-receipt-<n>.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen


#: Per the upstream ``laya_coreml.artifacts.tree_digest``: sha256 over, in
#: sorted-relative-path order, each file's relative path bytes followed by the
#: raw 32 bytes of its SHA-256 digest. The tree root is the ``model.mlpackage``
#: payload, so paths are relative to that root.
def tree_digest(files: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for relative_path, hex_digest in sorted(files):
        digest.update(relative_path.encode())
        digest.update(bytes.fromhex(hex_digest))
    return digest.hexdigest()


def _fetch(address: str) -> bytes:
    with urlopen(address, timeout=600) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--tree-root", default="model.mlpackage")
    parser.add_argument("--expected-tree-digest", required=True)
    parser.add_argument("--out", required=True, type=Path)
    arguments = parser.parse_args()

    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "model-manifest.v1":
        print("refusing: manifest schema_version is not model-manifest.v1", file=sys.stderr)
        return 2
    if manifest.get("signing") is not None:
        print("refusing: this tool records the digest-only pin; use the signed verifier", file=sys.stderr)
        return 2

    revision = arguments.revision
    entries: list[dict[str, Any]] = []
    tree_files: list[tuple[str, str]] = []
    failures: list[str] = []
    started = time.time()
    for artifact in manifest["artifacts"]:
        name = artifact["name"]
        address = f"https://huggingface.co/{arguments.repo}/resolve/{revision}/{name}"
        data = _fetch(address)
        observed = hashlib.sha256(data).hexdigest()
        matched = observed == artifact["sha256"] and len(data) == artifact["size"]
        entry = {
            "name": name,
            "declared_sha256": artifact["sha256"],
            "observed_sha256": observed,
            "declared_size": artifact["size"],
            "observed_size": len(data),
            "matched": matched,
        }
        entries.append(entry)
        if not matched:
            failures.append(name)
        if name.startswith(arguments.tree_root + "/"):
            tree_files.append((name[len(arguments.tree_root) + 1 :], observed))
        print(f"{'ok ' if matched else 'BAD'} {name} ({len(data)} bytes)", flush=True)

    computed_tree = tree_digest(tree_files)
    tree_matched = computed_tree == arguments.expected_tree_digest
    receipt = {
        "receipt": "model-distribution-pin.v1",
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "manifest_identity": manifest["manifest_identity"],
        "identifier": manifest["identifier"],
        "version": manifest["version"],
        "publisher_repository": arguments.repo,
        "publisher_revision": revision,
        "artifacts": entries,
        "tree_root": arguments.tree_root,
        "expected_tree_digest": arguments.expected_tree_digest,
        "computed_tree_digest": computed_tree,
        "tree_digest_matched": tree_matched,
        "all_artifacts_matched": not failures,
        "failures": failures,
        "elapsed_seconds": round(time.time() - started, 1),
        "signing": "absent (digest-only pin; signature activates under D-1 without changing artifact digests)",
    }
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"receipt written to {arguments.out}")
    if failures or not tree_matched:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
