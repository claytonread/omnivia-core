"""The digest-only Laya pin survives into the signed world and re-derives its tree.

The D-8 allocation's first deliverable is a digest-only distribution manifest
(``docs/distribution/laya-typed-decisions-manifest-v1.json``) for the
owner-selected ``aac6fef/laya-typed-decisions-coreml`` payload at the pinned
revision. These tests pin the two properties the G-2 gate rests on:

1. **Forward compatibility with signing (D-1):** the digest-only draft, once a
   signing pair is attached, is accepted verbatim by the merged
   ``verify_model_manifest`` — adding the signature does not change any
   artifact digest, so the pin's meaning survives the signing step.
2. **The tree digest is re-derivable from the manifest alone:** the upstream
   ``artifacts.tree_digest`` rule (sha256 over sorted relative paths + raw
   per-file digest bytes, rooted at ``model.mlpackage``) recomputes the
   recorded ``517a8071…`` digest from the manifest's own artifact table. The
   recorded value was matched against fetched bytes on 2026-09-29 (the D-8
   component receipt); this test proves the rule that makes every future
   receipt re-derivable from the manifest without trusting the publisher's
   claim.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from omnivia_core_runtime.distribution.model_manifest_trust import (
    DEV_TEST_ANCHOR_FLAG,
    ModelTrustError,
    ModelTrustRefusal,
    model_manifest_identity,
    sign_model_manifest,
    verify_model_manifest,
)

MANIFEST_PATH = (
    Path(__file__).parents[5] / "docs/distribution/laya-typed-decisions-manifest-v1.json"
)
RECORDED_TREE_DIGEST = "517a8071290a29c2f1e19b38356fdc5c59dfdc801711a92606de6bba67596c85"
TREE_ROOT = "model.mlpackage"


@pytest.fixture
def manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_the_digest_only_draft_carries_the_expected_shape_and_pin(
    manifest: dict[str, object],
) -> None:
    assert manifest["schema_version"] == "model-manifest.v1"
    assert manifest["identifier"] == "core.laya-typed-decisions"
    assert manifest["version"] == "1.0.0"
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list) and len(artifacts) == 13
    total = sum(artifact["size"] for artifact in artifacts)  # type: ignore[index]
    assert total == 848_151_894
    # The identity is self-consistent: recomputing it over the document with the
    # identity member removed reproduces the recorded member.
    assert manifest["manifest_identity"] == model_manifest_identity(manifest)


def test_the_draft_with_a_dev_signature_is_accepted_by_the_merged_verifier(
    manifest: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D-1's signing step activates the existing verifier without re-pinning."""
    monkeypatch.setenv(DEV_TEST_ANCHOR_FLAG, "1")
    private_key = Ed25519PrivateKey.generate()
    anchor_public = private_key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )
    document = dict(manifest)
    # The publisher adds the signing member when signing (D-1): the artifact
    # digests are untouched, the identity is recomputed over the document that
    # now names the key.
    document["signing"] = {"key_id": "dev-test-model-anchor"}
    signature = sign_model_manifest(
        document, key_id="dev-test-model-anchor", private_key=private_key
    )
    document["manifest_identity"] = signature["manifest_identity"]
    from omnivia_core_runtime.distribution.trusted_runtime import TrustAnchor

    anchor = TrustAnchor(
        key_id="dev-test-model-anchor",
        public_key=anchor_public,
        not_before=datetime.now(UTC) - timedelta(days=1),
        not_after=datetime.now(UTC) + timedelta(days=1),
    )
    verified = verify_model_manifest(
        document, signature, trust_anchors=[anchor], verification_time=datetime.now(UTC)
    )
    assert verified.identifier == "core.laya-typed-decisions"
    # The artifact digests — the pin's substance — are identical to the
    # unsigned draft's. Signing changed no artifact digest.
    assert [artifact.sha256 for artifact in verified.artifacts] == [
        artifact["sha256"] for artifact in manifest["artifacts"]  # type: ignore[index]
    ]


def test_the_unsigned_draft_is_refused_by_the_verifier_until_signing_lands(
    manifest: dict[str, object],
) -> None:
    """The digest-only pin does not masquerade as verified trust."""
    private_key = Ed25519PrivateKey.generate()
    anchor_public = private_key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )
    from omnivia_core_runtime.distribution.trusted_runtime import TrustAnchor

    anchor = TrustAnchor(
        key_id="dev-test-model-anchor",
        public_key=anchor_public,
        not_before=datetime.now(UTC) - timedelta(days=1),
        not_after=datetime.now(UTC) + timedelta(days=1),
    )
    with pytest.raises(ModelTrustError) as error:
        verify_model_manifest(
            manifest,
            {"signature_version": "model-manifest-signature.v1"},
            trust_anchors=[anchor],
            verification_time=datetime.now(UTC),
        )
    assert error.value.refusal is ModelTrustRefusal.METADATA_INVALID


def test_the_recorded_tree_digest_rederives_from_the_manifests_artifact_table(
    manifest: dict[str, object],
) -> None:
    """The upstream tree_digest rule, over the manifest's own artifact table."""
    files: list[tuple[str, str]] = []
    for artifact in manifest["artifacts"]:  # type: ignore[union-attr]
        name = artifact["name"]  # type: ignore[index]
        if name.startswith(TREE_ROOT + "/"):
            files.append((name[len(TREE_ROOT) + 1 :], name and artifact["sha256"]))  # type: ignore[index]
    digest = hashlib.sha256()
    for relative_path, hex_digest in sorted(files):
        digest.update(relative_path.encode())
        digest.update(bytes.fromhex(hex_digest))
    assert digest.hexdigest() == RECORDED_TREE_DIGEST
    assert len(files) == 3
