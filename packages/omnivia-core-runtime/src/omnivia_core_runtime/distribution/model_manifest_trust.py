"""Model-manifest trust: the G-3 mechanism, inert until the owner publishes an anchor.

SPEC-CORE-DEC-001's decision-runtime gates split model-distribution trust in two.
The *anchor* -- who holds the production signing key, where the private half lives,
where the public half is published -- is an owner decision (G-3a-d) that no
engineering lane may pre-empt: minting an anchor from this repository would be
self-certification, the exact anti-pattern the preflight's repository-external
checkpoint exists to prevent.

The *mechanism* is this module, and it deliberately reinvents nothing. It reuses
the runtime-payload trust format member for member -- the same
:class:`~omnivia_core_runtime.distribution.trusted_runtime.TrustAnchor` shape with
its validity windows, the same canonical-JSON signature discipline, the same
verify-then-name-the-pair order -- pointed at model manifests instead of runtime
payloads. A manifest declares one model's identifier, version, artifact digests and
sizes, and the runtime/API contract version it satisfies; a detached signature
document names the key that signed it.

It ships inert for production in three enforced ways:

1. every entry point demands an explicit anchor sequence from the caller -- anchors
   are the caller's authority, never installation metadata, and no production
   anchor source exists until G-3a-d resolve;
2. a failed verification answers with a closed, payload-free refusal code, so even
   a careless caller cannot leak manifest contents or key material;
3. the only key material this module can produce is :func:`dev_test_anchor`, which
   refuses to run without an explicit environment flag and exists for dev and test
   lanes alone.

Until the real anchor is published, a caller that passes a dev/test anchor gets a
dev/test verdict, and nothing in this repository treats that as production trust.
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from omnivia_core_runtime.distribution.trusted_runtime import (
    SIGNATURE_ALGORITHM,
    TrustAnchor,
    canonical_json,
)

#: The manifest document version this module verifies. A different value is a
#: metadata refusal, not a best-effort decode.
MODEL_MANIFEST_VERSION: Final = "model-manifest.v1"
#: The detached signature document version this module verifies.
MODEL_SIGNATURE_VERSION: Final = "model-manifest-signature.v1"
#: Domain separation for the manifest identity hash, mirroring the runtime
#: payload's ``IDENTITY_DOMAIN``: identities from different contracts can never
#: collide even where the canonical bytes happen to agree.
MODEL_IDENTITY_DOMAIN: Final = "omnivia-model-manifest-identity.v1"
#: Domain separation for the signed bytes, mirroring ``SIGNATURE_DOMAIN``: a
#: signature over a model manifest cannot be lifted onto a runtime payload
#: manifest or conversely, whatever the key.
MODEL_SIGNATURE_DOMAIN: Final = "omnivia-model-manifest-signature.v1"
#: Bound on a manifest document, so a hostile publisher cannot make verification
#: allocation-proportional to their ambition.
MAX_MODEL_MANIFEST_BYTES: Final = 64 * 1024
#: Bound on the detached signature document.
MAX_MODEL_SIGNATURE_BYTES: Final = 4 * 1024
#: The environment flag that unlocks :func:`dev_test_anchor`. Any other value --
#: including its absence -- keeps the helper refused.
DEV_TEST_ANCHOR_FLAG: Final = "OMNIVIA_ALLOW_DEV_MODEL_TRUST_ANCHOR"

_MODEL_SCHEMA_MEMBERS: Final = frozenset(
    {
        "schema_version",
        "manifest_identity",
        "identifier",
        "version",
        "runtime_contract_version",
        "artifacts",
        "signing",
    }
)
_ARTIFACT_MEMBERS: Final = frozenset({"name", "sha256", "size"})
_HEX64: Final = frozenset("0123456789abcdef")


class ModelTrustRefusal(str, Enum):
    """The closed, payload-free vocabulary a failed verification answers with.

    Same shape and same rationale as the runtime payload's refusal enum: one
    literal per class of remedy, because a consumer branches on them. Every value
    is a literal so a code never depends on declaration position.
    """

    #: A manifest or signature document is malformed, oversized, or an unsupported
    #: version. Repair the publishing lane.
    METADATA_INVALID = "model_manifest_metadata_invalid"
    #: The signature, the key, or the key's window is not approved. Never install.
    UNTRUSTED = "model_manifest_untrusted"
    #: A manifest identity, or an artifact digest or size, does not match. Never
    #: install.
    TAMPERED = "model_manifest_tampered"
    #: An authentic manifest that is not the model the caller asked for. Fetch the
    #: right one.
    INCOMPATIBLE = "model_manifest_incompatible"


class ModelTrustError(Exception):
    """One closed refusal, and nothing else.

    ``str()`` of it is the wire value, so even a caller that logs the exception
    carelessly cannot leak a manifest's contents, an artifact name, or key
    material through this type.
    """

    def __init__(self, refusal: ModelTrustRefusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal

    def to_dict(self) -> dict[str, Any]:
        """The versioned machine-readable refusal document."""
        return {
            "model_manifest_version": MODEL_MANIFEST_VERSION,
            "refusal": self.refusal.value,
        }


def _refuse(refusal: ModelTrustRefusal) -> ModelTrustError:
    return ModelTrustError(refusal)


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """One model artifact, as the signed manifest declares it."""

    name: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class VerifiedModelManifest:
    """A completely verified model manifest and the identity it was verified under.

    Returned on success only. Every field is derived from the signed manifest;
    nothing in it was taken from an untrusted record. It is a pinning hint for a
    consumer and never continuing authority: the next fetch resolves again, because
    a published distribution can change between two fetches and a cached verdict
    cannot notice.
    """

    identifier: str
    version: str
    manifest_identity: str
    runtime_contract_version: str
    artifacts: tuple[ModelArtifact, ...]

    def to_dict(self) -> dict[str, Any]:
        """The versioned machine-readable descriptor, and the whole of it.

        The artifact list is deliberately not in it, for the same reason the
        runtime descriptor omits its inventory: a consumer pins the identity and
        the pair, and publishing the artifact list would put the distribution's
        whole shape into whatever the consumer logs.
        """
        return {
            "model_manifest_version": MODEL_MANIFEST_VERSION,
            "identifier": self.identifier,
            "version": self.version,
            "manifest_identity": self.manifest_identity,
            "runtime_contract_version": self.runtime_contract_version,
        }


def _hex_digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in _HEX64 for c in value):
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    return value


def _identity_digest(value: Any) -> str:
    """One ``sha256:``-prefixed manifest identity, as the member is published."""
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(c not in _HEX64 for c in value[7:])
    ):
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    return value


def _non_empty_name(value: Any) -> str:
    """One logical artifact name: a non-empty single path segment."""
    if not isinstance(value, str) or not value:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    return value


def _positive_size(value: Any) -> int:
    # `bool` is an `int` subclass; a `True` size would otherwise pass.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    return value


def _short_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    return value


def model_manifest_identity(manifest: Mapping[str, Any]) -> str:
    """The identity of the model ``manifest`` describes.

    ``manifest_identity`` is dropped before canonicalising, which is what keeps
    the hash out of its own input -- the same self-exclusion discipline the
    runtime payload identity uses. Every other member is covered, so moving one
    byte of a declared digest, an artifact name, the version or the contract
    version produces a different identity and a signature that no longer
    verifies.
    """
    claim = {name: value for name, value in manifest.items() if name != "manifest_identity"}
    digest = hashlib.sha256(
        MODEL_IDENTITY_DOMAIN.encode("ascii") + b"\x00" + canonical_json(claim)
    )
    return f"sha256:{digest.hexdigest()}"


def signed_model_manifest_bytes(manifest: Mapping[str, Any], identity: str) -> bytes:
    """The exact bytes a model-manifest signature covers.

    The manifest *including* the computed identity, under the signature domain.
    A signature therefore attests to the identity as well as to the members it
    was derived from, so one cannot be lifted onto a manifest with a different
    identity.
    """
    signed = {name: value for name, value in manifest.items() if name != "manifest_identity"}
    signed["manifest_identity"] = identity
    return MODEL_SIGNATURE_DOMAIN.encode("ascii") + b"\x00" + canonical_json(signed)


def sign_model_manifest(
    manifest: Mapping[str, Any], *, key_id: str, private_key: Ed25519PrivateKey
) -> dict[str, Any]:
    """Sign one prepared manifest and return its detached signature document.

    The mechanism half of the release lane's job. Production *signing* -- which
    key, held by whom, on whose ceremony -- is G-3a-b and belongs to the owner;
    this function only formats and computes, and it signs whatever document it is
    handed, trusting the caller to have assembled that document honestly.
    """
    working = dict(manifest)
    working["signing"] = {"key_id": key_id}
    identity = model_manifest_identity(working)
    signature = private_key.sign(signed_model_manifest_bytes(working, identity))
    return {
        "signature_version": MODEL_SIGNATURE_VERSION,
        "key_id": key_id,
        "algorithm": SIGNATURE_ALGORITHM,
        "manifest_identity": identity,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def _validated_manifest(manifest: Mapping[str, Any]) -> None:
    """Structural validation of the manifest document, before any trust question."""
    if set(manifest) != _MODEL_SCHEMA_MEMBERS:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    if manifest["schema_version"] != MODEL_MANIFEST_VERSION:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    _short_text(manifest["identifier"])
    _short_text(manifest["version"])
    _short_text(manifest["runtime_contract_version"])
    _identity_digest(manifest["manifest_identity"])
    signing = manifest["signing"]
    if not isinstance(signing, Mapping) or set(signing) != {"key_id"}:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    _short_text(signing["key_id"])
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    seen: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or set(artifact) != _ARTIFACT_MEMBERS:
            raise _refuse(ModelTrustRefusal.METADATA_INVALID)
        name = _non_empty_name(artifact["name"])
        _hex_digest(artifact["sha256"])
        _positive_size(artifact["size"])
        if name in seen:
            # Two entries claiming one artifact name is a document a consumer
            # cannot act on deterministically; refuse it as malformed.
            raise _refuse(ModelTrustRefusal.METADATA_INVALID)
        seen.add(name)


def verify_model_manifest(
    manifest_document: Mapping[str, Any],
    signature_document: Mapping[str, Any],
    *,
    trust_anchors: Sequence[TrustAnchor],
    verification_time: datetime,
    expected_identifier: str | None = None,
    expected_version: str | None = None,
) -> VerifiedModelManifest:
    """Verify one model manifest completely, or refuse without naming its contents.

    The order below is the contract, because each step is only meaningful once
    the one before it has held:

    1. the manifest document is structurally valid;
    2. the recomputed identity agrees with both the manifest's own
       ``manifest_identity`` member and the detached signature document's;
    3. the signature document is well-formed and its key id agrees with the key
       named inside the signed bytes -- they are two independent statements and
       must agree, or one of the two was substituted;
    4. the named key is one of the caller's anchors and its window covers
       ``verification_time`` -- unknown, not yet valid, expired and retired are
       one answer: this manifest was not issued by anybody currently approved;
    5. the Ed25519 signature verifies over the exact signed bytes;
    6. the pair is the one the caller asked for.

    Only after every step has held is the pair named to the caller. A failure
    answers with the refusal code alone.
    """
    if verification_time.tzinfo is None:
        raise ValueError("verification_time must be timezone-aware")
    anchors: dict[str, TrustAnchor] = {}
    for anchor in trust_anchors:
        if anchor.key_id in anchors:
            raise ValueError(f"two trust anchors share the key id {anchor.key_id!r}")
        anchors[anchor.key_id] = anchor

    # 1. Structure.
    _validated_manifest(manifest_document)

    # 2. Identity agreement, recomputed here and never taken from the document.
    identity = model_manifest_identity(manifest_document)
    if manifest_document["manifest_identity"] != identity:
        raise _refuse(ModelTrustRefusal.TAMPERED)

    # 3. The detached signature document.
    if not isinstance(signature_document, Mapping) or set(signature_document) != {
        "signature_version",
        "key_id",
        "algorithm",
        "manifest_identity",
        "signature",
    }:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    if signature_document["signature_version"] != MODEL_SIGNATURE_VERSION:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID)
    if signature_document["algorithm"] != SIGNATURE_ALGORITHM:
        raise _refuse(ModelTrustRefusal.UNTRUSTED)
    if signature_document["key_id"] != manifest_document["signing"]["key_id"]:
        raise _refuse(ModelTrustRefusal.UNTRUSTED)
    if signature_document["manifest_identity"] != identity:
        raise _refuse(ModelTrustRefusal.TAMPERED)

    # 4. The anchor.
    approved = anchors.get(str(signature_document["key_id"]))
    if approved is None or not approved.usable_at(verification_time):
        raise _refuse(ModelTrustRefusal.UNTRUSTED)

    # 5. The signature itself.
    try:
        signature = base64.b64decode(str(signature_document["signature"]), validate=True)
    except (ValueError, TypeError) as failure:
        raise _refuse(ModelTrustRefusal.METADATA_INVALID) from failure
    try:
        Ed25519PublicKey.from_public_bytes(approved.public_key).verify(
            signature, signed_model_manifest_bytes(manifest_document, identity)
        )
    except (InvalidSignature, ValueError) as failure:
        raise _refuse(ModelTrustRefusal.UNTRUSTED) from failure

    # 6. The caller's request.
    if expected_identifier is not None and manifest_document["identifier"] != expected_identifier:
        raise _refuse(ModelTrustRefusal.INCOMPATIBLE)
    if expected_version is not None and manifest_document["version"] != expected_version:
        raise _refuse(ModelTrustRefusal.INCOMPATIBLE)

    artifacts = tuple(
        ModelArtifact(
            name=str(artifact["name"]),
            sha256=str(artifact["sha256"]),
            size=int(artifact["size"]),
        )
        for artifact in manifest_document["artifacts"]
    )
    return VerifiedModelManifest(
        identifier=str(manifest_document["identifier"]),
        version=str(manifest_document["version"]),
        manifest_identity=identity,
        runtime_contract_version=str(manifest_document["runtime_contract_version"]),
        artifacts=artifacts,
    )


def dev_test_anchor(
    *,
    not_before: datetime,
    not_after: datetime,
    key_id: str = "dev-test-model-anchor",
) -> tuple[Ed25519PrivateKey, TrustAnchor]:
    """One ephemeral Ed25519 keypair and its matching anchor, for dev and test lanes.

    This is the only key material this module can produce, and it is never
    production trust: the anchor this returns exists for as long as the process
    that made it, is generated on the spot, and is refused outright unless the
    ``OMNIVIA_ALLOW_DEV_MODEL_TRUST_ANCHOR`` environment flag is set. A verdict
    reached with it says "the mechanism works", nothing more. Production anchors
    come from the owner's key ceremony (G-3a-b) and are supplied by the caller
    exactly as any other anchor is.
    """
    if os.environ.get(DEV_TEST_ANCHOR_FLAG) != "1":
        raise RuntimeError(
            f"{DEV_TEST_ANCHOR_FLAG} must be '1' to mint a dev/test model trust anchor; "
            "production anchors come from the owner's published anchor document"
        )
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    anchor = TrustAnchor(
        key_id=key_id,
        public_key=public_bytes,
        not_before=not_before,
        not_after=not_after,
    )
    return private_key, anchor
