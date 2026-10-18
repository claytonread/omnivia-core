"""Unit properties of the model-manifest verifier (SPEC-CORE-DEC-001 G-3 mechanism).

The handoff's demanded negative set, plus the properties that make the mechanism
honest: every refusal class is exercised against a real signed document, the
closed refusal vocabulary never leaks manifest contents, the identity excludes
itself from its own preimage, the dev/test anchor helper is inert without its
flag, and rotation is expressed by anchor windows exactly as the runtime payload
format expresses it.

Two document classes are distinguished deliberately. A *structural* defect (a bad
digest format, a duplicate artifact name, a wrong schema version) is refused
before any trust question, so its test mutates an already-signed document and
recomputes only the identity -- the signature document is never asked to cover
garbage. A *trust* defect (tampered content, unknown key, expired window) is
refused with a genuinely signed document that fails for the reason under test.

Nothing here mints a production anchor: every key in this file is an ephemeral
dev/test key minted under the flag, and every verdict it reaches says "the
mechanism works" and nothing more.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from omnivia_core_runtime.distribution.model_manifest_trust import (
    DEV_TEST_ANCHOR_FLAG,
    MODEL_SIGNATURE_VERSION,
    ModelTrustError,
    ModelTrustRefusal,
    dev_test_anchor,
    model_manifest_identity,
    sign_model_manifest,
    verify_model_manifest,
)
from omnivia_core_runtime.distribution.trusted_runtime import TrustAnchor

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
TEST_KEY_ID = "dev-test-model-anchor"
OTHER_KEY_ID = "another-dev-test-model-anchor"


def _keypair() -> tuple[Ed25519PrivateKey, TrustAnchor]:
    private_key = Ed25519PrivateKey.generate()
    anchor = TrustAnchor(
        key_id=TEST_KEY_ID,
        public_key=private_key.public_key().public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        ),
        not_before=NOW - timedelta(days=1),
        not_after=NOW + timedelta(days=365),
    )
    return private_key, anchor


def _manifest() -> dict[str, object]:
    return {
        "schema_version": "model-manifest.v1",
        "manifest_identity": "",
        "identifier": "core.laya-instruct",
        "version": "1.0.0",
        "runtime_contract_version": "1.3",
        "artifacts": [
            {"name": "weights.safetensors", "sha256": "ab" * 32, "size": 1024},
            {"name": "tokenizer.json", "sha256": "cd" * 32, "size": 512},
        ],
        "signing": {"key_id": TEST_KEY_ID},
    }


def _signed(
    *,
    private_key: Ed25519PrivateKey | None = None,
    key_id: str = TEST_KEY_ID,
) -> tuple[dict[str, object], dict[str, object]]:
    document = _manifest()
    document["signing"] = {"key_id": key_id}
    signer = private_key if private_key is not None else _keypair()[0]
    signature = sign_model_manifest(document, key_id=key_id, private_key=signer)
    document["manifest_identity"] = model_manifest_identity(document)
    return document, dict(signature)


def _structurally_mutated(
    mutate: Callable[[dict[str, object]], None],
) -> tuple[dict[str, object], dict[str, object]]:
    """An already-signed manifest mutated before identity recomputation.

    The signature document is the original good one and is never consulted: the
    structural validation the test targets runs first, by contract.
    """
    _private_key, _anchor = _keypair()
    manifest, signature = _signed(private_key=_private_key)
    mutate(manifest)
    manifest["manifest_identity"] = model_manifest_identity(manifest)
    return manifest, signature


def _verify(
    manifest: dict[str, object],
    signature: dict[str, object],
    *,
    anchors: list[TrustAnchor],
    **overrides: object,
) -> object:
    arguments: dict[str, object] = {
        "trust_anchors": anchors,
        "verification_time": NOW,
    }
    arguments.update(overrides)
    return verify_model_manifest(manifest, signature, **arguments)  # type: ignore[arg-type]


def _refusal(error: object) -> ModelTrustRefusal:
    actual = error.value if isinstance(error, pytest.ExceptionInfo) else error
    assert isinstance(actual, ModelTrustError), actual
    return actual.refusal


# --------------------------------------------------------------------------
# The vertical: one well-formed signed manifest verifies and names its pair
# --------------------------------------------------------------------------


def test_a_well_formed_signed_manifest_verifies_and_names_its_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DEV_TEST_ANCHOR_FLAG, "1")
    private_key, anchor = dev_test_anchor(
        not_before=NOW - timedelta(days=1), not_after=NOW + timedelta(days=365)
    )
    manifest, signature = _signed(private_key=private_key)
    verified = verify_model_manifest(
        manifest,
        signature,
        trust_anchors=[anchor],
        verification_time=NOW,
        expected_identifier="core.laya-instruct",
        expected_version="1.0.0",
    )
    assert verified.identifier == "core.laya-instruct"
    assert verified.version == "1.0.0"
    assert verified.runtime_contract_version == "1.3"
    assert [artifact.name for artifact in verified.artifacts] == [
        "weights.safetensors",
        "tokenizer.json",
    ]
    assert verified.manifest_identity == signature["manifest_identity"]
    assert "artifacts" not in verified.to_dict()


def test_the_dev_test_anchor_helper_is_inert_without_its_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DEV_TEST_ANCHOR_FLAG, raising=False)
    with pytest.raises(RuntimeError, match="production anchors come from the owner"):
        dev_test_anchor(not_before=NOW, not_after=NOW + timedelta(days=1))


def test_the_dev_test_anchor_helper_mints_under_its_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DEV_TEST_ANCHOR_FLAG, "1")
    private_key, anchor = dev_test_anchor(
        not_before=NOW - timedelta(days=1), not_after=NOW + timedelta(days=365)
    )
    manifest, signature = _signed(private_key=private_key)
    verified = verify_model_manifest(
        manifest, signature, trust_anchors=[anchor], verification_time=NOW
    )
    assert verified.identifier == "core.laya-instruct"


def test_an_unflagged_helper_refusal_happens_before_any_key_material_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DEV_TEST_ANCHOR_FLAG, raising=False)
    with pytest.raises(RuntimeError):
        dev_test_anchor(not_before=NOW, not_after=NOW + timedelta(days=1))
    # Nothing was minted for a later ambient discovery: the refusal is the whole
    # effect, and the flag must be set explicitly for every call.
    with pytest.raises(RuntimeError):
        dev_test_anchor(not_before=NOW, not_after=NOW + timedelta(days=1))


# --------------------------------------------------------------------------
# The demanded negative set: tampered, unknown anchor, expired window, wrong key
# --------------------------------------------------------------------------


def test_a_tampered_manifest_is_refused_as_tampered() -> None:
    private_key, anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    manifest["runtime_contract_version"] = "9.9"
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[anchor])
    assert _refusal(error) is ModelTrustRefusal.TAMPERED


def test_a_tampered_artifact_digest_is_refused_even_under_the_real_key() -> None:
    private_key, anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    manifest["artifacts"][0]["sha256"] = "ff" * 32
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[anchor])
    assert _refusal(error) is ModelTrustRefusal.TAMPERED


def test_an_unknown_anchor_is_refused_as_untrusted() -> None:
    private_key, _signer_anchor = _keypair()
    _unused, other_anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[other_anchor])
    assert _refusal(error) is ModelTrustRefusal.UNTRUSTED


def test_an_expired_window_is_refused_as_untrusted() -> None:
    private_key, anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    with pytest.raises(ModelTrustError) as error:
        _verify(
            manifest,
            signature,
            anchors=[anchor],
            verification_time=NOW + timedelta(days=366),
        )
    assert _refusal(error) is ModelTrustRefusal.UNTRUSTED


def test_a_not_yet_valid_window_is_refused_as_untrusted() -> None:
    private_key, anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    with pytest.raises(ModelTrustError) as error:
        _verify(
            manifest,
            signature,
            anchors=[anchor],
            verification_time=NOW - timedelta(days=2),
        )
    assert _refusal(error) is ModelTrustRefusal.UNTRUSTED


def test_a_retired_key_is_refused_as_untrusted() -> None:
    private_key, _anchor = _keypair()
    retired_at = NOW - timedelta(days=1)
    anchor = TrustAnchor(
        key_id=_anchor.key_id,
        public_key=_anchor.public_key,
        not_before=_anchor.not_before,
        not_after=_anchor.not_after,
        retired_at=retired_at,
    )
    manifest, signature = _signed(private_key=private_key)
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[anchor])
    assert _refusal(error) is ModelTrustRefusal.UNTRUSTED


def test_a_wrong_key_id_between_manifest_and_signature_is_refused_as_untrusted() -> None:
    private_key, anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    signature["key_id"] = OTHER_KEY_ID
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[anchor])
    assert _refusal(error) is ModelTrustRefusal.UNTRUSTED


def test_a_wrong_algorithm_is_refused_as_untrusted() -> None:
    private_key, anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    signature["algorithm"] = "rsa2048"
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[anchor])
    assert _refusal(error) is ModelTrustRefusal.UNTRUSTED


def test_a_forged_signature_is_refused_as_untrusted() -> None:
    private_key, anchor = _keypair()
    impostor = Ed25519PrivateKey.generate()
    manifest, signature = _signed(private_key=private_key)
    forged = impostor.sign(base64.b64decode(str(signature["signature"]), validate=True))
    signature["signature"] = base64.b64encode(forged).decode("ascii")
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[anchor])
    assert _refusal(error) is ModelTrustRefusal.UNTRUSTED


def test_a_wrong_requested_model_is_refused_as_incompatible() -> None:
    private_key, anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    with pytest.raises(ModelTrustError) as error:
        _verify(
            manifest,
            signature,
            anchors=[anchor],
            expected_identifier="core.other-model",
        )
    assert _refusal(error) is ModelTrustRefusal.INCOMPATIBLE


def test_a_wrong_requested_version_is_refused_as_incompatible() -> None:
    private_key, anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[anchor], expected_version="2.0.0")
    assert _refusal(error) is ModelTrustRefusal.INCOMPATIBLE


# --------------------------------------------------------------------------
# Structural defects: refused before any trust question
# --------------------------------------------------------------------------


def test_a_wrong_schema_version_is_refused_as_metadata_invalid() -> None:
    manifest, signature = _structurally_mutated(
        lambda document: document.__setitem__("schema_version", "model-manifest.v2")
    )
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[_keypair()[1]])
    assert _refusal(error) is ModelTrustRefusal.METADATA_INVALID


def test_a_malformed_artifact_digest_is_refused_as_metadata_invalid() -> None:
    def mutate(document: dict[str, object]) -> None:
        artifacts = document["artifacts"]
        assert isinstance(artifacts, list)
        artifacts[0]["sha256"] = "zz" * 32  # type: ignore[index]

    manifest, signature = _structurally_mutated(mutate)
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[_keypair()[1]])
    assert _refusal(error) is ModelTrustRefusal.METADATA_INVALID


def test_a_duplicate_artifact_name_is_refused_as_metadata_invalid() -> None:
    def mutate(document: dict[str, object]) -> None:
        artifacts = document["artifacts"]
        assert isinstance(artifacts, list)
        artifacts[1]["name"] = artifacts[0]["name"]  # type: ignore[index]

    manifest, signature = _structurally_mutated(mutate)
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[_keypair()[1]])
    assert _refusal(error) is ModelTrustRefusal.METADATA_INVALID


def test_an_empty_artifact_list_is_refused_as_metadata_invalid() -> None:
    manifest, signature = _structurally_mutated(
        lambda document: document.__setitem__("artifacts", [])
    )
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[_keypair()[1]])
    assert _refusal(error) is ModelTrustRefusal.METADATA_INVALID


def test_a_negative_or_bool_sized_artifact_is_refused_as_metadata_invalid() -> None:
    for bad_size in (-1, 0, True):
        def mutate(document: dict[str, object], bad_size: object = bad_size) -> None:
            artifacts = document["artifacts"]
            assert isinstance(artifacts, list)
            artifacts[0]["size"] = bad_size  # type: ignore[index]

        manifest, signature = _structurally_mutated(mutate)
        with pytest.raises(ModelTrustError) as error:
            _verify(manifest, signature, anchors=[_keypair()[1]])
        assert _refusal(error) is ModelTrustRefusal.METADATA_INVALID


def test_a_float_sized_artifact_is_outside_the_canonical_subset_entirely() -> None:
    """A float never reaches the size validator: the identity's canonical subset
    refuses it first, at the same gate the runtime-payload format uses."""
    from omnivia_core_runtime.distribution.trusted_runtime import CanonicalJsonError

    manifest = _manifest()
    manifest["artifacts"][0]["size"] = 1.5  # type: ignore[typeddict-item]
    with pytest.raises(CanonicalJsonError):
        model_manifest_identity(manifest)


def test_a_traversal_artifact_name_is_refused_as_metadata_invalid() -> None:
    def mutate(document: dict[str, object]) -> None:
        artifacts = document["artifacts"]
        assert isinstance(artifacts, list)
        artifacts[0]["name"] = "../weights.safetensors"  # type: ignore[index]

    manifest, signature = _structurally_mutated(mutate)
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[_keypair()[1]])
    assert _refusal(error) is ModelTrustRefusal.METADATA_INVALID


def test_an_unknown_manifest_member_is_refused_as_metadata_invalid() -> None:
    manifest, signature = _structurally_mutated(
        lambda document: document.__setitem__("publisher_note", "trust me")
    )
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[_keypair()[1]])
    assert _refusal(error) is ModelTrustRefusal.METADATA_INVALID


def test_a_wrong_signature_document_version_is_refused_as_metadata_invalid() -> None:
    private_key, anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    signature["signature_version"] = "model-manifest-signature.v0"
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[anchor])
    assert _refusal(error) is ModelTrustRefusal.METADATA_INVALID


# --------------------------------------------------------------------------
# Honesty properties
# --------------------------------------------------------------------------


def test_the_identity_excludes_itself_from_its_own_preimage() -> None:
    manifest = _manifest()
    manifest["manifest_identity"] = "sha256:" + "00" * 32
    first = model_manifest_identity(manifest)
    manifest["manifest_identity"] = "sha256:" + "ff" * 32
    assert model_manifest_identity(manifest) == first


def test_a_refusal_string_carries_no_manifest_content() -> None:
    private_key, anchor = _keypair()
    manifest, signature = _signed(private_key=private_key)
    manifest["artifacts"][0]["sha256"] = "ff" * 32
    with pytest.raises(ModelTrustError) as error:
        _verify(manifest, signature, anchors=[anchor])
    assert str(error.value) == ModelTrustRefusal.TAMPERED.value
    assert "ff" not in str(error.value)
    assert "weights" not in str(error.value)
    assert error.value.to_dict() == {
        "model_manifest_version": "model-manifest.v1",
        "refusal": "model_manifest_tampered",
    }


def test_rotation_is_two_overlapping_windows_and_nothing_else() -> None:
    old_key, old_anchor = _keypair()
    new_key, _new_anchor = _keypair()
    new_anchor = TrustAnchor(
        key_id=OTHER_KEY_ID,
        public_key=new_key.public_key().public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        ),
        not_before=NOW - timedelta(days=1),
        not_after=NOW + timedelta(days=365),
    )
    old_manifest, old_signature = _signed(private_key=old_key)
    new_manifest, new_signature = _signed(private_key=new_key, key_id=OTHER_KEY_ID)
    both = [old_anchor, new_anchor]
    assert (
        _verify(old_manifest, old_signature, anchors=both).identifier  # type: ignore[attr-defined]
        == "core.laya-instruct"
    )
    assert (
        _verify(new_manifest, new_signature, anchors=both).identifier  # type: ignore[attr-defined]
        == "core.laya-instruct"
    )
    late = NOW + timedelta(days=400)
    for document, detached in ((old_manifest, old_signature), (new_manifest, new_signature)):
        with pytest.raises(ModelTrustError) as error:
            _verify(document, detached, anchors=both, verification_time=late)
        assert _refusal(error) is ModelTrustRefusal.UNTRUSTED


def test_two_anchors_sharing_a_key_id_are_a_caller_defect_not_a_refusal() -> None:
    first, anchor = _keypair()
    second, _ = _keypair()
    twin = TrustAnchor(
        key_id=anchor.key_id,
        public_key=second.public_key().public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        ),
        not_before=anchor.not_before,
        not_after=anchor.not_after,
    )
    manifest, signature = _signed(private_key=first)
    with pytest.raises(ValueError, match="share the key id"):
        _verify(manifest, signature, anchors=[anchor, twin])


def test_the_signature_document_version_constant_is_the_one_verified() -> None:
    from omnivia_core_runtime.distribution import model_manifest_trust

    assert MODEL_SIGNATURE_VERSION == "model-manifest-signature.v1"
    assert model_manifest_trust.MODEL_SIGNATURE_VERSION == MODEL_SIGNATURE_VERSION


def test_the_flag_name_is_stable() -> None:
    assert DEV_TEST_ANCHOR_FLAG == "OMNIVIA_ALLOW_DEV_MODEL_TRUST_ANCHOR"
