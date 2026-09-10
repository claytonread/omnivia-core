from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnivia_core_runtime.distribution.runtime_payload_signing import (
    PayloadSigningError,
    sign_runtime_payload,
)
from omnivia_core_runtime.distribution.trusted_runtime import (
    RUNTIME_MANIFEST_NAME,
    RUNTIME_SIGNATURE_NAME,
    TrustAnchor,
    verify_payload,
)


def _payload(tmp_path: Path) -> Path:
    payload = tmp_path / "payload"
    binary = payload / "bin"
    binary.mkdir(parents=True)
    (binary / "omnivia").write_bytes(b"#!/bin/sh\nexit 0\n")
    (binary / "omnivia-core-service").write_bytes(b"#!/bin/sh\nexit 0\n")
    (payload / "release.txt").write_text("release member\n", encoding="utf-8")
    return payload


def _key(tmp_path: Path, value: bytes = b"k" * 32) -> Path:
    path = tmp_path / "release.key"
    path.write_bytes(value)
    path.chmod(0o600)
    return path


def _sign(payload: Path, key: Path, **overrides: object):
    arguments: dict[str, object] = {
        "release_version": "0.6.5",
        "operating_system": "macos",
        "architecture": "arm64",
        "minimum_bootstrap_contract": "1.0",
        "maximum_bootstrap_contract": "1.0",
        "key_id": "release-2026a",
        "private_key_path": key,
        "not_before": "2026-01-01T00:00:00Z",
        "not_after": "2027-01-01T00:00:00Z",
    }
    arguments.update(overrides)
    return sign_runtime_payload(payload, **arguments)  # type: ignore[arg-type]


def test_signer_emits_a_payload_the_reference_verifier_accepts(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    result = _sign(payload, _key(tmp_path))

    assert result.payload_identity.startswith("sha256:")
    assert set(result.trust_anchor) == {
        "key_id",
        "algorithm",
        "public_key",
        "not_before",
        "not_after",
        "retired_at",
    }
    assert (payload / RUNTIME_MANIFEST_NAME).is_file()
    assert (payload / RUNTIME_SIGNATURE_NAME).is_file()

    verified = verify_payload(
        payload,
        trust_anchors=[TrustAnchor.from_document(result.trust_anchor)],
        verification_time=datetime(2026, 9, 11, tzinfo=UTC),
        host_operating_system="macos",
        host_architecture="arm64",
        enforce_installation_policy=False,
    )
    assert verified.payload_identity == result.payload_identity


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("release_version", "01.0.0"),
        ("key_id", "bad key"),
        ("minimum_bootstrap_contract", "١.٠"),
    ],
)
def test_invalid_schema_fields_are_refused_without_metadata(
    tmp_path: Path, override: str, value: str
) -> None:
    payload = _payload(tmp_path)
    with pytest.raises(PayloadSigningError):
        _sign(payload, _key(tmp_path), **{override: value})
    assert not (payload / RUNTIME_MANIFEST_NAME).exists()
    assert not (payload / RUNTIME_SIGNATURE_NAME).exists()


def test_wrong_key_length_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PayloadSigningError):
        _sign(_payload(tmp_path), _key(tmp_path, b"short"))


def test_private_key_inside_payload_is_never_inventoried(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    key = payload / "release.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    with pytest.raises(PayloadSigningError):
        _sign(payload, key)
    assert not (payload / RUNTIME_MANIFEST_NAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission policy")
def test_non_private_key_permissions_are_refused(tmp_path: Path) -> None:
    key = _key(tmp_path)
    key.chmod(0o640)
    with pytest.raises(PayloadSigningError):
        _sign(_payload(tmp_path), key)


def test_symlink_and_stale_metadata_are_refused(tmp_path: Path) -> None:
    if os.name != "nt":
        payload = _payload(tmp_path)
        (payload / "link").symlink_to(payload / "release.txt")
        with pytest.raises(PayloadSigningError):
            _sign(payload, _key(tmp_path))

    other = tmp_path / "other"
    payload = _payload(other)
    (payload / RUNTIME_MANIFEST_NAME).write_text("{}", encoding="ascii")
    with pytest.raises(PayloadSigningError):
        _sign(payload, _key(other))


def test_missing_fixed_executable_is_refused(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    (payload / "bin" / "omnivia-core-service").unlink()
    with pytest.raises(PayloadSigningError):
        _sign(payload, _key(tmp_path))
