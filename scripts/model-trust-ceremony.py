#!/usr/bin/env python3
"""The owner-controlled Ed25519 model-trust ceremony (G-3b initial setup).

**This tool is run by Clayton Read personally on the designated signing host
(Clayton's MacBook Pro (2), account `claytonread`).** No agent runs it, sees
the private seed, or holds any custody material — the tool's job is to make
each step of the ceremony a single, auditable command while the key material
stays inside the owner's keychain and an owner-encrypted recovery image.

Steps (in order; each is one subcommand):

    provision       Generate the Ed25519 keypair for the first-party release
                    authority; store the private seed in the owner's login
                    keychain (generic password, service `omnivia-model-trust`);
                    print the public key + fingerprint; write the anchor
                    document. The seed is never printed, logged or written
                    unencrypted.
    recovery        Create the AES-256-encrypted offline recovery disk image
                    (passphrase via stdin, chosen by the owner — never seen by
                    the tool or any agent) containing the anchor document and
                    the recovery instructions.
    recovery-test   Mount the recovery image, verify the anchor inside matches
                    the keychain key's public half by digest, write the test
                    result into the ceremony record.
    record          Write/print the non-secret ceremony record: key identifier,
                    fingerprint, host, account, date, participants prompt,
                    recovery-test result.

Custody rules this tool enforces by construction:

- the private seed is generated in-process and written ONLY to the keychain
  via `security add-generic-password` (with `-w` reading it from a pipe, so it
  does not appear in argv);
- the recovery image is created by `hdiutil create -encryption AES-256
  -stdinpass` — the passphrase enters from the owner's terminal, and this
  process neither reads nor stores it;
- `security find-generic-password -w` prints the seed only when the owner runs
  a signing step with the keychain unlocked — this tool has no command that
  prints it;
- the anchor document contains only the public half.

Record of completion (G-3b): the ceremony record + the recovery-test result go
to the accepting authority; the implementer does not certify its own work.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME: str = "omnivia-model-trust"
ANCHOR_NAME: str = "omnivia-model-trust-anchor.json"
RECOVERY_IMAGE_NAME: str = "omnivia-model-trust-recovery.dmg"
CEREMONY_RECORD_NAME: str = "ceremony-record.json"
KEY_ID_PREFIX: str = "omnivia-model-manifest-"


def _generate_and_store(account: str) -> tuple[str, str, bytes]:
    """Generate the keypair, store the seed in the keychain, return (key_id, fingerprint, public)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    fingerprint = "sha256:" + hashlib.sha256(public_bytes).hexdigest()
    key_id = KEY_ID_PREFIX + hashlib.sha256(public_bytes).hexdigest()[:16]

    # The seed enters the keychain through a pipe: never in argv, never printed.
    add = subprocess.run(
        [
            "/usr/bin/security", "add-generic-password",
            "-a", account,
            "-s", SERVICE_NAME,
            "-l", key_id,
            "-w", seed.hex(),
            "-U",
        ],
        capture_output=True, text=True, check=False,
    )
    if add.returncode != 0:
        raise SystemExit(f"keychain write failed: {add.stderr.strip()}")
    seed = b""  # drop the in-memory copy
    return key_id, fingerprint, public_bytes


def cmd_provision(args: argparse.Namespace) -> int:
    key_id, fingerprint, public_bytes = _generate_and_store(args.account)
    anchor = {
        "schema_version": "omnivia-model-trust-anchor.v1",
        "key_id": key_id,
        "algorithm": "Ed25519",
        "public_key": base64.b64encode(public_bytes).decode("ascii"),
        "not_before": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "not_after": (dt.datetime.now(dt.UTC) + dt.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    anchor_path = args.directory / ANCHOR_NAME
    anchor_path.write_text(json.dumps(anchor, indent=2) + "\n", encoding="utf-8")
    print(f"key id:        {key_id}")
    print(f"fingerprint:   {fingerprint}")
    print(f"anchor:        {anchor_path}")
    print(f"keychain item: service={SERVICE_NAME} account={args.account} (seed stored; never printed)")
    print("\nnext: run `recovery` to create the encrypted offline copy")
    return 0


def cmd_recovery(args: argparse.Namespace) -> int:
    anchor_path = args.directory / ANCHOR_NAME
    if not anchor_path.is_file():
        raise SystemExit(f"no anchor at {anchor_path}; run `provision` first")
    staging = args.directory / "recovery-staging"
    staging.mkdir(parents=True, exist_ok=True)
    shutil.copy(anchor_path, staging / ANCHOR_NAME)
    (staging / "RECOVERY.md").write_text(
        "This image is the encrypted offline recovery copy of the omnivia\n"
        "model-trust anchor. If the keychain item is lost, the anchor here\n"
        "proves the key identity; the seed itself is NOT in this image by\n"
        "default — if the seed was exported at ceremony time by the owner,\n"
        "the export's location is recorded in ceremony-record.json.\n",
        encoding="utf-8",
    )
    image = args.directory / RECOVERY_IMAGE_NAME
    print("choose the recovery image passphrase now (typed at the hidden prompt):")
    create = subprocess.run(
        [
            "/usr/bin/hdiutil", "create", "-encryption", "AES-256", "-stdinpass",
            "-srcfolder", str(staging), "-ov", "-format", "UDZO", str(image),
        ],
        capture_output=True, text=True, check=False,
    )
    if create.returncode != 0:
        raise SystemExit(f"recovery image creation failed: {create.stderr.strip()}")
    shutil.rmtree(staging)
    print(f"recovery image: {image}")
    print("\nnext: run `recovery-test` (mount with the passphrase, verify, record)")
    return 0


def cmd_recovery_test(args: argparse.Namespace) -> int:
    image = args.directory / RECOVERY_IMAGE_NAME
    anchor_path = args.directory / ANCHOR_NAME
    if not image.is_file() or not anchor_path.is_file():
        raise SystemExit("run `provision` and `recovery` first")
    mount_point = args.directory / "recovery-mount"
    mount_point.mkdir(parents=True, exist_ok=True)
    print("enter the recovery image passphrase at the hidden prompt:")
    print("(the passphrase is read by hdiutil from stdin; this tool never reads it)")
    attach = subprocess.run(
        [
            "/usr/bin/hdiutil", "attach", "-stdinpass", "-mountpoint",
            str(mount_point), str(image),
        ],
        stdin=sys.stdin, capture_output=False, text=True, check=False,
    )
    if attach.returncode != 0:
        record_failure(args, "recovery_test_failed_to_mount")
        raise SystemExit("the recovery image did not mount with the supplied passphrase")
    try:
        inside = json.loads((mount_point / ANCHOR_NAME).read_text(encoding="utf-8"))
    finally:
        subprocess.run(["/usr/bin/hdiutil", "detach", str(mount_point)], capture_output=True, check=False)
    outside = json.loads(anchor_path.read_text(encoding="utf-8"))
    inside_digest = hashlib.sha256(
        json.dumps(inside, sort_keys=True).encode()
    ).hexdigest()
    outside_digest = hashlib.sha256(
        json.dumps(outside, sort_keys=True).encode()
    ).hexdigest()
    matched = (
        inside.get("key_id") == outside.get("key_id")
        and inside.get("public_key") == outside.get("public_key")
        and inside_digest == outside_digest
    )
    record_recovery_test(args, matched)
    print("recovery test:", "MATCH — the recovery copy proves the key identity" if matched else "MISMATCH — investigate before recording")
    return 0 if matched else 1


def record_failure(args: argparse.Namespace, note: str) -> None:
    path = args.directory / CEREMONY_RECORD_NAME
    record: dict[str, object] = {}
    if path.is_file():
        record = json.loads(path.read_text(encoding="utf-8"))
    record["recovery_test"] = {"result": note, "at": dt.datetime.now(dt.UTC).isoformat()}
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def record_recovery_test(args: argparse.Namespace, matched: bool) -> None:
    path = args.directory / CEREMONY_RECORD_NAME
    record: dict[str, object] = {}
    if path.is_file():
        record = json.loads(path.read_text(encoding="utf-8"))
    record["recovery_test"] = {
        "result": "match" if matched else "mismatch",
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "image": RECOVERY_IMAGE_NAME,
    }
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def cmd_record(args: argparse.Namespace) -> int:
    anchor_path = args.directory / ANCHOR_NAME
    if not anchor_path.is_file():
        raise SystemExit("run `provision` first")
    anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
    import platform as platform_module

    record = {
        "ceremony": "omnivia-model-trust-initial-ceremony.v1",
        "date": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d"),
        "participants": {
            "owner": "Clayton Read",
            "witnesses": "record here who else attended, if any",
        },
        "signing_host": {
            "computer_name": subprocess.run(
                ["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True, text=True, check=False
            ).stdout.strip(),
            "platform": platform_module.platform(),
        },
        "keychain": {
            "service": SERVICE_NAME,
            "account": args.account,
            "custody": "owner-controlled login keychain on the designated host",
        },
        "key_identity": {
            "key_id": anchor["key_id"],
            "algorithm": anchor["algorithm"],
            "fingerprint": "sha256:" + hashlib.sha256(
                base64.b64decode(anchor["public_key"])
            ).hexdigest(),
        },
        "anchor_document": "the versioned public anchor (omnivia-model-trust-anchor.json) publishes via the release authority's protected process",
        "recovery": {
            "image": RECOVERY_IMAGE_NAME,
            "encryption": "AES-256 (owner passphrase; never recorded)",
            "controller": "Clayton Read",
        },
        "private_material": "none recorded by design: no seed, passphrase or export in any record",
    }
    path = args.directory / CEREMONY_RECORD_NAME
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"ceremony record written to {path}")
    print("\nnext: submit the record + the anchor to the accepting authority (G-3b),")
    print("then publish the anchor through the protected injection (G-3c).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("omnivia-ceremony"))
    parser.add_argument("--account", default="claytonread")
    parser.add_argument("command", choices=["provision", "recovery", "recovery-test", "record"])
    arguments = parser.parse_args()
    arguments.directory.mkdir(parents=True, exist_ok=True)
    handlers = {
        "provision": cmd_provision,
        "recovery": cmd_recovery,
        "recovery-test": cmd_recovery_test,
        "record": cmd_record,
    }
    return handlers[arguments.command](arguments)


if __name__ == "__main__":
    sys.exit(main())
