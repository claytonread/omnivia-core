"""Release-owned construction of a signed trusted-runtime payload.

The private signing seed is an explicit file input and is never persisted in the
payload or returned to the caller.  The emitted public trust anchor is the only
key material a Platform build needs.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omnivia_core_runtime.distribution.trusted_runtime import (
    EXECUTABLE_LAYOUT,
    PAYLOAD_ARCHITECTURES,
    PAYLOAD_MANIFEST_VERSION,
    RELEASE_SIGNATURE_VERSION,
    RUNTIME_MANIFEST_NAME,
    RUNTIME_SIGNATURE_NAME,
    SIGNATURE_ALGORITHM,
    RuntimeResolutionError,
    TrustAnchor,
    canonical_json,
    payload_identity,
    signed_manifest_bytes,
    verify_payload,
)

_SEMVER: Final = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_CONTRACT: Final = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_KEY_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TIMESTAMP: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_MAX_FILES: Final = 4096
_MAX_FILE_BYTES: Final = 64 * 1024 * 1024
_MAX_PAYLOAD_BYTES: Final = 512 * 1024 * 1024
_CHUNK: Final = 1024 * 1024


class PayloadSigningError(RuntimeError):
    """The prepared directory cannot become a trusted runtime payload."""


@dataclass(frozen=True, slots=True)
class SignedPayload:
    payload_identity: str
    trust_anchor: dict[str, Any]


def _timestamp(value: str) -> datetime:
    if _TIMESTAMP.fullmatch(value) is None:
        raise PayloadSigningError("timestamp must use YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as failure:
        raise PayloadSigningError("timestamp is invalid") from failure


def _digest(path: Path, expected_size: int) -> str:
    digest = hashlib.sha256()
    observed = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            observed += len(chunk)
            if observed > expected_size:
                raise PayloadSigningError("payload member changed while signing")
            digest.update(chunk)
    if observed != expected_size:
        raise PayloadSigningError("payload member changed while signing")
    return digest.hexdigest()


def _inventory(root: Path, executable_paths: frozenset[str]) -> list[dict[str, Any]]:
    entries: list[tuple[str, Path, os.stat_result]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as failure:
            raise PayloadSigningError("payload directory could not be read") from failure
        for child in children:
            path = Path(child.path)
            try:
                status = os.lstat(path)
            except OSError as failure:
                raise PayloadSigningError("payload member could not be read") from failure
            if stat.S_ISLNK(status.st_mode):
                raise PayloadSigningError("payload symlinks are refused")
            relative = path.relative_to(root).as_posix()
            if relative in {RUNTIME_MANIFEST_NAME, RUNTIME_SIGNATURE_NAME}:
                raise PayloadSigningError("payload metadata already exists")
            if stat.S_ISDIR(status.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(status.st_mode):
                raise PayloadSigningError("payload members must be regular files")
            entries.append((relative, path, status))

    entries.sort(key=lambda entry: entry[0].encode("utf-8"))
    if len(entries) > _MAX_FILES:
        raise PayloadSigningError("payload inventory is too large")
    present = {name for name, _path, _status in entries}
    if not executable_paths <= present:
        raise PayloadSigningError("payload executable layout is incomplete")

    total = 0
    inventory: list[dict[str, Any]] = []
    for relative, path, status in entries:
        size = status.st_size
        if size < 0 or size > _MAX_FILE_BYTES:
            raise PayloadSigningError("payload member exceeds its size bound")
        total += size
        if total > _MAX_PAYLOAD_BYTES:
            raise PayloadSigningError("payload exceeds its size bound")
        inventory.append(
            {
                "path": relative,
                "sha256": _digest(path, size),
                "size": size,
                "executable": relative in executable_paths,
            }
        )
    return inventory


def _private_key(path: Path) -> Ed25519PrivateKey:
    if not path.is_absolute():
        raise PayloadSigningError("private-key path must be absolute")
    try:
        status = os.lstat(path)
    except OSError as failure:
        raise PayloadSigningError("private key could not be read") from failure
    if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise PayloadSigningError("private key must be a regular file")
    if os.name != "nt" and (
        status.st_uid != os.geteuid() or (status.st_mode & 0o077) != 0
    ):
        raise PayloadSigningError("private key must be owned by this user and owner-only")
    try:
        seed = path.read_bytes()
    except OSError as failure:
        raise PayloadSigningError("private key could not be read") from failure
    if len(seed) != 32:
        raise PayloadSigningError("Ed25519 private key must be a raw 32-byte seed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def _write_pair(root: Path, manifest: bytes, signature: bytes) -> None:
    parent = root.parent
    temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}.metadata-", dir=parent))
    published: list[Path] = []
    try:
        staged_manifest = temporary / RUNTIME_MANIFEST_NAME
        staged_signature = temporary / RUNTIME_SIGNATURE_NAME
        staged_manifest.write_bytes(manifest)
        staged_signature.write_bytes(signature)
        for staged, name in (
            (staged_manifest, RUNTIME_MANIFEST_NAME),
            (staged_signature, RUNTIME_SIGNATURE_NAME),
        ):
            destination = root / name
            os.link(staged, destination)
            published.append(destination)
    except (FileExistsError, OSError) as failure:
        for path in reversed(published):
            try:
                path.unlink()
            except OSError:
                pass
        raise PayloadSigningError("payload metadata could not be published") from failure
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def sign_runtime_payload(
    payload_root: Path,
    *,
    release_version: str,
    operating_system: str,
    architecture: str,
    minimum_bootstrap_contract: str,
    maximum_bootstrap_contract: str,
    key_id: str,
    private_key_path: Path,
    not_before: str,
    not_after: str,
    retired_at: str | None = None,
) -> SignedPayload:
    """Sign a prepared payload and return its public release input."""
    if not payload_root.is_absolute():
        raise PayloadSigningError("payload path must be absolute")
    try:
        root_status = os.lstat(payload_root)
    except OSError as failure:
        raise PayloadSigningError("payload directory could not be read") from failure
    if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
        raise PayloadSigningError("payload must be a real directory")
    if _SEMVER.fullmatch(release_version) is None:
        raise PayloadSigningError("release version is invalid")
    if operating_system not in EXECUTABLE_LAYOUT:
        raise PayloadSigningError("operating system is invalid")
    if architecture not in PAYLOAD_ARCHITECTURES:
        raise PayloadSigningError("architecture is invalid")
    if _CONTRACT.fullmatch(minimum_bootstrap_contract) is None or _CONTRACT.fullmatch(
        maximum_bootstrap_contract
    ) is None:
        raise PayloadSigningError("bootstrap contract version is invalid")
    if tuple(map(int, minimum_bootstrap_contract.split("."))) > tuple(
        map(int, maximum_bootstrap_contract.split("."))
    ):
        raise PayloadSigningError("bootstrap contract window is invalid")
    if _KEY_ID.fullmatch(key_id) is None:
        raise PayloadSigningError("key id is invalid")
    try:
        key_inside_payload = private_key_path.resolve(strict=True).is_relative_to(
            payload_root.resolve(strict=True)
        )
    except OSError as failure:
        raise PayloadSigningError("payload or private key could not be resolved") from failure
    if key_inside_payload:
        raise PayloadSigningError("private key must be outside the payload")

    before = _timestamp(not_before)
    after = _timestamp(not_after)
    retired = None if retired_at is None else _timestamp(retired_at)
    private_key = _private_key(private_key_path)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    anchor = TrustAnchor(
        key_id=key_id,
        public_key=public_key,
        not_before=before,
        not_after=after,
        retired_at=retired,
    )
    anchor_document: dict[str, Any] = {
        "key_id": key_id,
        "algorithm": SIGNATURE_ALGORITHM,
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "not_before": not_before,
        "not_after": not_after,
        "retired_at": retired_at,
    }

    cli, service = EXECUTABLE_LAYOUT[operating_system]
    executable_paths = frozenset((cli, service))
    inventory = _inventory(payload_root, executable_paths)
    by_path = {entry["path"]: entry for entry in inventory}
    manifest: dict[str, Any] = {
        "manifest_version": PAYLOAD_MANIFEST_VERSION,
        "release_version": release_version,
        "platform": {"os": operating_system, "arch": architecture},
        "executables": {
            "cli": {"path": cli, "sha256": by_path[cli]["sha256"]},
            "service": {"path": service, "sha256": by_path[service]["sha256"]},
        },
        "inventory": inventory,
        "compatibility": {
            "minimum_bootstrap_contract": minimum_bootstrap_contract,
            "maximum_bootstrap_contract": maximum_bootstrap_contract,
        },
        "signing": {"key_id": key_id, "algorithm": SIGNATURE_ALGORITHM},
    }
    identity = payload_identity(manifest)
    manifest["payload_identity"] = identity
    signature_document = {
        "signature_version": RELEASE_SIGNATURE_VERSION,
        "key_id": key_id,
        "algorithm": SIGNATURE_ALGORITHM,
        "payload_identity": identity,
        "signature": base64.b64encode(
            private_key.sign(signed_manifest_bytes(manifest, identity))
        ).decode("ascii"),
    }
    _write_pair(
        payload_root,
        canonical_json(manifest) + b"\n",
        canonical_json(signature_document) + b"\n",
    )
    try:
        verify_payload(
            payload_root,
            trust_anchors=[anchor],
            verification_time=before,
            bootstrap_contract_version=minimum_bootstrap_contract,
            host_operating_system=operating_system,
            host_architecture=architecture,
            expected_payload_identity=identity,
            expected_release_version=release_version,
            enforce_installation_policy=False,
        )
    except (OSError, RuntimeResolutionError, ValueError) as failure:
        for name in (RUNTIME_SIGNATURE_NAME, RUNTIME_MANIFEST_NAME):
            try:
                (payload_root / name).unlink()
            except OSError:
                pass
        raise PayloadSigningError("emitted payload failed self-verification") from failure
    return SignedPayload(payload_identity=identity, trust_anchor=anchor_document)


__all__ = ["PayloadSigningError", "SignedPayload", "sign_runtime_payload"]
