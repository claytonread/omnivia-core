"""The trusted-runtime v1 conformance corpus, run against the reference verifier.

Every case under ``contracts/runtime/v1/fixtures`` is self-contained: it carries the
installation tree to materialise, the approved trust anchors, the verification
instant, the consumer's bounds, and the verdict it expects. This module materialises
each one under a fresh absolute root and asserts
``omnivia_core_runtime.distribution.trusted_runtime`` produces exactly that verdict.

The point of the corpus is that this file is *replaceable*. Platform implements the
same verifier in TypeScript, runs the same JSON, and must agree case for case --
which is only a meaningful claim if the cases say what they expect rather than
leaving it to whichever runner reads them. That is why the expected outcome travels
inside the case document and not in a table beside it.

Two capability gates, declared per case rather than inferred here: ``posix_modes``
for the cases whose verdict depends on owner and mode enforcement, and ``symlinks``
for the cases that need one. A host that cannot provide one skips that case rather
than reporting a verdict it did not observe.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator
from omnivia_core_runtime.distribution.trusted_runtime import (
    IDENTITY_DOMAIN,
    SIGNATURE_DOMAIN,
    CanonicalJsonError,
    RuntimeRefusal,
    RuntimeResolutionError,
    TrustAnchor,
    VerifiedRuntime,
    canonical_json,
    parse_canonical_document,
    parse_manifest,
    payload_identity,
    resolve_runtime,
    unharden_tree,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_ROOT = REPO_ROOT / "contracts" / "runtime" / "v1"
SCHEMA_PATH = CANONICAL_ROOT / "schemas" / "trusted-runtime-v1.schema.json"
FIXTURES_ROOT = CANONICAL_ROOT / "fixtures"
VECTORS_PATH = FIXTURES_ROOT / "vectors" / "canonicalisation-and-signatures.json"

CASE_PATHS = sorted(
    path
    for directory in ("valid", "invalid")
    for path in (FIXTURES_ROOT / directory).glob("*.json")
)


def _case(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _symlinks_available(tmp_path: Path) -> bool:
    probe = tmp_path / ".symlink-probe"
    try:
        probe.symlink_to("target")
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


def _materialise(case: dict[str, Any], root: Path) -> None:
    """Build the case's installation tree under `root`.

    Contents first and modes last, in two passes, because a directory hardened to
    ``0500`` admits no new entry -- so a single pass that applied a mode as it went
    could not create the file beside it. Directory modes are applied deepest-first
    for the same reason in reverse.
    """
    entries = case["installation"]["entries"]
    for entry in entries:
        target = root / PurePosixPath(entry["path"])
        if entry["kind"] == "directory":
            target.mkdir(parents=True, exist_ok=True)
        elif entry["kind"] == "file":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(entry["content_base64"]))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(entry["target"])

    for entry in entries:
        if entry["kind"] == "file" and "mode" in entry:
            os.chmod(root / PurePosixPath(entry["path"]), int(entry["mode"], 8))
    directories = [entry for entry in entries if entry["kind"] == "directory" and "mode" in entry]
    for entry in sorted(
        directories, key=lambda item: len(PurePosixPath(item["path"]).parts), reverse=True
    ):
        os.chmod(root / PurePosixPath(entry["path"]), int(entry["mode"], 8))


def _resolve(case: dict[str, Any], root: Path) -> VerifiedRuntime:
    """Resolve `root` exactly as the case specifies. Refusals propagate."""
    return resolve_runtime(
        installation_root=root,
        trust_anchors=[TrustAnchor.from_document(entry) for entry in case["trust_anchors"]],
        verification_time=datetime.fromisoformat(case["verification_time"]),
        bootstrap_contract_version=case["bootstrap_contract_version"],
        minimum_release_version=case["minimum_release_version"],
        host_operating_system=case["host_operating_system"],
        host_architecture=case["host_architecture"],
    )


def _observed(case: dict[str, Any], root: Path) -> dict[str, Any]:
    """The case's verdict in the corpus's own vocabulary."""
    try:
        verified = _resolve(case, root)
    except RuntimeResolutionError as refusal:
        return {"outcome": "refused", "refusal": refusal.refusal.value}
    return {
        "outcome": "verified",
        "refusal": None,
        "descriptor": {
            **verified.to_dict(),
            "runtime_root": verified.runtime_root.relative_to(root).as_posix(),
            "cli_path": verified.cli_path.relative_to(root).as_posix(),
            "service_path": verified.service_path.relative_to(root).as_posix(),
        },
    }


# --------------------------------------------------------------------------
# The corpus
# --------------------------------------------------------------------------


def test_the_corpus_is_not_empty_and_covers_every_refusal_code() -> None:
    """A corpus that stopped covering a code would still pass every case in it.

    `runtime_io_failure` is the one code with no fixture and cannot have one: it
    means a bounded local read failed, which is a property of the host rather than
    of any tree a case can describe. It is exercised directly in
    `packages/omnivia-core-runtime/tests/phase3/runtime/test_trusted_runtime.py`.
    """
    assert len(CASE_PATHS) >= 20
    covered = {
        _case(path)["expected"]["refusal"]
        for path in CASE_PATHS
        if _case(path)["expected"]["outcome"] == "refused"
    }
    assert covered == {
        member.value for member in RuntimeRefusal if member is not RuntimeRefusal.IO_FAILURE
    }
    assert any(_case(path)["expected"]["outcome"] == "verified" for path in CASE_PATHS)


def test_every_case_and_the_vectors_validate_against_the_published_schema() -> None:
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    for path in (*CASE_PATHS, VECTORS_PATH):
        errors = sorted(validator.iter_errors(json.loads(path.read_text(encoding="utf-8"))))
        assert not errors, f"{path.name}: {[error.message for error in errors[:3]]}"


def test_a_case_lives_in_the_directory_its_expected_outcome_names() -> None:
    """`valid/` and `invalid/` are derived, so a case cannot disagree with its folder."""
    for path in CASE_PATHS:
        expected = "valid" if _case(path)["expected"]["outcome"] == "verified" else "invalid"
        assert path.parent.name == expected, path.name
        assert path.stem == _case(path)["case_id"], path.name


@pytest.mark.parametrize("path", CASE_PATHS, ids=lambda path: path.stem)
def test_the_reference_verifier_agrees_with_every_conformance_case(
    path: Path, tmp_path: Path
) -> None:
    case = _case(path)
    if case["requires"]["posix_modes"] and os.name == "nt":
        pytest.skip("the case's verdict depends on POSIX owner and mode enforcement")
    if case["requires"]["symlinks"] and not _symlinks_available(tmp_path):
        pytest.skip("this host cannot create symlinks")

    root = tmp_path / "installation"
    root.mkdir()
    try:
        _materialise(case, root)
        observed = _observed(case, root)
    finally:
        unharden_tree(root)

    assert observed["outcome"] == case["expected"]["outcome"], case["description"]
    assert observed["refusal"] == case["expected"]["refusal"], case["description"]
    if "descriptor" in case["expected"]:
        assert observed["descriptor"] == case["expected"]["descriptor"]


@pytest.mark.parametrize("path", CASE_PATHS, ids=lambda path: path.stem)
def test_a_refusal_carries_no_path_and_no_payload(path: Path, tmp_path: Path) -> None:
    """The refusal document is one closed code and a version. Nothing else may ride out.

    Asserted over the whole corpus rather than at one call site, because "no path
    leaks" is a property of every refusing branch and a new branch that formatted a
    filename into a message would pass a test that only looked at one of them.
    """
    case = _case(path)
    if case["expected"]["outcome"] != "refused":
        pytest.skip("this case verifies")
    if case["requires"]["posix_modes"] and os.name == "nt":
        pytest.skip("the case's verdict depends on POSIX owner and mode enforcement")
    if case["requires"]["symlinks"] and not _symlinks_available(tmp_path):
        pytest.skip("this host cannot create symlinks")

    root = tmp_path / "installation"
    root.mkdir()
    try:
        _materialise(case, root)
        with pytest.raises(RuntimeResolutionError) as raised:
            _resolve(case, root)
    finally:
        unharden_tree(root)

    document = raised.value.to_dict()
    assert set(document) == {"runtime_descriptor_version", "refusal"}
    assert document["refusal"] == case["expected"]["refusal"]
    assert str(raised.value) == case["expected"]["refusal"]
    assert str(root) not in json.dumps(document)


# --------------------------------------------------------------------------
# Canonicalisation, identity and signature vectors
# --------------------------------------------------------------------------


def _vectors() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    return document


def test_the_vectors_pin_the_two_domain_separators() -> None:
    """A verifier that agreed on canonical bytes but not on the prefix would compute
    a different identity for the same manifest and never find out why."""
    vectors = _vectors()
    assert vectors["identity_domain"] == IDENTITY_DOMAIN
    assert vectors["signature_domain"] == SIGNATURE_DOMAIN
    assert IDENTITY_DOMAIN != SIGNATURE_DOMAIN


def test_every_canonicalisation_vector_reproduces_its_declared_bytes() -> None:
    for vector in _vectors()["canonicalisation"]:
        raw = vector["json"].encode("utf-8")
        if not vector["accepted"]:
            with pytest.raises(CanonicalJsonError):
                canonical_json(parse_canonical_document(raw, limit=4096))
            continue
        canonical = canonical_json(parse_canonical_document(raw, limit=4096))
        assert canonical.decode("ascii") == vector["canonical"], vector["name"]
        assert hashlib.sha256(canonical).hexdigest() == vector["sha256"], vector["name"]


def test_every_identity_vector_reproduces_its_declared_payload_identity() -> None:
    for vector in _vectors()["identity"]:
        manifest = parse_canonical_document(
            vector["manifest_json"].encode("utf-8"), limit=256 * 1024
        )
        claim = {name: value for name, value in manifest.items() if name != "payload_identity"}
        message = IDENTITY_DOMAIN.encode("ascii") + b"\x00" + canonical_json(claim)
        assert hashlib.sha256(message).hexdigest() == vector["identity_claim_sha256"]
        assert payload_identity(manifest) == vector["payload_identity"]
        assert manifest["payload_identity"] == vector["payload_identity"]


def test_every_signature_vector_verifies_exactly_as_declared() -> None:
    for vector in _vectors()["signatures"]:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(vector["public_key"]))
        message = base64.b64decode(vector["signed_message_base64"])
        signature = base64.b64decode(vector["signature"])
        if vector["valid"]:
            key.verify(signature, message)
            continue
        with pytest.raises(InvalidSignature):
            key.verify(signature, message)


def test_the_published_test_keys_are_labelled_as_test_only() -> None:
    """A fixture key must never be mistakable for a release key.

    The corpus is public and regenerable, so its keys are worthless -- but only if
    nobody reads one as an approved issuer. Every key identifier in it says so in
    its own name.
    """
    identifiers = {
        anchor["key_id"] for path in CASE_PATHS for anchor in _case(path)["trust_anchors"]
    }
    assert identifiers
    assert all(identifier.startswith("test-only-") for identifier in identifiers), identifiers


def test_the_corpus_carries_no_private_key_material() -> None:
    """The vectors publish public keys, messages and signatures, and nothing else.

    A signature vector needs no secret to check, so the corpus holds none. The seeds
    the generator derives its test keys from live in
    `scripts/generate-runtime-contract.py`, are `sha256` of a published label, and
    exist so the corpus can be regenerated -- not so anything can be signed with
    authority.
    """
    for vector in _vectors()["signatures"]:
        assert set(vector) <= {
            "name",
            "public_key",
            "signed_message_base64",
            "signature",
            "valid",
        }
    text = VECTORS_PATH.read_text(encoding="utf-8")
    for forbidden in ("private", "secret", "seed", "BEGIN "):
        assert forbidden not in text, forbidden


def test_the_corpus_files_stay_regular_and_bounded() -> None:
    """A fixture tree is read by other languages' test harnesses; keep it plain."""
    for path in sorted(FIXTURES_ROOT.rglob("*")):
        assert not path.is_symlink(), path
        if path.is_file():
            assert stat.S_ISREG(path.lstat().st_mode), path
            assert path.stat().st_size < 512 * 1024, path


# --------------------------------------------------------------------------
# The reference verifier is never looser than the schema it publishes
# --------------------------------------------------------------------------

#: Values the published `keyId` and `relativePath` definitions reject. Each is a
#: document Platform's verifier must refuse, so a reference verifier that accepted
#: one would be the two implementations disagreeing about the same bytes -- which is
#: the failure the corpus exists to make impossible, and which no corpus *case* can
#: catch, because a case can only carry documents the schema already admits.
_SCHEMA_REJECTED_KEY_IDS = ("", "-leading-dash", "k" * 129, "key id", "key/id", "ké")
_SCHEMA_REJECTED_MEMBER_PATHS = (
    "bin/om:nivia",
    "C:/absolute",
    "a*b",
    "a?b",
    'a"b',
    "a<b",
    "a>b",
    "a|b",
    "a\x01b",
    "a\x00b",
    "a\\b",
    "..\\escape",
    "/absolute",
    "../escape",
    "a/./b",
    "a//b",
    "a/..",
)
#: Versions the published `semver` and `contractVersion` definitions reject. Both
#: patterns spell their digits `[0-9]`, and the reference verifier used
#: `str.isdigit()` -- which is true of the Arabic-Indic `٠`, the Devanagari `०` and
#: the superscript `²`. So `٠.٦.٥` was a release version here and a schema violation
#: there, `١.٠` was a bootstrap-contract bound the same way, and `1.2.²` was neither:
#: `int()` raised a bare `ValueError` straight past the eight refusal codes.
_SCHEMA_REJECTED_SEMVERS = (
    "٠.٦.٥",
    "0.6.٥",
    "१.०.०",
    "1.2.²",
    "01.0.0",
    "1.0",
    "1.0.0.0",
    "0.6. 5",
    "v1.0.0",
    "1.0.0-rc1",
    "",
)
_SCHEMA_REJECTED_CONTRACT_VERSIONS = (
    "١.٠",
    "1.٠",
    "1.²",
    "01.0",
    "1",
    "1.0.0",
    "1. 0",
    "",
)


def _definition_validator(name: str) -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{name}"})


def _manifest_with(**overrides: Any) -> dict[str, Any]:
    """One structurally complete manifest, for probing a single member's policy."""
    manifest: dict[str, Any] = {
        "manifest_version": "1.0",
        "release_version": "0.6.5",
        "platform": {"os": "linux", "arch": "arm64"},
        "executables": {
            "cli": {"path": "bin/omnivia", "sha256": "0" * 64},
            "service": {"path": "bin/omnivia-core-service", "sha256": "0" * 64},
        },
        "inventory": [
            {"path": "bin/omnivia", "sha256": "0" * 64, "size": 1, "executable": True},
            {
                "path": "bin/omnivia-core-service",
                "sha256": "0" * 64,
                "size": 1,
                "executable": True,
            },
        ],
        "compatibility": {
            "minimum_bootstrap_contract": "1.0",
            "maximum_bootstrap_contract": "1.0",
        },
        "signing": {"key_id": "test-only-release-2026a", "algorithm": "ed25519"},
        "payload_identity": "sha256:" + "0" * 64,
    }
    manifest.update(overrides)
    return manifest


def test_the_baseline_probe_manifest_is_accepted_by_both() -> None:
    """The control. Without it the two tests below could pass on a broken probe."""
    assert not sorted(_definition_validator("payloadManifest").iter_errors(_manifest_with()))
    assert parse_manifest(_manifest_with())[0] == "0.6.5"


def test_no_key_id_the_schema_rejects_is_accepted_by_the_reference_verifier() -> None:
    """`signing.key_id` used to be checked with `isinstance(value, str)` and nothing
    else, so an empty, unbounded or space-carrying identifier verified here and was
    refused by the published schema."""
    validator = _definition_validator("keyId")
    for value in _SCHEMA_REJECTED_KEY_IDS:
        assert sorted(validator.iter_errors(value)), value
        with pytest.raises(RuntimeResolutionError) as raised:
            parse_manifest(_manifest_with(signing={"key_id": value, "algorithm": "ed25519"}))
        assert raised.value.refusal is RuntimeRefusal.METADATA_INVALID, value


def test_no_version_the_schema_rejects_is_accepted_by_the_reference_verifier() -> None:
    """`release_version` and both `compatibility` bounds, against their definitions."""
    validator = _definition_validator("semver")
    for value in _SCHEMA_REJECTED_SEMVERS:
        assert sorted(validator.iter_errors(value)), value
        with pytest.raises(RuntimeResolutionError) as raised:
            parse_manifest(_manifest_with(release_version=value))
        assert raised.value.refusal is RuntimeRefusal.METADATA_INVALID, value

    validator = _definition_validator("contractVersion")
    for value in _SCHEMA_REJECTED_CONTRACT_VERSIONS:
        assert sorted(validator.iter_errors(value)), value
        for bound in ("minimum_bootstrap_contract", "maximum_bootstrap_contract"):
            window = {
                "minimum_bootstrap_contract": "1.0",
                "maximum_bootstrap_contract": "1.0",
                bound: value,
            }
            with pytest.raises(RuntimeResolutionError) as raised:
                parse_manifest(_manifest_with(compatibility=window))
            assert raised.value.refusal is RuntimeRefusal.METADATA_INVALID, (bound, value)


def test_a_version_component_no_integer_can_hold_refuses_rather_than_raising() -> None:
    """The schema bounds no component, and CPython bounds `int(str)` at 4300 digits.

    A 4400-digit major version is therefore a document the schema admits, no
    implementation can order, and this one converted -- raising the same bare
    `ValueError` an oversized JSON integer used to raise, and past the same eight
    codes. Refusing it is fail-closed and stays inside the vocabulary.
    """
    with pytest.raises(RuntimeResolutionError) as raised:
        parse_manifest(_manifest_with(release_version="1" * 4400 + ".0.0"))
    assert raised.value.refusal is RuntimeRefusal.METADATA_INVALID


def test_no_member_path_the_schema_rejects_is_accepted_by_the_reference_verifier() -> None:
    """`relativePath` excludes a reserved set and every control character; the parse
    checked only the backslash, the NUL, a leading slash and a second-position colon,
    so `a:b` and `a|b` were inventory paths here and violations there."""
    validator = _definition_validator("relativePath")
    for value in _SCHEMA_REJECTED_MEMBER_PATHS:
        assert sorted(validator.iter_errors(value)), value
        entry = {"path": value, "sha256": "0" * 64, "size": 1, "executable": False}
        with pytest.raises(RuntimeResolutionError) as raised:
            parse_manifest(_manifest_with(inventory=[entry, *_manifest_with()["inventory"]]))
        assert raised.value.refusal in {
            RuntimeRefusal.METADATA_INVALID,
            RuntimeRefusal.LAYOUT_INVALID,
        }, value
