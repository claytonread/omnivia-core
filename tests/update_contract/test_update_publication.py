"""The publisher and the consumer, held against each other.

``scripts/publish-update-channel.py`` writes the channel document and
``omnivia_core_client`` reads it. Nothing proved they agreed: the publisher had
no tests at all, and its one structural claim -- that it "verifies its own
checksum inventory" -- was false, because it tested that the inventory file
existed and never read a row of it.

The test that matters here is the round trip. A candidate directory is built,
the publisher is run in prepare mode with a test-only key, and the document it
emits is handed to ``validate_channel_document`` under the matching anchor. A
publisher that emits a shape the consumer refuses is a release nobody can
install, and that is not a failure either side would notice alone.

No test here reaches the network, clones a repository, or runs ``--apply``:
publication stays an owner-gated act and these cases stop at the bytes.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from omnivia_core_client.update_trust import (
    ENVELOPE_SCHEMA_VERSION,
    TrustedUpdateKey,
    UpdateTrustPolicy,
)
from omnivia_core_client.updates import UpdateCheckError, validate_channel_document

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "publish-update-channel.py"

FIRST_PARTY = (
    "omnivia-core",
    "omnivia-core-runtime",
    "omnivia-core-client",
    "omnivia-core-cli",
    "omnivia-core-mcp",
)

KEY_ID = "test-only-channel-2026a"
SEED = hashlib.sha256(b"omnivia core update v2 test-only channel key a").digest()


def _publisher() -> ModuleType:
    """Load the script as a module, the way the other script tests do."""
    specification = importlib.util.spec_from_file_location("publish_update_channel", SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


PUBLISHER = _publisher()


def _candidate(tmp_path: Path, version: str = "0.7.0") -> Path:
    """A minimal standard candidate: five wheels, metadata, bundle, inventory.

    Built with the real inventory grammar -- ``<64 hex>  <path relative to the
    candidate root>`` -- so a publisher that reads the rows is exercised and a
    publisher that only checks the file exists is not.
    """
    import zipfile

    candidate = tmp_path / "standard-candidate"
    (candidate / "wheels").mkdir(parents=True)
    (candidate / "metadata").mkdir(parents=True)

    wheels: list[dict[str, Any]] = []
    for name in FIRST_PARTY:
        distribution = name.replace("-", "_")
        filename = f"{distribution}-{version}-py3-none-any.whl"
        content = f"test-only wheel {distribution} {version}\n".encode()
        (candidate / "wheels" / filename).write_bytes(content)
        wheels.append(
            {
                "name": name,
                "version": version,
                "path": f"wheels/{filename}",
                "sha256": hashlib.sha256(content).hexdigest(),
                "first_party": True,
            }
        )
    manifest = {
        "format": "omnivia.standard-release-manifest.v1",
        "candidate_status": "qualification_only",
        "production_release_eligible": False,
        "profile": list(FIRST_PARTY),
        "wheels": wheels,
        "signing": {"status": "unsigned"},
    }
    (candidate / "metadata" / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    files = sorted(
        path for path in candidate.rglob("*") if path.is_file()
    )
    (candidate / PUBLISHER.CHECKSUMS_FILE).write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
            f"{path.relative_to(candidate).as_posix()}\n"
            for path in files
        ),
        encoding="utf-8",
    )
    # The bundle is deliberately written *after* the inventory and excluded from
    # it, which is what the builder's own two-way coverage does: an archive
    # cannot contain its own digest.
    with zipfile.ZipFile(candidate / PUBLISHER.BUNDLE_ZIP_NAME, "w") as archive:
        for path in [*files, candidate / PUBLISHER.CHECKSUMS_FILE]:
            archive.write(path, path.relative_to(candidate).as_posix())
    return candidate


def _key_file(tmp_path: Path) -> Path:
    path = tmp_path / "channel.key"
    path.write_bytes(SEED)
    path.chmod(0o600)
    return path


def _anchor() -> TrustedUpdateKey:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private = Ed25519PrivateKey.from_private_bytes(SEED)
    return TrustedUpdateKey(
        key_id=KEY_ID,
        public_key=private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        not_before="2020-01-01T00:00:00Z",
        not_after="2099-01-01T00:00:00Z",
    )


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the publisher as the operator runs it, in prepare mode only."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(REPO_ROOT / "src"),
            str(REPO_ROOT / "packages" / "omnivia-core-client" / "src"),
            environment.get("PYTHONPATH", ""),
        ]
    )
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        check=False,
    )


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------


def test_the_document_the_publisher_emits_is_one_the_consumer_accepts(
    tmp_path: Path,
) -> None:
    """The whole point. Publisher out, consumer in, no human in between."""
    candidate = _candidate(tmp_path)
    completed = _run(
        "--candidate",
        str(candidate),
        "--sequence",
        "8",
        "--key-id",
        KEY_ID,
        "--signing-key",
        str(_key_file(tmp_path)),
    )
    assert completed.returncode == 0, completed.stderr
    document = completed.stdout.strip()

    # Verified at the instant the publisher stamped, not a fixed date: the
    # window opens when the envelope is issued, so a test clock behind the
    # publisher's would read a correctly signed envelope as not yet valid.
    parsed_window = json.loads(document)["not_before"]
    policy = UpdateTrustPolicy(trusted_keys=(_anchor(),))
    release = validate_channel_document(
        document, policy=policy, verified_at=parsed_window
    )
    assert release is not None
    assert release.version == "0.7.0"
    assert release.packages == {name: "0.7.0" for name in FIRST_PARTY}
    assert release.bundle_sha256 is not None
    assert release.bundle_sha256.startswith("sha256:")
    assert release.signed is True

    parsed = json.loads(document)
    assert parsed["schema_version"] == ENVELOPE_SCHEMA_VERSION
    assert parsed["sequence"] == 8
    assert parsed["signing"] == {"key_id": KEY_ID, "algorithm": "ed25519"}


def test_the_emitted_document_is_canonical_with_no_pretty_printing(
    tmp_path: Path,
) -> None:
    """A pretty-printed channel is one the consumer refuses as non-canonical.

    The old publisher wrote ``json.dumps(..., indent=2)``. Every installation
    would have fetched it, re-canonicalised it, found the text was not the
    canonical form, and reported ``check_failed`` -- a channel that is live and
    unreadable.
    """
    candidate = _candidate(tmp_path)
    completed = _run(
        "--candidate",
        str(candidate),
        "--sequence",
        "1",
        "--key-id",
        KEY_ID,
        "--signing-key",
        str(_key_file(tmp_path)),
    )
    assert completed.returncode == 0, completed.stderr
    document = completed.stdout.strip()
    assert "\n" not in document
    assert ": " not in document


def test_the_per_package_map_is_carried_through_verbatim(tmp_path: Path) -> None:
    """The map the candidate's own wheels name, not one release number repeated."""
    candidate = _candidate(tmp_path)
    recommendation = PUBLISHER.read_candidate(candidate)
    assert recommendation["packages"] == {name: "0.7.0" for name in FIRST_PARTY}
    body = PUBLISHER.envelope_body(
        recommendation,
        recommendation["version"],
        sequence=3,
        key_id=KEY_ID,
        issued_at="2026-10-17T00:00:00Z",
        not_after="2026-11-17T00:00:00Z",
    )
    assert body["release"]["packages"] is recommendation["packages"]
    assert body["release"]["bundle_sha256"] == recommendation["bundle_sha256"]


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_an_unsigned_channel_is_never_staged(tmp_path: Path) -> None:
    """``--apply`` without a signature is refused before anything is cloned.

    Prepare mode still works and prints the signing input, so the owner can sign
    on a machine this tool never touches.
    """
    candidate = _candidate(tmp_path)
    prepared = _run("--candidate", str(candidate), "--sequence", "2")
    assert prepared.returncode == 0, prepared.stderr
    assert "signing input sha256" in prepared.stderr
    assert "nothing signed" in prepared.stderr
    assert "signature" not in json.loads(prepared.stdout)

    refused = _run("--candidate", str(candidate), "--sequence", "2", "--apply")
    assert refused.returncode != 0
    assert "refuses an unsigned channel" in refused.stderr


def test_a_tampered_candidate_file_is_refused(tmp_path: Path) -> None:
    """The inventory is read, row by row, which it never was before."""
    candidate = _candidate(tmp_path)
    wheel = next((candidate / "wheels").glob("omnivia_core-*.whl"))
    wheel.write_bytes(b"replaced\n")
    completed = _run("--candidate", str(candidate), "--sequence", "2")
    assert completed.returncode != 0
    assert "checksum verification failed" in completed.stderr


def test_a_file_the_inventory_does_not_cover_is_refused(tmp_path: Path) -> None:
    """Coverage runs both ways; a file nothing vouched for ships in the bundle."""
    candidate = _candidate(tmp_path)
    (candidate / "wheels" / "omnivia_core-9.9.9-py3-none-any.whl").write_bytes(b"x")
    completed = _run("--candidate", str(candidate), "--sequence", "2")
    assert completed.returncode != 0
    assert "does not cover the whole candidate" in completed.stderr


def test_an_inventory_row_that_escapes_the_candidate_is_refused(
    tmp_path: Path,
) -> None:
    """An inventory that can name ``../`` can pass a digest check on a file
    nobody shipped."""
    candidate = _candidate(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x")
    index = candidate / PUBLISHER.CHECKSUMS_FILE
    index.write_text(
        f"{hashlib.sha256(b'x').hexdigest()}  ../outside.txt\n", encoding="utf-8"
    )
    completed = _run("--candidate", str(candidate), "--sequence", "2")
    assert completed.returncode != 0
    assert "escapes" in completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_signing_key_others_can_read_is_refused(tmp_path: Path) -> None:
    """A signing key in a world-readable file is not a signing key."""
    candidate = _candidate(tmp_path)
    key = _key_file(tmp_path)
    key.chmod(0o644)
    completed = _run(
        "--candidate",
        str(candidate),
        "--sequence",
        "2",
        "--key-id",
        KEY_ID,
        "--signing-key",
        str(key),
    )
    assert completed.returncode != 0
    assert "owner-private" in completed.stderr


def test_an_offline_signature_must_be_canonical_base64url(tmp_path: Path) -> None:
    """The same one-spelling rule the consumer applies, applied at the source."""
    candidate = _candidate(tmp_path)
    padded = base64.urlsafe_b64encode(b"\x00" * 64).decode("ascii")
    completed = _run(
        "--candidate",
        str(candidate),
        "--sequence",
        "2",
        "--key-id",
        KEY_ID,
        "--signature",
        padded,
    )
    assert completed.returncode != 0
    assert "canonical base64url" in completed.stderr


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (("--sequence", "0"), "sequence starts at 1"),
        (("--sequence", "2", "--valid-days", "0"), "never valid"),
        (("--sequence", "2", "--signature", "x"), "--key-id"),
        (("--sequence", "2", "--key-id", "bad id!"), "key id is malformed"),
    ],
    ids=["zero-sequence", "zero-validity", "signature-without-key-id", "bad-key-id"],
)
def test_the_arguments_that_make_no_sense_are_refused(
    tmp_path: Path, arguments: tuple[str, ...], expected: str
) -> None:
    completed = _run("--candidate", str(_candidate(tmp_path)), *arguments)
    assert completed.returncode != 0
    assert expected in completed.stderr


def test_an_offline_signature_that_is_wrong_is_refused_by_the_consumer(
    tmp_path: Path,
) -> None:
    """The publisher cannot check an offline signature, and does not pretend to.

    It validates the *spelling* and emits the document; whether the bytes verify
    is the consumer's question, and the consumer answers it.
    """
    candidate = _candidate(tmp_path)
    completed = _run(
        "--candidate",
        str(candidate),
        "--sequence",
        "2",
        "--key-id",
        KEY_ID,
        "--signature",
        base64.urlsafe_b64encode(b"\x00" * 64).decode("ascii").rstrip("="),
    )
    assert completed.returncode == 0, completed.stderr
    document = completed.stdout.strip()
    with pytest.raises(UpdateCheckError) as refused:
        validate_channel_document(
            document,
            policy=UpdateTrustPolicy(trusted_keys=(_anchor(),)),
            verified_at=json.loads(document)["not_before"],
        )
    assert refused.value.refusal == "channel_bad_signature"
