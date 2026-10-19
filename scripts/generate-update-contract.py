#!/usr/bin/env python3
"""Generate and gate the Core update contract v1 corpus and vectors.

``contracts/update/v1/schemas/core-update-v1.schema.json`` is hand-authored and
is the contract. Everything under ``contracts/update/v1/fixtures`` is *derived*
-- every canonical document, every SHA-256 and every Ed25519 signature in it --
so it is emitted here rather than typed, and ``--check`` fails when the committed
bytes and the bytes this script would write differ.

That check is the whole point of a generator for these files. A hand-edited
fixture whose signature no longer matches its document is not a fixture that
fails loudly; it is a fixture that quietly stops testing the thing it was named
for.

**What is derived and what is declared.** The *bytes* are derived -- a signature
nobody can recompute is not a vector. The *verdicts* are declared, in this file,
by hand. That line matters: a corpus whose expectations came from running the
implementation would record whatever the implementation does, including its bugs,
and prove nothing. ``tests/update_contract`` is where the two meet.

**Every key below is a published test key and has no production role.** The
seeds are derived from fixed labels precisely so anybody can regenerate the
corpus, in any language, and get the same bytes -- which is what makes the
signature vectors usable as vectors. Nothing in this repository holds, or should
ever hold, a channel signing key: a real channel is signed off-repository and
only its public key reaches a trust anchor.

Offline and deterministic. The only non-standard-library imports are the
repository's own reference implementation -- so a fixture cannot disagree with
the verifier about canonical bytes or about a refusal code -- and
``cryptography`` for Ed25519.

    python scripts/generate-update-contract.py            # write
    python scripts/generate-update-contract.py --check     # verify, write nothing
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(REPO_ROOT / "src"),
    str(REPO_ROOT / "packages" / "omnivia-core-client" / "src"),
]

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    PublicFormat,
)
from omnivia_core_client.update_trust import (  # noqa: E402
    ENVELOPE_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    SIGNATURE_ALGORITHM,
    SIGNATURE_DOMAIN,
    signing_input,
)
from omnivia_core_client.updates import FIRST_PARTY_PACKAGES  # noqa: E402
from omnivia_core_client.updates_apply import (  # noqa: E402
    CHECKSUMS_MEMBER,
    MAX_BUNDLE_MEMBERS,
    WHEELS_DIRECTORY,
)

from omnivia_core.contracts.v1.canonical_json import canonical_bytes  # noqa: E402

CANONICAL_ROOT: Final = REPO_ROOT / "contracts" / "update" / "v1"
FIXTURES_ROOT: Final = CANONICAL_ROOT / "fixtures"
CASE_VERSION: Final = "1.0"
VECTORS_VERSION: Final = "1.0"

PRODUCT: Final = "omnivia-core"
OTHER_PRODUCT: Final = "omnivia-apps"
CHANNEL: Final = "stable"

#: The instant every case is verified at unless it is about a window.
NOW: Final = "2026-10-16T12:00:00Z"

#: Test-only channel keys. The seed is `sha256(label)`, so the corpus
#: regenerates byte for byte anywhere. These are not production keys and never
#: will be: a production signing key never enters this repository in any form --
#: only its public half, and only as a trust anchor a caller passes in.
KEY_LABELS: Final = {
    "test-only-channel-2026a": b"omnivia core update v2 test-only channel key a",
    "test-only-channel-2026b": b"omnivia core update v2 test-only channel key b",
    "test-only-unapproved": b"omnivia core update v2 test-only unapproved key",
}

#: A window generously around `NOW`, for anchors whose windows are not the point.
OPEN_WINDOW: Final = ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")

INSTALLED_VERSION: Final = "0.6.5"
RELEASE_VERSION: Final = "0.7.0"
OLDER_VERSION: Final = "0.6.0"
SEQUENCE: Final = 7

RELEASE_URL: Final = (
    f"https://github.com/claytonread/omnivia-core/releases/tag/core-v{RELEASE_VERSION}"
)
BUNDLE_DIGEST: Final = "sha256:" + hashlib.sha256(b"test-only candidate bundle").hexdigest()


def _seed(key_id: str) -> bytes:
    return hashlib.sha256(KEY_LABELS[key_id]).digest()


def _private(key_id: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_seed(key_id))


def _b64url(raw: bytes) -> str:
    """Canonical unpadded base64url, the one spelling this contract admits."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _public(key_id: str) -> str:
    raw = _private(key_id).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _b64url(raw)


def anchor(
    key_id: str,
    *,
    window: tuple[str, str] = OPEN_WINDOW,
    retired_at: str | None = None,
) -> dict[str, Any]:
    return {
        "key_id": key_id,
        "algorithm": SIGNATURE_ALGORITHM,
        "public_key": _public(key_id),
        "not_before": window[0],
        "not_after": window[1],
        "retired_at": retired_at,
    }


def packages(version: str, **overrides: str) -> dict[str, str]:
    mapping = {name: version for name in FIRST_PARTY_PACKAGES}
    mapping.update(overrides)
    return mapping


def recommendation(
    version: str = RELEASE_VERSION, **overrides: Any
) -> dict[str, Any]:
    document = {
        "version": version,
        "release_url": RELEASE_URL,
        "packages": packages(version),
        "bundle_sha256": BUNDLE_DIGEST,
    }
    document.update(overrides)
    return document


def envelope_body(**overrides: Any) -> dict[str, Any]:
    """One signed-envelope body, before the signature is attached."""
    body: dict[str, Any] = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "product_id": PRODUCT,
        "channel": CHANNEL,
        "sequence": SEQUENCE,
        "issued_at": "2026-10-16T09:00:00Z",
        "not_before": "2026-10-16T09:00:00Z",
        "not_after": "2026-11-16T09:00:00Z",
        "release": recommendation(),
        "signing": {"key_id": "test-only-channel-2026a", "algorithm": SIGNATURE_ALGORITHM},
    }
    body.update(overrides)
    return body


def signed(
    body: dict[str, Any],
    *,
    key_id: str | None = None,
    domain_product: str | None = None,
    corrupt: bool = False,
) -> str:
    """The canonical text of ``body`` plus a signature, as a consumer fetches it.

    ``domain_product`` signs under another product's domain prefix while leaving
    the document's own ``product_id`` alone, which is the only way to test the
    separation rather than the field.
    """
    signer = key_id or body["signing"]["key_id"]
    message = signing_input(body, product_id=domain_product or body["product_id"])
    signature = _private(signer).sign(message)
    if corrupt:
        signature = signature[:-1] + bytes([signature[-1] ^ 0x01])
    return canonical_bytes({**body, "signature": _b64url(signature)}).decode("ascii")


def policy(
    *,
    keys: tuple[dict[str, Any], ...] = (),
    product: str = PRODUCT,
    channel: str = CHANNEL,
    verified_at: str = NOW,
    accept_unsigned_v1: bool = False,
    minimum_sequence: int | None = None,
    installed: dict[str, str] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "expected_product_id": product,
        "expected_channel": channel,
        "trusted_keys": list(keys) or [anchor("test-only-channel-2026a")],
        "verified_at": verified_at,
        "accept_unsigned_v1": accept_unsigned_v1,
    }
    if minimum_sequence is not None:
        document["minimum_sequence"] = minimum_sequence
    if installed is not None:
        document["installed"] = installed
    return document


def envelope_case(
    case_id: str,
    description: str,
    *,
    document_text: str,
    expected_policy: dict[str, Any],
    refusal: str | None,
    release: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "case_version": CASE_VERSION,
        "case_id": case_id,
        "case_kind": "envelope",
        "description": description,
        "document_text": document_text,
        "policy": expected_policy,
        "expected": {
            "outcome": "refused" if refusal else "accepted",
            "refusal": refusal,
            "release": release,
        },
    }


# --------------------------------------------------------------------------
# Envelope cases
# --------------------------------------------------------------------------


def envelope_cases() -> list[dict[str, Any]]:
    accepted = recommendation()
    rotation = (anchor("test-only-channel-2026a"), anchor("test-only-channel-2026b"))
    cases = [
        envelope_case(
            "signed-envelope-baseline",
            "A correctly signed envelope inside its window is accepted and its "
            "recommendation is the one the consumer acts on.",
            document_text=signed(envelope_body()),
            expected_policy=policy(),
            refusal=None,
            release=accepted,
        ),
        envelope_case(
            "signed-envelope-pauses-the-channel",
            "A signed envelope may recommend nothing. The channel is paused, not broken.",
            document_text=signed(envelope_body(release=None)),
            expected_policy=policy(),
            refusal=None,
            release=None,
        ),
        envelope_case(
            "signed-envelope-second-approved-key",
            "Rotation is expressed by the windows alone: two keys are approved at "
            "once, so either may have signed and no flag day is needed.",
            document_text=signed(
                envelope_body(
                    signing={
                        "key_id": "test-only-channel-2026b",
                        "algorithm": SIGNATURE_ALGORITHM,
                    }
                )
            ),
            expected_policy=policy(keys=rotation),
            refusal=None,
            release=accepted,
        ),
        envelope_case(
            "signed-envelope-at-the-window-opening",
            "The validity window is closed at the start and open at the end, so the "
            "instant it opens is inside it.",
            document_text=signed(envelope_body()),
            expected_policy=policy(verified_at="2026-10-16T09:00:00Z"),
            refusal=None,
            release=accepted,
        ),
        envelope_case(
            "unsigned-v1-under-a-development-policy",
            "The legacy unsigned shape is admitted only by a policy that explicitly "
            "says it is a test or development policy.",
            document_text=canonical_bytes(
                {
                    "schema_version": LEGACY_SCHEMA_VERSION,
                    "channel": CHANNEL,
                    "release": {
                        "version": RELEASE_VERSION,
                        "release_url": RELEASE_URL,
                        "packages": packages(RELEASE_VERSION),
                    },
                }
            ).decode("ascii"),
            expected_policy=policy(accept_unsigned_v1=True),
            refusal=None,
            release={
                "version": RELEASE_VERSION,
                "release_url": RELEASE_URL,
                "packages": packages(RELEASE_VERSION),
                "bundle_sha256": None,
            },
        ),
        envelope_case(
            "unsigned-v1-refused-by-production",
            "The same bytes under the production policy. An unsigned channel is not "
            "a production shape, and there is no argument that makes it one.",
            document_text=canonical_bytes(
                {
                    "schema_version": LEGACY_SCHEMA_VERSION,
                    "channel": CHANNEL,
                    "release": None,
                }
            ).decode("ascii"),
            expected_policy=policy(),
            refusal="channel_unsigned_refused",
        ),
        envelope_case(
            "wrong-product",
            "A correctly signed envelope for another product. Refused on its own "
            "terms, and it would fail the signature too: the product id is in the "
            "signing input.",
            document_text=signed(
                envelope_body(product_id=OTHER_PRODUCT), domain_product=OTHER_PRODUCT
            ),
            expected_policy=policy(),
            refusal="channel_wrong_product",
        ),
        envelope_case(
            "wrong-domain-product",
            "The document names this product and the signature was made under "
            "another product's domain prefix. This is the separation itself: the "
            "same key, the same body, bytes that were never these bytes.",
            document_text=signed(envelope_body(), domain_product=OTHER_PRODUCT),
            expected_policy=policy(),
            refusal="channel_bad_signature",
        ),
        envelope_case(
            "wrong-channel",
            "A correctly signed envelope for a channel this installation does not "
            "follow.",
            document_text=signed(envelope_body(channel="beta")),
            expected_policy=policy(),
            refusal="channel_wrong_channel",
        ),
        envelope_case(
            "unapproved-signing-key",
            "Signed by a key that is not a trust anchor. Unknown is the same answer "
            "as expired: nobody currently approved issued this.",
            document_text=signed(
                envelope_body(
                    signing={
                        "key_id": "test-only-unapproved",
                        "algorithm": SIGNATURE_ALGORITHM,
                    }
                )
            ),
            expected_policy=policy(),
            refusal="channel_untrusted_key",
        ),
        envelope_case(
            "not-yet-valid-signing-key",
            "The key is an anchor, and its window has not opened.",
            document_text=signed(envelope_body()),
            expected_policy=policy(
                keys=(
                    anchor(
                        "test-only-channel-2026a",
                        window=("2026-11-01T00:00:00Z", "2027-01-01T00:00:00Z"),
                    ),
                )
            ),
            refusal="channel_untrusted_key",
        ),
        envelope_case(
            "expired-signing-key",
            "The key is an anchor, and its window has closed.",
            document_text=signed(envelope_body()),
            expected_policy=policy(
                keys=(
                    anchor(
                        "test-only-channel-2026a",
                        window=("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
                    ),
                )
            ),
            refusal="channel_untrusted_key",
        ),
        envelope_case(
            "retired-signing-key",
            "The key is inside its window and was retired early. Retirement is the "
            "ceiling, not the window.",
            document_text=signed(envelope_body()),
            expected_policy=policy(
                keys=(
                    anchor("test-only-channel-2026a", retired_at="2026-06-01T00:00:00Z"),
                )
            ),
            refusal="channel_untrusted_key",
        ),
        envelope_case(
            "corrupted-signature",
            "One flipped bit in the signature.",
            document_text=signed(envelope_body(), corrupt=True),
            expected_policy=policy(),
            refusal="channel_bad_signature",
        ),
        envelope_case(
            "duplicate-member-name",
            "One name given twice. A document with two values for one member has no "
            "single value to verify a signature over, and this is the only point at "
            "which the duplicate is still visible: a JSON parser keeps the last and "
            "drops the rest.",
            document_text='{"channel":"stable","channel":"beta"}',
            expected_policy=policy(),
            refusal="channel_duplicate_member",
        ),
        envelope_case(
            "noncanonical-member-order",
            "Well-formed JSON that is not the canonical spelling of what it parses "
            "to. One document has one spelling.",
            document_text=json.dumps(
                {"schema_version": ENVELOPE_SCHEMA_VERSION, "channel": CHANNEL},
                indent=2,
            ),
            expected_policy=policy(),
            refusal="channel_noncanonical",
        ),
        envelope_case(
            "noncanonical-number-spelling",
            "A sequence written as a float. It parses, and it is outside the "
            "canonical number subset -- and a float compared against a replay floor "
            "is a question nobody asked.",
            document_text='{"channel":"stable","sequence":7.0}',
            expected_policy=policy(),
            refusal="channel_noncanonical",
        ),
        envelope_case(
            "unsupported-schema-version",
            "A version this consumer does not implement. Refused, never guessed at.",
            document_text=canonical_bytes(
                {"schema_version": "omnivia-core-update.v9", "channel": CHANNEL}
            ).decode("ascii"),
            expected_policy=policy(),
            refusal="channel_unsupported_version",
        ),
        envelope_case(
            "unknown-envelope-member",
            "An extra member inside the signed bytes. Ignoring it would mean "
            "verifying a signature over a document that was not read.",
            document_text=signed(envelope_body(auto_install=True)),
            expected_policy=policy(),
            refusal="channel_malformed",
        ),
        envelope_case(
            "outside-validity-expired",
            "A correctly signed envelope read after it stopped being usable. This is "
            "the bound on how long a captured envelope is worth anything.",
            document_text=signed(envelope_body()),
            expected_policy=policy(verified_at="2026-12-01T00:00:00Z"),
            refusal="channel_outside_validity",
        ),
        envelope_case(
            "outside-validity-not-yet",
            "A correctly signed envelope read before it becomes usable.",
            document_text=signed(envelope_body()),
            expected_policy=policy(verified_at="2026-10-01T00:00:00Z"),
            refusal="channel_outside_validity",
        ),
        envelope_case(
            "replayed-sequence",
            "A correctly signed envelope at or below the consumer's durable floor. "
            "This is what makes a captured envelope useless once a later one has "
            "been accepted.",
            document_text=signed(envelope_body()),
            expected_policy=policy(minimum_sequence=SEQUENCE),
            refusal="channel_replayed",
        ),
        envelope_case(
            "downgrade-recommendation",
            "A correctly signed envelope recommending a release below the "
            "consumer's floor. A signature is not a reason to move backwards.",
            document_text=signed(
                envelope_body(release=recommendation(OLDER_VERSION))
            ),
            expected_policy=policy(installed=packages(INSTALLED_VERSION)),
            refusal="channel_downgrade",
        ),
        envelope_case(
            "missing-bundle-digest",
            "A correctly signed envelope whose recommendation states no bundle "
            "digest. The signature is sound and the recommendation is not usable: an "
            "archive this installation cannot identify before unpacking it is an "
            "archive it must not unpack.",
            document_text=signed(
                envelope_body(
                    release={
                        "version": RELEASE_VERSION,
                        "release_url": RELEASE_URL,
                        "packages": packages(RELEASE_VERSION),
                    }
                )
            ),
            expected_policy=policy(),
            refusal="channel_release_malformed",
        ),
        envelope_case(
            "non-https-release-url",
            "A correctly signed envelope pointing at a cleartext release.",
            document_text=signed(
                envelope_body(
                    release=recommendation(
                        release_url="http://github.com/claytonread/omnivia-core/releases/tag/core-v0.7.0"
                    )
                )
            ),
            expected_policy=policy(),
            refusal="channel_release_malformed",
        ),
        envelope_case(
            "partial-package-map",
            "A correctly signed envelope that names four of the five first-party "
            "distributions. A partial map cannot be compared or installed.",
            document_text=signed(
                envelope_body(
                    release=recommendation(
                        packages={
                            name: RELEASE_VERSION
                            for name in FIRST_PARTY_PACKAGES
                            if name != "omnivia-core-mcp"
                        }
                    )
                )
            ),
            expected_policy=policy(),
            refusal="channel_release_malformed",
        ),
    ]
    return cases


# --------------------------------------------------------------------------
# Journal and transition cases
# --------------------------------------------------------------------------

OPERATION_ID: Final = "upd-00000000-0000-4000-8000-000000000001"


def record(phase: str, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "operation_id": OPERATION_ID,
        "product_id": PRODUCT,
        "installation_ref": "/installations/test-only",
        "approved_release_ref": {
            "version": RELEASE_VERSION,
            "bundle_sha256": BUNDLE_DIGEST,
        },
        "started_at": NOW,
        "phase": phase,
    }
    document.update(overrides)
    return document


def state_case(
    case_id: str,
    description: str,
    *,
    record_text: str,
    outcome: str,
    proposed_phase: str | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    case: dict[str, Any] = {
        "case_version": CASE_VERSION,
        "case_id": case_id,
        "case_kind": "state",
        "description": description,
        "record_text": record_text,
        "expected": {"outcome": outcome},
    }
    if proposed_phase is not None:
        case["proposed_phase"] = proposed_phase
    if phase is not None:
        case["expected"]["phase"] = phase
    return case


def state_cases() -> list[dict[str, Any]]:
    def text(document: Any) -> str:
        return json.dumps(document, indent=2, sort_keys=True) + "\n"

    return [
        state_case(
            "journal-admitted",
            "A well-formed record in flight. A reader gets the phase it says.",
            record_text=text(record("installing")),
            outcome="record_admitted",
            phase="installing",
        ),
        state_case(
            "transition-preparing-to-stopping",
            "The first move the machine makes.",
            record_text=text(record("preparing")),
            proposed_phase="stopping",
            outcome="transition_legal",
        ),
        state_case(
            "transition-preparing-to-blocked",
            "Staging refused before anything was stopped. A no-mutation exit.",
            record_text=text(record("preparing")),
            proposed_phase="blocked",
            outcome="transition_legal",
        ),
        state_case(
            "transition-verifying-to-complete",
            "The only way to reach complete: readiness, not an installer exit code.",
            record_text=text(record("verifying")),
            proposed_phase="complete",
            outcome="transition_legal",
        ),
        state_case(
            "transition-installing-to-failed",
            "Every working phase may fail.",
            record_text=text(record("installing")),
            proposed_phase="failed",
            outcome="transition_legal",
        ),
        state_case(
            "transition-skips-stopping",
            "Installing without having stopped the affected services is the race the "
            "phase machine exists to prevent, so the journal cannot describe it.",
            record_text=text(record("preparing")),
            proposed_phase="installing",
            outcome="transition_illegal",
        ),
        state_case(
            "transition-leaves-a-terminal-phase",
            "Complete is terminal. A record that resumes from it would say an update "
            "finished twice.",
            record_text=text(record("complete", outcome="updated", finished_at=NOW)),
            proposed_phase="stopping",
            outcome="transition_illegal",
        ),
        state_case(
            "transition-failed-to-complete",
            "A failed operation does not become a successful one by being rewritten.",
            record_text=text(
                record("failed", outcome="failed", failure_code="install_failed")
            ),
            proposed_phase="complete",
            outcome="transition_illegal",
        ),
        state_case(
            "journal-truncated",
            "A half-written journal. Corruption is a third answer and never "
            "conflated with 'no update has run'.",
            record_text='{"operation_id": "upd-00000000-0000-4000-8000-0000000',
            outcome="record_corrupt",
        ),
        state_case(
            "journal-not-an-object",
            "Valid JSON that is not a record.",
            record_text="[]\n",
            outcome="record_corrupt",
        ),
        state_case(
            "journal-unknown-phase",
            "A phase outside the published vocabulary. Reporting it as a record "
            "would hand a caller a phase no phase machine has.",
            record_text=text(record("resuming")),
            outcome="record_corrupt",
        ),
    ]


# --------------------------------------------------------------------------
# Archive safety cases
# --------------------------------------------------------------------------


def wheel_name(distribution: str, version: str) -> str:
    return f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"


def _wheel_bytes(distribution: str, version: str) -> bytes:
    """Deterministic stand-in content. The extractor never opens a wheel."""
    return f"test-only wheel {distribution} {version}\n".encode("ascii")


def safe_members(
    version: str = RELEASE_VERSION,
    *,
    also: dict[str, bytes] | None = None,
    without: tuple[str, ...] = (),
    **overrides: str,
) -> list[dict[str, Any]]:
    """The members of a well-formed candidate bundle, with its own inventory.

    ``also`` adds members that the inventory *does* cover, which is what a bundle
    altered before its inventory was written looks like. A member appended to the
    returned list instead is one the inventory does not mention, which is the
    other attack and a different refusal. ``without`` removes a file before the
    inventory is computed, so the result is an internally consistent bundle that
    is simply missing something.
    """
    files: dict[str, bytes] = {}
    for name in FIRST_PARTY_PACKAGES:
        wheel_version = overrides.get(name, version)
        files[f"{WHEELS_DIRECTORY}/{wheel_name(name, wheel_version)}"] = _wheel_bytes(
            name, wheel_version
        )
    files["metadata/release-manifest.json"] = b'{"format":"omnivia.standard-release-manifest.v1"}\n'
    # Two directories deep, with no directory member above it. The real
    # candidate's licence tree looks exactly like this, and it is the shape that
    # catches an extractor using `mkdir(parents=True, mode=...)`: that call
    # applies the mode to the leaf alone and creates the parents from the umask,
    # so the file lands owner-private inside a world-readable directory.
    files["licenses/omnivia-core-suite/LICENSE"] = b"test-only licence text\n"
    files.update(also or {})
    for name in without:
        del files[name]
    inventory = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {path}\n"
        for path, content in sorted(files.items())
    ).encode("ascii")
    members: list[dict[str, Any]] = [
        {"name": f"{WHEELS_DIRECTORY}/", "kind": "directory", "unix_mode": "0755"},
        {"name": "metadata/", "kind": "directory", "unix_mode": "0755"},
    ]
    for path, content in sorted(files.items()):
        members.append(
            {
                "name": path,
                "kind": "file",
                "content_base64": base64.b64encode(content).decode("ascii"),
                "unix_mode": "0644",
            }
        )
    members.append(
        {
            "name": CHECKSUMS_MEMBER,
            "kind": "file",
            "content_base64": base64.b64encode(inventory).decode("ascii"),
            "unix_mode": "0644",
        }
    )
    return members


def file_member(name: str, content: bytes = b"x", **overrides: Any) -> dict[str, Any]:
    member: dict[str, Any] = {
        "name": name,
        "kind": "file",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "unix_mode": "0644",
    }
    member.update(overrides)
    return member


def archive_case(
    case_id: str,
    description: str,
    *,
    members: list[dict[str, Any]],
    refusal: str | None,
    approved: dict[str, Any] | None = None,
    max_members: int = MAX_BUNDLE_MEMBERS,
    max_expanded_bytes: int = 384 * 1024 * 1024,
    owner_only_staging: bool = True,
) -> dict[str, Any]:
    return {
        "case_version": CASE_VERSION,
        "case_id": case_id,
        "case_kind": "archive",
        "description": description,
        "approved": approved if approved is not None else recommendation(),
        "members": members,
        "bounds": {
            "max_members": max_members,
            "max_expanded_bytes": max_expanded_bytes,
        },
        "expected": {
            "outcome": "refused" if refusal else "extracted",
            "refusal": refusal,
            "owner_only_staging": owner_only_staging,
        },
    }


def archive_cases() -> list[dict[str, Any]]:
    inventory_escape = (
        f"{hashlib.sha256(b'x').hexdigest()}  ../outside.txt\n".encode("ascii")
    )
    return [
        archive_case(
            "archive-baseline",
            "A well-formed candidate bundle: every inventory row matches, the five "
            "first-party wheels are present, and the staging tree is reachable by "
            "its owner alone.",
            members=safe_members(),
            refusal=None,
        ),
        archive_case(
            "archive-absolute-member",
            "A member naming an absolute path. Refused, not rewritten into a "
            "relative one: an archive that tried to escape must not become an "
            "archive that got a different layout.",
            members=[*safe_members(), file_member("/etc/omnivia.conf")],
            refusal="archive_absolute_member",
        ),
        archive_case(
            "archive-parent-traversal",
            "A member climbing out of the archive root.",
            members=[*safe_members(), file_member("../../escaped.txt")],
            refusal="archive_traversal_member",
        ),
        archive_case(
            "archive-backslash-member",
            "A member whose name holds a backslash. Refused on every platform: a "
            "backslash is an ordinary filename character on POSIX and a separator on "
            "Windows, so one archive name means two different layouts.",
            members=[*safe_members(), file_member("wheels\\..\\escaped.txt")],
            refusal="archive_backslash_member",
        ),
        archive_case(
            "archive-reserved-character",
            "A member naming a Windows drive. A relative name with a colon in it is "
            "not a relative name there.",
            members=[*safe_members(), file_member("C:/escaped.txt")],
            refusal="archive_reserved_member",
        ),
        archive_case(
            "archive-states-no-file-type",
            "A bundle whose members state no POSIX file type at all, which is what "
            "`ZipFile.writestr` and most non-POSIX producers write. An absent "
            "type is a claim the archive did not make, not a special file: "
            "refusing it would refuse ordinary archives.",
            members=[
                {
                    "name": member["name"],
                    "kind": member["kind"],
                    **(
                        {"content_base64": member["content_base64"]}
                        if "content_base64" in member
                        else {}
                    ),
                }
                for member in safe_members()
            ],
            refusal=None,
        ),
        archive_case(
            "archive-symlink-member",
            "A symbolic link member. It is how a name that passed every check still "
            "writes outside the root -- by being the directory a later member is "
            "written through.",
            members=[
                *safe_members(),
                {
                    "name": "wheels/link",
                    "kind": "symlink",
                    "content_base64": base64.b64encode(b"/etc").decode("ascii"),
                    "unix_mode": "0120777",
                },
            ],
            refusal="archive_special_member",
        ),
        archive_case(
            "archive-duplicate-member",
            "One name twice. Which of the two the inventory digest describes is not "
            "a question a verifier should have to answer.",
            members=[*safe_members(), file_member(CHECKSUMS_MEMBER, b"forged\n")],
            refusal="archive_duplicate_member",
        ),
        archive_case(
            "archive-too-many-members",
            "More members than the bound admits.",
            members=[
                *safe_members(),
                *(file_member(f"filler/{index}.txt") for index in range(12)),
            ],
            refusal="archive_too_many_members",
            max_members=16,
        ),
        archive_case(
            "archive-expands-past-the-bound",
            "A member that expands past the permitted total. Counted from bytes "
            "written, never from the size the archive declares.",
            members=[*safe_members(), file_member("filler/big.txt", b"y" * 4096)],
            refusal="archive_too_large",
            max_expanded_bytes=1024,
        ),
        archive_case(
            "archive-wheel-name-unparseable",
            "A wheelhouse member ending in .whl whose name is not the built form, "
            "covered by a consistent inventory -- a bundle altered before its "
            "inventory was written, which every digest check passes.",
            members=safe_members(also={f"{WHEELS_DIRECTORY}/not-a-wheel.whl": b"x"}),
            refusal="wheel_name_unparseable",
        ),
        archive_case(
            "archive-wheel-foreign-distribution",
            "A wheel for a distribution outside the five, covered by a consistent "
            "inventory, sitting in the directory pip --find-links is pointed at.",
            members=safe_members(
                also={
                    f"{WHEELS_DIRECTORY}/omnivia_backdoor-9.9.9-py3-none-any.whl": b"x"
                }
            ),
            refusal="wheel_unknown_distribution",
        ),
        archive_case(
            "archive-file-the-inventory-does-not-cover",
            "A wheel dropped into the wheelhouse and left out of the inventory. "
            "Coverage has to run in both directions: this file's digest matches "
            "nothing because nothing claims it, and pip --find-links would still "
            "be able to install it.",
            members=[
                *safe_members(),
                file_member(f"{WHEELS_DIRECTORY}/omnivia_core-9.9.9-py3-none-any.whl"),
            ],
            refusal="inventory_incomplete",
        ),
        archive_case(
            "archive-wheel-version-mismatch",
            "An authentic, intact bundle for a release the channel did not "
            "recommend.",
            members=safe_members(OLDER_VERSION),
            refusal="wheel_version_mismatch",
        ),
        archive_case(
            "archive-wheel-version-per-package",
            "The five do not have to move in lockstep. Each wheel is verified "
            "against the version the channel recommended for *that* distribution, "
            "which a coordinator repeating one release number for all five would "
            "refuse.",
            members=safe_members(**{"omnivia-core-mcp": OLDER_VERSION}),
            refusal=None,
            approved=recommendation(
                packages=packages(RELEASE_VERSION, **{"omnivia-core-mcp": OLDER_VERSION})
            ),
        ),
        archive_case(
            "archive-inventory-digest-mismatch",
            "One altered file. The inventory covers the whole candidate, so a "
            "changed metadata file is a changed bundle whatever the file was for.",
            members=[
                *[
                    member
                    if member["name"] != "metadata/release-manifest.json"
                    else file_member("metadata/release-manifest.json", b"{}\n")
                    for member in safe_members()
                ],
            ],
            refusal="inventory_digest_mismatch",
        ),
        archive_case(
            "archive-inventory-row-escapes",
            "An inventory row naming a path outside the bundle. An inventory that "
            "can say ../ is an inventory that can point a digest check at a file "
            "nobody shipped and pass.",
            members=[
                *[m for m in safe_members() if m["name"] != CHECKSUMS_MEMBER],
                file_member(CHECKSUMS_MEMBER, inventory_escape),
            ],
            refusal="archive_traversal_member",
        ),
        archive_case(
            "archive-missing-wheel",
            "An internally consistent bundle -- every inventory row matches and "
            "nothing is uncovered -- that is simply missing one of the five. "
            "Nothing about the archive is wrong; it is not the release the channel "
            "recommended.",
            members=safe_members(
                without=(
                    (
                        f"{WHEELS_DIRECTORY}/"
                        f"{wheel_name('omnivia-core-mcp', RELEASE_VERSION)}"
                    ),
                )
            ),
            refusal="wheel_missing",
        ),
        archive_case(
            "archive-no-inventory",
            "A bundle with no checksum inventory at all. Nothing in it can be held "
            "against anything.",
            members=[m for m in safe_members() if m["name"] != CHECKSUMS_MEMBER],
            refusal="checksums_missing",
        ),
    ]


# --------------------------------------------------------------------------
# Vectors
# --------------------------------------------------------------------------


def canonicalisation_vectors() -> list[dict[str, Any]]:
    accepted = [
        ('{"b":1,"a":2,"C":3}', '{"C":3,"a":2,"b":1}', "sorted-object-names"),
        (
            '{"outer": {"z": [1, 2, {"y": true, "x": null}]}}',
            '{"outer":{"z":[1,2,{"x":null,"y":true}]}}',
            "nested-objects-and-arrays",
        ),
        # RFC 8785 serialises a string as UTF-8 and escapes only what JSON
        # requires, so non-ASCII passes through. Stated explicitly because the
        # trusted-runtime contract's vectors say the opposite about *its* own
        # canonicaliser, and an implementer reading both needs to know the two
        # are different functions rather than one of them being wrong.
        ('{"k\\u00e9y": "v\\u00e4lue"}', '{"kéy":"välue"}', "non-ascii-is-not-escaped"),
        ('{"a": "\\u001f\\n\\""}', '{"a":"\\u001f\\n\\""}', "control-characters-are-escaped"),
        ('{"a": {}, "b": []}', '{"a":{},"b":[]}', "empty-containers"),
        ('{"a": [3, 1, 2]}', '{"a":[3,1,2]}', "array-order-is-significant"),
        ('{"a": -1, "b": 0, "c": 1}', '{"a":-1,"b":0,"c":1}', "negative-and-zero-integers"),
        # Numbers are *in* the canonical subset: RFC 8785 defines a serialization
        # for every finite binary64, and the four below are the ones that catch an
        # implementation out. Each has exactly one canonical spelling, which is
        # what makes them the reason the one-spelling rule has teeth: `1.0`,
        # `1e3` and `-0.0` all canonicalise to something that is not the text
        # they were written as, so a channel document spelled any of those ways
        # is refused as `channel_noncanonical` rather than quietly re-spelled.
        ('{"a": 1.5}', '{"a":1.5}', "fractional-number"),
        ('{"a": 1.0}', '{"a":1}', "integral-float-canonicalises-to-an-integer"),
        ('{"a": 1e3}', '{"a":1000}', "exponent-canonicalises-to-digits"),
        ('{"a": -0.0}', '{"a":0}', "negative-zero-canonicalises-to-zero"),
    ]
    refused = [
        ('{"a": 1, "a": 2}', "duplicate-object-name", "duplicate object name"),
        ('{"a": NaN}', "not-a-number", "NaN is not JSON"),
        ('{"a": Infinity}', "infinity", "Infinity is not JSON"),
        (
            '{"a": 1e400}',
            "number-outside-binary64",
            "a number that is not a finite binary64 is not expressible in JSON",
        ),
    ]
    vectors = [
        {
            "name": name,
            "json": raw,
            "accepted": True,
            "canonical": canonical,
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }
        for raw, canonical, name in accepted
    ]
    vectors.extend(
        {"name": name, "json": raw, "accepted": False, "reason": reason}
        for raw, name, reason in refused
    )
    return sorted(vectors, key=lambda vector: str(vector["name"]))


def base64url_vectors() -> list[dict[str, Any]]:
    key = _private("test-only-channel-2026a").public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    accepted = [
        ("canonical-32-byte-key", _b64url(key), 32, key),
        ("canonical-one-byte", "Yw", 1, b"c"),
    ]
    refused = [
        ("padded", _b64url(key) + "=", 32, "padding is not part of the canonical spelling"),
        (
            "standard-alphabet",
            base64.b64encode(key).decode("ascii").rstrip("="),
            32,
            "+ and / are not the base64url alphabet",
        ),
        (
            "non-zero-trailing-bits",
            "Yx",
            1,
            (
                "the unused trailing bits of a final partial group must be zero, "
                "or one byte string has more than one spelling"
            ),
        ),
        ("wrong-length", "Yw", 32, "not 32 bytes of base64url"),
    ]
    vectors = [
        {
            "name": name,
            "text": text,
            "expected_bytes": size,
            "accepted": True,
            "decoded_hex": raw.hex(),
        }
        for name, text, size, raw in accepted
    ]
    vectors.extend(
        {
            "name": name,
            "text": text,
            "expected_bytes": size,
            "accepted": False,
            "reason": reason,
        }
        for name, text, size, reason in refused
    )
    # `standard-alphabet` only differs from the canonical spelling when the key
    # actually contains a 62nd or 63rd sextet. Assert it rather than hope.
    standard = base64.b64encode(key).decode("ascii")
    if "+" not in standard and "/" not in standard:
        raise SystemExit(
            "the test-only key's standard base64 has no + or /, so the "
            "standard-alphabet vector would not be a refusal"
        )
    return sorted(vectors, key=lambda vector: str(vector["name"]))


def signing_input_vectors() -> list[dict[str, Any]]:
    bodies = [
        ("baseline", envelope_body(), PRODUCT),
        ("paused-channel", envelope_body(release=None), PRODUCT),
        ("other-product-domain", envelope_body(), OTHER_PRODUCT),
    ]
    return [
        {
            "name": name,
            "envelope_json": canonical_bytes(body).decode("ascii"),
            "product_id": product,
            "signing_input_sha256": hashlib.sha256(
                signing_input(body, product_id=product)
            ).hexdigest(),
        }
        for name, body, product in bodies
    ]


def signature_vectors() -> list[dict[str, Any]]:
    body = envelope_body()
    good = _private("test-only-channel-2026a").sign(
        signing_input(body, product_id=PRODUCT)
    )
    other_domain = _private("test-only-channel-2026a").sign(
        signing_input(body, product_id=OTHER_PRODUCT)
    )
    other_key = _private("test-only-channel-2026b").sign(
        signing_input(body, product_id=PRODUCT)
    )
    flipped = good[:-1] + bytes([good[-1] ^ 0x01])
    text = canonical_bytes(body).decode("ascii")
    return [
        {
            "name": "valid",
            "envelope_json": text,
            "product_id": PRODUCT,
            "public_key": _public("test-only-channel-2026a"),
            "signature": _b64url(good),
            "verifies": True,
        },
        {
            "name": "other-product-domain",
            "envelope_json": text,
            "product_id": PRODUCT,
            "public_key": _public("test-only-channel-2026a"),
            "signature": _b64url(other_domain),
            "verifies": False,
            "reason": "signed under another product's domain prefix over the same body",
        },
        {
            "name": "other-key",
            "envelope_json": text,
            "product_id": PRODUCT,
            "public_key": _public("test-only-channel-2026a"),
            "signature": _b64url(other_key),
            "verifies": False,
            "reason": "signed by a different key",
        },
        {
            "name": "flipped-bit",
            "envelope_json": text,
            "product_id": PRODUCT,
            "public_key": _public("test-only-channel-2026a"),
            "signature": _b64url(flipped),
            "verifies": False,
            "reason": "one bit of the signature changed",
        },
    ]


def vectors_document() -> dict[str, Any]:
    return {
        "vectors_version": VECTORS_VERSION,
        "signature_domain": SIGNATURE_DOMAIN,
        "canonicalisation": canonicalisation_vectors(),
        "base64url": base64url_vectors(),
        "signing_input": signing_input_vectors(),
        "signatures": signature_vectors(),
    }


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------


def planned_files() -> dict[str, Any]:
    """Every path this generator owns, as ``relative path -> document``."""
    planned: dict[str, Any] = {}
    for category, cases in (
        ("envelopes", envelope_cases()),
        ("states", state_cases()),
        ("archives", archive_cases()),
    ):
        for case in cases:
            planned[f"{category}/{case['case_id']}.json"] = case
    planned["vectors/canonicalisation-and-signatures.json"] = vectors_document()
    return planned


def serialise(document: Any) -> str:
    """One deterministic spelling for every emitted file."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed corpus matches this generator; write nothing",
    )
    arguments = parser.parse_args()

    planned = planned_files()
    rendered = {path: serialise(document) for path, document in planned.items()}

    existing = {
        path.relative_to(FIXTURES_ROOT).as_posix()
        for path in FIXTURES_ROOT.rglob("*.json")
    } if FIXTURES_ROOT.is_dir() else set()

    if arguments.check:
        problems: list[str] = []
        for stale in sorted(existing - set(rendered)):
            problems.append(f"{stale} is committed and this generator does not emit it")
        for path, content in sorted(rendered.items()):
            target = FIXTURES_ROOT / path
            if not target.is_file():
                problems.append(f"{path} is missing")
            elif target.read_text(encoding="utf-8") != content:
                problems.append(f"{path} differs from the generated bytes")
        if problems:
            for problem in problems:
                print(f"update contract corpus: {problem}", file=sys.stderr)
            print(
                "\nRun `python scripts/generate-update-contract.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"update contract corpus check passed ({len(rendered)} files).")
        return 0

    for stale in sorted(existing - set(rendered)):
        (FIXTURES_ROOT / stale).unlink()
    for path, content in sorted(rendered.items()):
        target = FIXTURES_ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    print(f"wrote {len(rendered)} files under {FIXTURES_ROOT.relative_to(REPO_ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
