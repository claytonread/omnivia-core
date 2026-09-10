#!/usr/bin/env python3
"""Generate and gate the trusted-runtime v1 conformance fixtures and vectors.

``contracts/runtime/v1/schemas/trusted-runtime-v1.schema.json`` is hand-authored and
is the contract. Everything under ``contracts/runtime/v1/fixtures`` is *derived* --
every SHA-256, every payload identity and every Ed25519 signature in it -- so it is
emitted here rather than typed, and ``--check`` fails when the committed bytes and
the bytes this script would write differ.

That check is the whole point of a generator for these files. A hand-edited fixture
whose digest no longer matches its content is not a fixture that fails loudly; it is
a fixture that quietly stops testing the thing it was named for.

**Every key below is a published test key and has no production role.** The seeds
are derived from fixed labels precisely so anybody can regenerate the corpus, in any
language, and get the same bytes -- which is what makes the signature vectors usable
as vectors. Nothing in this repository holds, or should ever hold, a release signing
key: a real release is signed off-repository and only its public key reaches a trust
anchor.

Offline and deterministic. The only non-standard-library imports are the
repository's own reference implementation -- so a fixture cannot disagree with the
verifier about canonical bytes -- and ``cryptography`` for Ed25519.

    python scripts/generate-runtime-contract.py            # write
    python scripts/generate-runtime-contract.py --check     # verify, write nothing
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO_ROOT / "packages" / "omnivia-core-runtime" / "src")]

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from omnivia_core_runtime.distribution.trusted_runtime import (  # noqa: E402
    BOOTSTRAP_CONTRACT_VERSION,
    EXECUTABLE_LAYOUT,
    IDENTITY_DOMAIN,
    PAYLOAD_MANIFEST_VERSION,
    RELEASE_SIGNATURE_VERSION,
    RUNTIME_DESCRIPTOR_VERSION,
    RUNTIME_MANIFEST_NAME,
    RUNTIME_SIGNATURE_NAME,
    SIGNATURE_ALGORITHM,
    SIGNATURE_DOMAIN,
    canonical_json,
    payload_identity,
    signed_manifest_bytes,
)

CANONICAL_ROOT: Final = REPO_ROOT / "contracts" / "runtime" / "v1"
FIXTURES_ROOT: Final = CANONICAL_ROOT / "fixtures"
CASE_VERSION: Final = "1.0"
VECTORS_VERSION: Final = "1.0"

#: The instant every case is verified at unless it is about a key window.
NOW: Final = "2026-09-10T12:00:00Z"

#: Test-only release keys. The seed is `sha256(label)`, so the corpus regenerates
#: byte for byte anywhere. These are not production keys and never will be: a
#: production signing key never enters this repository in any form -- only its
#: public half, and only as a trust anchor a caller passes in.
KEY_LABELS: Final = {
    "test-only-release-2026a": b"omnivia trusted-runtime v1 test-only release key a",
    "test-only-release-2026b": b"omnivia trusted-runtime v1 test-only release key b",
    "test-only-unapproved": b"omnivia trusted-runtime v1 test-only unapproved key",
}

#: A window generously around `NOW`, for the anchors whose windows are not the point.
OPEN_WINDOW: Final = ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")

RELEASE: Final = "0.6.5"
OTHER_RELEASE: Final = "0.6.6"


def _seed(key_id: str) -> bytes:
    return hashlib.sha256(KEY_LABELS[key_id]).digest()


def _private(key_id: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_seed(key_id))


def _public_b64(key_id: str) -> str:
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    raw = _private(key_id).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def anchor(
    key_id: str,
    *,
    window: tuple[str, str] = OPEN_WINDOW,
    retired_at: str | None = None,
) -> dict[str, Any]:
    return {
        "key_id": key_id,
        "algorithm": SIGNATURE_ALGORITHM,
        "public_key": _public_b64(key_id),
        "not_before": window[0],
        "not_after": window[1],
        "retired_at": retired_at,
    }


# --------------------------------------------------------------------------
# Payload construction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Payload:
    """One signed payload: its files, its manifest and its detached signature."""

    release_version: str
    operating_system: str
    files: dict[str, bytes]
    manifest: dict[str, Any]
    signature: dict[str, Any]

    @property
    def identity_hex(self) -> str:
        identity: str = self.manifest["payload_identity"]
        return identity.removeprefix("sha256:")

    @property
    def manifest_bytes(self) -> bytes:
        return _document_bytes(self.manifest)

    @property
    def signature_bytes(self) -> bytes:
        return _document_bytes(self.signature)


def _document_bytes(document: dict[str, Any]) -> bytes:
    """A payload document on disk: the canonical bytes, with one trailing newline.

    The canonical form is what the identity and the signature are taken over, so
    writing anything else here would mean a file whose bytes differ from the bytes
    that were signed. The newline is outside the JSON value and is what a text
    editor and `git` expect of a committed file.
    """
    return canonical_json(document) + b"\n"


def build_payload(
    *,
    release_version: str = RELEASE,
    operating_system: str = "macos",
    architecture: str = "arm64",
    key_id: str = "test-only-release-2026a",
    signing_key_id: str | None = None,
    compatibility: tuple[str, str] = (BOOTSTRAP_CONTRACT_VERSION, BOOTSTRAP_CONTRACT_VERSION),
    manifest_version: str = PAYLOAD_MANIFEST_VERSION,
    extra_files: dict[str, bytes] | None = None,
    executable_paths: tuple[str, str] | None = None,
    manifest_extra: dict[str, Any] | None = None,
    sign_with: str | None = None,
    sign_over: dict[str, Any] | None = None,
) -> Payload:
    """One complete signed payload, built the way a release would build one.

    Every knob exists for one negative fixture and defaults to the honest value, so
    a case reads as "the valid payload, except for this".
    """
    cli_path, service_path = executable_paths or EXECUTABLE_LAYOUT[operating_system]
    marker = f"{release_version} {operating_system} {key_id}".encode("ascii")
    files: dict[str, bytes] = {
        cli_path: b"#!/bin/sh\n# test-only omnivia " + marker + b"\n",
        service_path: b"#!/bin/sh\n# test-only omnivia-core-service " + marker + b"\n",
        "lib/omnivia/release.txt": b"test-only payload member " + marker + b"\n",
    }
    files.update(extra_files or {})

    inventory = [
        {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "executable": path in (cli_path, service_path),
        }
        for path, content in sorted(files.items(), key=lambda item: item[0].encode("utf-8"))
    ]
    manifest: dict[str, Any] = {
        "manifest_version": manifest_version,
        "release_version": release_version,
        "platform": {"os": operating_system, "arch": architecture},
        "executables": {
            "cli": {"path": cli_path, "sha256": hashlib.sha256(files[cli_path]).hexdigest()},
            "service": {
                "path": service_path,
                "sha256": hashlib.sha256(files[service_path]).hexdigest(),
            },
        },
        "inventory": inventory,
        "compatibility": {
            "minimum_bootstrap_contract": compatibility[0],
            "maximum_bootstrap_contract": compatibility[1],
        },
        "signing": {"key_id": signing_key_id or key_id, "algorithm": SIGNATURE_ALGORITHM},
    }
    manifest.update(manifest_extra or {})
    identity = payload_identity(manifest)
    manifest["payload_identity"] = identity

    signer = _private(sign_with or key_id)
    message = signed_manifest_bytes(sign_over or manifest, payload_identity(sign_over or manifest))
    signature = {
        "signature_version": RELEASE_SIGNATURE_VERSION,
        "key_id": manifest["signing"]["key_id"],
        "algorithm": SIGNATURE_ALGORITHM,
        "payload_identity": identity,
        "signature": base64.b64encode(signer.sign(message)).decode("ascii"),
    }
    return Payload(
        release_version=release_version,
        operating_system=operating_system,
        files=files,
        manifest=manifest,
        signature=signature,
    )


# --------------------------------------------------------------------------
# Installation trees
# --------------------------------------------------------------------------

#: Modes a hardened installation carries. Parents of a candidate stay
#: owner-writable: a candidate is published into them by rename.
DIR_OPEN: Final = "0700"
DIR_SEALED: Final = "0500"
FILE_SEALED: Final = "0400"
FILE_EXECUTABLE: Final = "0500"


def _file(path: str, content: bytes, mode: str = FILE_SEALED) -> dict[str, Any]:
    return {
        "path": path,
        "kind": "file",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "mode": mode,
    }


def _directory(path: str, mode: str) -> dict[str, Any]:
    return {"path": path, "kind": "directory", "mode": mode}


def _symlink(path: str, target: str) -> dict[str, Any]:
    return {"path": path, "kind": "symlink", "target": target}


def candidate_entries(
    payload: Payload,
    *,
    directory: str | None = None,
    omit: tuple[str, ...] = (),
    replace_content: dict[str, bytes] | None = None,
    replace_modes: dict[str, str] | None = None,
    symlinks: dict[str, str] | None = None,
    root_mode: str = DIR_SEALED,
    manifest_bytes: bytes | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The installation entries for one candidate, and its relative directory."""
    relative = directory or f"runtimes/{payload.release_version}/{payload.identity_hex}"
    swapped = replace_content or {}
    modes = replace_modes or {}
    entries: list[dict[str, Any]] = [
        _directory("runtimes", DIR_OPEN),
        _directory(f"runtimes/{payload.release_version}", DIR_OPEN),
    ]
    members: dict[str, bytes] = {
        RUNTIME_MANIFEST_NAME: manifest_bytes or payload.manifest_bytes,
        RUNTIME_SIGNATURE_NAME: payload.signature_bytes,
        **payload.files,
    }
    executables = {
        payload.manifest["executables"]["cli"]["path"],
        payload.manifest["executables"]["service"]["path"],
    }
    for name, content in sorted(members.items()):
        if name in omit:
            continue
        mode = modes.get(name, FILE_EXECUTABLE if name in executables else FILE_SEALED)
        entries.append(_file(f"{relative}/{name}", swapped.get(name, content), mode))
    for name, target in (symlinks or {}).items():
        entries.append(_symlink(f"{relative}/{name}", target))

    # Every ancestor, not only the immediate parent: a directory the tree needs but
    # nobody declares is created with whatever the host's umask gives it, which on a
    # hardened payload is a writable directory the installation policy then refuses.
    nested: set[str] = set()
    for name in members:
        parts = name.split("/")[:-1]
        nested.update("/".join(parts[: index + 1]) for index in range(len(parts)))
    entries.extend(
        _directory(f"{relative}/{child}", modes.get(f"dir:{child}", DIR_SEALED))
        for child in sorted(nested)
    )
    entries.append(_directory(relative, root_mode))
    return relative, entries


def selection(payload: Payload, *, relative_path: str | None = None) -> dict[str, Any]:
    """The `active.json` record, exactly as `SharedRuntimeInstallation` writes it."""
    derived = f"runtimes/{payload.release_version}/{payload.identity_hex}"
    return {
        "schema_version": 1,
        "release_version": payload.release_version,
        "payload_digest": payload.identity_hex,
        "relative_path": relative_path or derived,
    }


def _selection_bytes(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def case(
    case_id: str,
    description: str,
    *,
    entries: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    refusal: str | None,
    descriptor: dict[str, Any] | None = None,
    verification_time: str = NOW,
    bootstrap_contract_version: str = BOOTSTRAP_CONTRACT_VERSION,
    minimum_release_version: str | None = None,
    host_operating_system: str = "macos",
    host_architecture: str = "arm64",
    posix_modes: bool = False,
    symlinks: bool = False,
) -> dict[str, Any]:
    expected: dict[str, Any] = {
        "outcome": "refused" if refusal else "verified",
        "refusal": refusal,
    }
    if descriptor is not None:
        expected["descriptor"] = descriptor
    return {
        "case_version": CASE_VERSION,
        "case_id": case_id,
        "description": description,
        "requires": {"posix_modes": posix_modes, "symlinks": symlinks},
        "verification_time": verification_time,
        "bootstrap_contract_version": bootstrap_contract_version,
        "minimum_release_version": minimum_release_version,
        "host_operating_system": host_operating_system,
        "host_architecture": host_architecture,
        "trust_anchors": anchors,
        "installation": {"entries": entries},
        "expected": expected,
    }


def _descriptor(payload: Payload, relative: str) -> dict[str, Any]:
    cli, service = EXECUTABLE_LAYOUT[payload.operating_system]
    return {
        "runtime_descriptor_version": RUNTIME_DESCRIPTOR_VERSION,
        "release_version": payload.release_version,
        "payload_identity": payload.manifest["payload_identity"],
        "runtime_root": relative,
        "cli_path": f"{relative}/{cli}",
        "service_path": f"{relative}/{service}",
    }


# --------------------------------------------------------------------------
# The corpus
# --------------------------------------------------------------------------


def build_cases() -> list[dict[str, Any]]:
    """Every case the handoff's sections 4.5 and 7.1 name, and nothing speculative."""
    approved = [anchor("test-only-release-2026a")]
    baseline = build_payload()
    other = build_payload(release_version=OTHER_RELEASE)
    cases: list[dict[str, Any]] = []

    def with_selection(
        payload: Payload,
        entries: list[dict[str, Any]],
        *,
        relative_path: str | None = None,
    ) -> list[dict[str, Any]]:
        record = selection(payload, relative_path=relative_path)
        return [_file("active.json", _selection_bytes(record), "0600"), *entries]

    # --- valid --------------------------------------------------------------
    relative, entries = candidate_entries(baseline)
    cases.append(
        case(
            "valid-baseline",
            "A correctly signed packaged runtime resolves the paired executable paths.",
            entries=with_selection(baseline, entries),
            anchors=approved,
            refusal=None,
            descriptor=_descriptor(baseline, relative),
            posix_modes=True,
        )
    )

    windows = build_payload(operating_system="windows", architecture="x86_64")
    windows_relative, windows_entries = candidate_entries(windows)
    cases.append(
        case(
            "valid-windows-layout",
            "The fixed Windows executable layout verifies and resolves the .exe pair.",
            entries=with_selection(windows, windows_entries),
            anchors=approved,
            refusal=None,
            descriptor=_descriptor(windows, windows_relative),
            host_operating_system="windows",
            host_architecture="x86_64",
        )
    )

    rotated = build_payload(key_id="test-only-release-2026b")
    rotated_relative, rotated_entries = candidate_entries(rotated)
    cases.append(
        case(
            "valid-key-rotation",
            "Overlapping approved anchors accept a release signed by the new key.",
            entries=with_selection(rotated, rotated_entries),
            anchors=[
                anchor(
                    "test-only-release-2026a",
                    window=("2026-01-01T00:00:00Z", "2026-12-01T00:00:00Z"),
                ),
                anchor(
                    "test-only-release-2026b",
                    window=("2026-06-01T00:00:00Z", "2027-06-01T00:00:00Z"),
                ),
            ],
            refusal=None,
            descriptor=_descriptor(rotated, rotated_relative),
        )
    )

    # --- untrusted ----------------------------------------------------------
    forged = build_payload(sign_over=other.manifest)
    cases.append(
        case(
            "wrong-release-signature",
            "A signature made over another manifest does not verify against this one.",
            entries=with_selection(forged, candidate_entries(forged)[1]),
            anchors=approved,
            refusal="runtime_untrusted",
        )
    )

    unapproved = build_payload(key_id="test-only-unapproved")
    cases.append(
        case(
            "unknown-signing-key",
            "A valid integrity digest with an unapproved issuer is refused.",
            entries=with_selection(unapproved, candidate_entries(unapproved)[1]),
            anchors=approved,
            refusal="runtime_untrusted",
        )
    )

    for case_id, description, key_anchor in (
        (
            "retired-signing-key",
            "A key retired before the verification instant is refused inside its window.",
            anchor("test-only-release-2026a", retired_at="2026-06-01T00:00:00Z"),
        ),
        (
            "expired-signing-key",
            "A key whose window closed before the verification instant is refused.",
            anchor(
                "test-only-release-2026a",
                window=("2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            ),
        ),
        (
            "not-yet-valid-signing-key",
            "A key whose window has not opened at the verification instant is refused.",
            anchor(
                "test-only-release-2026a",
                window=("2027-01-01T00:00:00Z", "2028-01-01T00:00:00Z"),
            ),
        ),
    ):
        cases.append(
            case(
                case_id,
                description,
                entries=with_selection(baseline, candidate_entries(baseline)[1]),
                anchors=[key_anchor],
                refusal="runtime_untrusted",
            )
        )

    # --- tampered -----------------------------------------------------------
    service_path = EXECUTABLE_LAYOUT["macos"][1]
    original = baseline.files[service_path]
    flipped = original[:-2] + bytes([original[-2] ^ 0x01]) + original[-1:]
    cases.append(
        case(
            "executable-digest-mismatch",
            "A single-byte change to an executable is refused before any process runs.",
            entries=with_selection(
                baseline,
                candidate_entries(baseline, replace_content={service_path: flipped})[1],
            ),
            anchors=approved,
            refusal="runtime_tampered",
        )
    )

    cli_path = EXECUTABLE_LAYOUT["macos"][0]
    cases.append(
        case(
            "split-executable-pair",
            "Two executables from different payloads never resolve as one pair.",
            entries=with_selection(
                baseline,
                candidate_entries(baseline, replace_content={cli_path: other.files[cli_path]})[1],
            ),
            anchors=approved,
            refusal="runtime_tampered",
        )
    )

    cases.append(
        case(
            "missing-inventory-member",
            "An inventoried file absent from the payload is refused.",
            entries=with_selection(
                baseline, candidate_entries(baseline, omit=("lib/omnivia/release.txt",))[1]
            ),
            anchors=approved,
            refusal="runtime_tampered",
        )
    )

    cases.append(
        case(
            "extra-inventory-member",
            "A payload file nobody signed is refused rather than ignored.",
            entries=[
                *with_selection(baseline, candidate_entries(baseline)[1]),
                _file(f"{relative}/lib/omnivia/injected.txt", b"unsigned\n"),
            ],
            anchors=approved,
            refusal="runtime_tampered",
        )
    )

    cases.append(
        case(
            "wrong-payload-directory-identity",
            "A valid payload under another payload's digest directory is refused.",
            entries=[
                _file(
                    "active.json",
                    _selection_bytes(
                        {
                            "schema_version": 1,
                            "release_version": baseline.release_version,
                            "payload_digest": other.identity_hex,
                            "relative_path": (
                                f"runtimes/{baseline.release_version}/{other.identity_hex}"
                            ),
                        }
                    ),
                    "0600",
                ),
                *candidate_entries(
                    baseline,
                    directory=f"runtimes/{baseline.release_version}/{other.identity_hex}",
                )[1],
            ],
            anchors=approved,
            refusal="runtime_tampered",
        )
    )

    # --- metadata -----------------------------------------------------------
    cases.append(
        case(
            "active-record-path-traversal",
            "An escaping relative_path in the active record cannot leave the root.",
            entries=with_selection(
                baseline, candidate_entries(baseline)[1], relative_path="../../outside"
            ),
            anchors=approved,
            refusal="runtime_metadata_invalid",
        )
    )

    unsupported = build_payload(manifest_version="2.0")
    cases.append(
        case(
            "unsupported-manifest-version",
            "An unsupported manifest schema version is refused before trust is decided.",
            entries=with_selection(unsupported, candidate_entries(unsupported)[1]),
            anchors=approved,
            refusal="runtime_metadata_invalid",
        )
    )

    widened = build_payload(manifest_extra={"telemetry_endpoint": "https://example.invalid"})
    cases.append(
        case(
            "manifest-unknown-member",
            "An unknown manifest member is refused rather than silently trusted.",
            entries=with_selection(widened, candidate_entries(widened)[1]),
            anchors=approved,
            refusal="runtime_metadata_invalid",
        )
    )

    duplicated = baseline.manifest_bytes.replace(
        b'"manifest_version":"1.0"', b'"manifest_version":"1.0","manifest_version":"2.0"', 1
    )
    cases.append(
        case(
            "duplicate-manifest-key",
            "A duplicated manifest member is refused rather than resolved to one value.",
            entries=with_selection(
                baseline, candidate_entries(baseline, manifest_bytes=duplicated)[1]
            ),
            anchors=approved,
            refusal="runtime_metadata_invalid",
        )
    )

    cases.append(
        case(
            "no-active-selection",
            "No active record at all is not installed, not a trust failure.",
            entries=candidate_entries(baseline)[1],
            anchors=approved,
            refusal="runtime_not_installed",
        )
    )

    cases.append(
        case(
            "active-selection-without-candidate",
            "A selector naming an absent candidate is a bounded retry, not a reinstall.",
            entries=[
                _file("active.json", _selection_bytes(selection(baseline)), "0600"),
                _directory("runtimes", DIR_OPEN),
            ],
            anchors=approved,
            refusal="runtime_busy",
        )
    )

    # --- layout -------------------------------------------------------------
    relocated = build_payload(executable_paths=("bin/omnivia", "bin/omnivia-service-relocated"))
    cases.append(
        case(
            "metadata-supplied-executable-path",
            "Metadata cannot move an executable off the fixed layout.",
            entries=with_selection(relocated, candidate_entries(relocated)[1]),
            anchors=approved,
            refusal="runtime_layout_invalid",
        )
    )

    cases.append(
        case(
            "symlinked-active-selection",
            "A symlinked installation selector is refused before it is read.",
            entries=[
                _file(
                    "selectors/active.json", _selection_bytes(selection(baseline)), "0600"
                ),
                _symlink("active.json", "selectors/active.json"),
                *entries,
            ],
            anchors=approved,
            refusal="runtime_layout_invalid",
            symlinks=True,
        )
    )

    cases.append(
        case(
            "symlinked-candidate",
            "A symlinked candidate directory is refused at the installation boundary.",
            entries=[
                _file("active.json", _selection_bytes(selection(baseline)), "0600"),
                *candidate_entries(baseline, directory="payloads/real")[1],
                _symlink(relative, "../../payloads/real"),
            ],
            anchors=approved,
            refusal="runtime_layout_invalid",
            symlinks=True,
        )
    )

    cases.append(
        case(
            "symlinked-manifest",
            "A symlinked payload manifest is refused before it is read.",
            entries=with_selection(
                baseline,
                candidate_entries(
                    baseline,
                    omit=(RUNTIME_MANIFEST_NAME,),
                    symlinks={RUNTIME_MANIFEST_NAME: "../../../elsewhere.json"},
                )[1],
            ),
            anchors=approved,
            refusal="runtime_layout_invalid",
            symlinks=True,
        )
    )

    cases.append(
        case(
            "symlinked-executable",
            "A symlinked executable is refused before it is digested.",
            entries=with_selection(
                baseline,
                candidate_entries(
                    baseline,
                    omit=(service_path,),
                    symlinks={service_path: "../../../../../bin/sh"},
                )[1],
            ),
            anchors=approved,
            refusal="runtime_layout_invalid",
            symlinks=True,
        )
    )

    cases.append(
        case(
            "world-writable-executable",
            "An executable anybody may rewrite is refused by the installation policy.",
            entries=with_selection(
                baseline, candidate_entries(baseline, replace_modes={service_path: "0755"})[1]
            ),
            anchors=approved,
            refusal="runtime_layout_invalid",
            posix_modes=True,
        )
    )

    cases.append(
        case(
            "writable-payload-directory",
            "A payload directory that still admits new entries is refused.",
            entries=with_selection(
                baseline, candidate_entries(baseline, root_mode=DIR_OPEN)[1]
            ),
            anchors=approved,
            refusal="runtime_layout_invalid",
            posix_modes=True,
        )
    )

    # --- compatibility ------------------------------------------------------
    cases.append(
        case(
            "incompatible-core-release",
            "An authentic release below the consumer's floor is a compatibility refusal.",
            entries=with_selection(baseline, candidate_entries(baseline)[1]),
            anchors=approved,
            refusal="runtime_incompatible",
            minimum_release_version="9.9.9",
        )
    )

    foreign_arch = build_payload(architecture="x86_64")
    cases.append(
        case(
            "wrong-payload-architecture",
            "A correctly signed payload built for the other architecture is refused.",
            entries=with_selection(foreign_arch, candidate_entries(foreign_arch)[1]),
            anchors=approved,
            refusal="runtime_incompatible",
            host_architecture="arm64",
        )
    )

    future = build_payload(compatibility=("2.0", "2.0"))
    cases.append(
        case(
            "incompatible-bootstrap-contract",
            "A payload serving only a later bootstrap contract is refused, not executed.",
            entries=with_selection(future, candidate_entries(future)[1]),
            anchors=approved,
            refusal="runtime_incompatible",
        )
    )

    identifiers = [entry["case_id"] for entry in cases]
    if len(set(identifiers)) != len(identifiers):
        raise SystemExit(f"duplicate case ids: {sorted(identifiers)}")
    return cases


def build_vectors() -> dict[str, Any]:
    """Canonicalisation, identity and signature vectors, as raw text throughout.

    Raw text and not parsed values: a parsed value cannot carry a duplicate name, and
    a language whose only number is a double cannot tell ``1`` from ``1.0``. Both are
    exactly what these vectors are for.
    """
    accepted = [
        ("sorted-object-names", '{"b":1,"a":2,"C":3}'),
        ("nested-objects-and-arrays", '{"outer": {"z": [1, 2, {"y": true, "x": null}]}}'),
        ("non-ascii-is-escaped", '{"k\\u00e9y": "v\\u00e4lue"}'),
        ("empty-containers", '{"a": {}, "b": []}'),
        ("array-order-is-significant", '{"a": [3, 1, 2]}'),
        ("negative-and-zero-integers", '{"a": -1, "b": 0, "c": 1}'),
    ]
    rejected = [
        ("duplicate-object-name", '{"a": 1, "a": 2}', "duplicate object name"),
        ("floating-point-literal", '{"a": 1.5}', "floats are outside the canonical subset"),
        ("integral-float-literal", '{"a": 1.0}', "floats are outside the canonical subset"),
        ("not-a-number", '{"a": NaN}', "NaN is not JSON"),
        ("infinity", '{"a": Infinity}', "Infinity is not JSON"),
    ]
    canonicalisation: list[dict[str, Any]] = []
    for name, text in accepted:
        canonical = canonical_json(json.loads(text))
        canonicalisation.append(
            {
                "name": name,
                "json": text,
                "accepted": True,
                "canonical": canonical.decode("ascii"),
                "sha256": hashlib.sha256(canonical).hexdigest(),
            }
        )
    canonicalisation.extend(
        {"name": name, "json": text, "accepted": False, "reason": reason}
        for name, text, reason in rejected
    )

    baseline = build_payload()
    claim = {
        name: value
        for name, value in baseline.manifest.items()
        if name != "payload_identity"
    }
    identity_message = IDENTITY_DOMAIN.encode("ascii") + b"\x00" + canonical_json(claim)
    identity = [
        {
            "name": "baseline-payload-manifest",
            "manifest_json": baseline.manifest_bytes.decode("ascii").rstrip("\n"),
            "identity_claim_sha256": hashlib.sha256(identity_message).hexdigest(),
            "payload_identity": baseline.manifest["payload_identity"],
        }
    ]

    signed = signed_manifest_bytes(baseline.manifest, baseline.manifest["payload_identity"])
    other = build_payload(release_version=OTHER_RELEASE)
    signatures = [
        {
            "name": "valid-baseline-signature",
            "public_key": _public_b64("test-only-release-2026a"),
            "signed_message_base64": base64.b64encode(signed).decode("ascii"),
            "signature": baseline.signature["signature"],
            "valid": True,
        },
        {
            "name": "signature-over-another-manifest",
            "public_key": _public_b64("test-only-release-2026a"),
            "signed_message_base64": base64.b64encode(signed).decode("ascii"),
            "signature": other.signature["signature"],
            "valid": False,
        },
        {
            "name": "signature-by-an-unapproved-key",
            "public_key": _public_b64("test-only-release-2026a"),
            "signed_message_base64": base64.b64encode(signed).decode("ascii"),
            "signature": base64.b64encode(_private("test-only-unapproved").sign(signed)).decode(
                "ascii"
            ),
            "valid": False,
        },
    ]
    return {
        "vectors_version": VECTORS_VERSION,
        "identity_domain": IDENTITY_DOMAIN,
        "signature_domain": SIGNATURE_DOMAIN,
        "canonicalisation": canonicalisation,
        "identity": identity,
        "signatures": signatures,
    }


# --------------------------------------------------------------------------
# Emit / check
# --------------------------------------------------------------------------


def _rendered() -> dict[Path, bytes]:
    documents: dict[Path, bytes] = {}
    for entry in build_cases():
        directory = "valid" if entry["expected"]["outcome"] == "verified" else "invalid"
        path = FIXTURES_ROOT / directory / f"{entry['case_id']}.json"
        documents[path] = (json.dumps(entry, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
            "ascii"
        )
    vectors = FIXTURES_ROOT / "vectors" / "canonicalisation-and-signatures.json"
    documents[vectors] = (
        json.dumps(build_vectors(), ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")
    return documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed fixtures match the generator instead of writing them",
    )
    arguments = parser.parse_args(argv)
    documents = _rendered()

    expected = {path.relative_to(FIXTURES_ROOT).as_posix() for path in documents}
    present = {
        path.relative_to(FIXTURES_ROOT).as_posix()
        for path in FIXTURES_ROOT.rglob("*.json")
        if path.is_file()
    }

    if arguments.check:
        findings = [f"{name}: committed but not generated" for name in sorted(present - expected)]
        findings += [f"{name}: generated but not committed" for name in sorted(expected - present)]
        findings += [
            f"{path.relative_to(FIXTURES_ROOT).as_posix()}: committed bytes differ"
            for path, content in sorted(documents.items())
            if path.is_file() and path.read_bytes() != content
        ]
        if findings:
            print("\n".join(findings), file=sys.stderr)
            print(
                "\nRun `python scripts/generate-runtime-contract.py` and review the diff.",
                file=sys.stderr,
            )
            return 1
        print(f"{len(documents)} trusted-runtime fixture(s) match the generator.")
        return 0

    for name in sorted(present - expected):
        (FIXTURES_ROOT / name).unlink()
    for path, content in sorted(documents.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print(f"wrote {len(documents)} trusted-runtime fixture(s) under {FIXTURES_ROOT}")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry
    raise SystemExit(main())
