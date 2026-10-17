"""The ``service update-check`` machinery (spec v0.4 §5; A01/A02/A21).

No test here touches a live feed: the fetch is an injected callable, the
installed-version provider is an injected callable, and every channel document
is built in memory. What is pinned is the honesty of the six statuses (A01
ordering, A02 failures), the strict channel schema, the editable-checkout
eligibility gate, and the "never up to date on failure" rule.

**Every document here is a signed envelope v2, because that is what production
reads.** These cases used to build the unsigned v1 shape and pass it to a check
with no policy at all, which exercised a path production must now refuse. The
fixture below signs with a published test-only key and the check is given the
matching anchor, so what the six statuses are proved over is the production
admission path. The one deliberate exception is
:func:`test_the_legacy_unsigned_shape_is_refused_unless_a_policy_asks_for_it`,
which is the whole point of the development switch existing.

The key is test-only and derived from a fixed label, so it regenerates anywhere
and has no production role. Nothing in this repository holds a real channel
signing key.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from omnivia_core_client.update_trust import (
    ENVELOPE_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    PACKAGED_TRUSTED_KEYS,
    PRODUCTION_TRUST_POLICY,
    TrustedUpdateKey,
    UpdateTrustPolicy,
    development_trust_policy,
    signing_input,
)
from omnivia_core_client.updates import (
    DEFAULT_UPDATE_CHANNEL_URL,
    MAX_BUNDLE_BYTES,
    MAX_CHANNEL_BYTES,
    UpdateCheckError,
    check_for_updates,
    installed_packages,
    validate_channel_document,
)

from omnivia_core.contracts.v1.canonical_json import canonical_bytes

INSTALLED = {
    "omnivia-core": "0.1.0",
    "omnivia-core-runtime": "0.1.0",
    "omnivia-core-client": "0.1.0",
    "omnivia-core-cli": "0.1.0",
    "omnivia-core-mcp": "0.1.0",
}
NOW = "2026-09-30T00:00:00Z"
DIGEST = "sha256:" + "a" * 64

KEY_ID = "test-only-channel-2026a"
OTHER_KEY_ID = "test-only-channel-2026b"


def _private(label: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label).digest())


SIGNER = _private(b"omnivia core update v2 test-only channel key a")
OTHER_SIGNER = _private(b"omnivia core update v2 test-only channel key b")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _anchor(
    key_id: str,
    signer: Ed25519PrivateKey,
    *,
    not_before: str = "2026-01-01T00:00:00Z",
    not_after: str = "2027-01-01T00:00:00Z",
    retired_at: str | None = None,
) -> TrustedUpdateKey:
    return TrustedUpdateKey(
        key_id=key_id,
        public_key=signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        not_before=not_before,
        not_after=not_after,
        retired_at=retired_at,
    )


TRUSTED = _anchor(KEY_ID, SIGNER)
POLICY = UpdateTrustPolicy(trusted_keys=(TRUSTED,))


def _body(version: str | None = "0.1.0", **overrides: Any) -> dict[str, Any]:
    """One signed-envelope body, before the signature is attached."""
    release: dict[str, Any] | None = None
    if version is not None:
        release = {
            "version": version,
            "release_url": f"https://github.com/claytonread/omnivia-core/releases/tag/core-v{version}",
            "packages": {name: version for name in INSTALLED},
            "bundle_sha256": DIGEST,
        }
    body: dict[str, Any] = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "product_id": "omnivia-core",
        "channel": "stable",
        "sequence": 4,
        "issued_at": "2026-09-01T00:00:00Z",
        "not_before": "2026-09-01T00:00:00Z",
        "not_after": "2026-10-31T00:00:00Z",
        "release": release,
        "signing": {"key_id": KEY_ID, "algorithm": "ed25519"},
    }
    body.update(overrides)
    return body


def _sign(
    body: dict[str, Any],
    *,
    signer: Ed25519PrivateKey = SIGNER,
    domain_product: str | None = None,
) -> bytes:
    """The canonical bytes a consumer fetches, signature attached."""
    message = signing_input(body, product_id=domain_product or body["product_id"])
    return canonical_bytes({**body, "signature": _b64url(signer.sign(message))})


def _channel(version: str | None = "0.1.0", **overrides: Any) -> bytes:
    return _sign(_body(version, **overrides))


def _legacy(version: str | None = "0.1.0") -> bytes:
    """The unsigned v1 shape, for the one test that is about it."""
    release: dict[str, Any] | None = None
    if version is not None:
        release = {
            "version": version,
            "release_url": f"https://github.com/claytonread/omnivia-core/releases/tag/core-v{version}",
            "packages": {name: version for name in INSTALLED},
        }
    return canonical_bytes(
        {
            "schema_version": LEGACY_SCHEMA_VERSION,
            "channel": "stable",
            "release": release,
        }
    )


def _check(
    channel: Any,
    *,
    installed: dict[str, str] | None = None,
    fetch: Any = None,
    policy: UpdateTrustPolicy = POLICY,
    checked_at: str = NOW,
) -> Any:
    def fetch_stub(url: str) -> Any:
        if fetch is not None:
            return fetch(url)
        return channel

    return check_for_updates(
        fetch_channel=fetch_stub,
        installed=INSTALLED if installed is None else installed,
        checked_at=checked_at,
        policy=policy,
    )


# --- A01: ordering -------------------------------------------------------------------


def test_equal_versions_answer_up_to_date() -> None:
    assert _check(_channel("0.1.0")).status == "up_to_date"


def test_a_newer_recommendation_answers_update_available_with_the_candidate() -> None:
    result = _check(_channel("0.2.0"))
    assert result.status == "update_available"
    assert result.candidate_version == "0.2.0"
    assert result.release_url is not None
    assert result.release_url.endswith("/core-v0.2.0")


def test_an_older_recommendation_answers_ahead_of_channel() -> None:
    assert _check(_channel("0.0.9")).status == "ahead_of_channel"


def test_an_explicitly_paused_channel_answers_no_release() -> None:
    assert _check(_channel(None)).status == "no_release"


def test_version_ordering_is_dotted_integer_not_string_sorting() -> None:
    assert _check(_channel("0.1.10")).status == "update_available"


def test_the_candidate_carries_the_channels_own_per_package_versions() -> None:
    """The map goes through verbatim, which is what the coordinator installs.

    A per-package map is the shape the channel actually publishes, and the five
    distributions are not obliged to move in lockstep. The check used to drop the
    map and the coordinator then rebuilt it as five copies of one release number,
    so a release where one package did not move would have been staged and then
    refused for carrying the version the channel recommended.
    """
    uneven = _body("0.2.0")
    assert uneven["release"] is not None
    uneven["release"]["packages"]["omnivia-core-mcp"] = "0.3.0"
    result = _check(_sign(uneven))
    assert result.status == "update_available"
    assert result.candidate_packages == {
        "omnivia-core": "0.2.0",
        "omnivia-core-runtime": "0.2.0",
        "omnivia-core-client": "0.2.0",
        "omnivia-core-cli": "0.2.0",
        "omnivia-core-mcp": "0.3.0",
    }


def test_the_rendered_document_is_unchanged_by_the_per_package_map() -> None:
    """``to_wire`` is the pinned CLI surface, so the new field stays off it."""
    document = _check(_channel("0.2.0")).to_wire()
    assert set(document) == {
        "update_check_adapter_version",
        "product_id",
        "status",
        "installed",
        "checked_at",
        "channel_url",
        "candidate_version",
        "release_url",
        "bundle_sha256",
    }


# --- A02: failures are never "up to date" ---------------------------------------------


def test_a_network_failure_answers_check_failed_never_up_to_date() -> None:
    def fetch(url: str) -> Any:
        raise UpdateCheckError("check_failed", "the channel was unreachable")

    result = _check(None, fetch=fetch)
    assert result.status == "check_failed"
    assert result.reason == "the channel was unreachable"


def test_a_transport_refusal_answers_check_failed_rather_than_escaping() -> None:
    """An oversized or non-HTTPS channel is a status, not a traceback.

    ``fetch_https_bytes`` raises ``TransportError``, which is a ``ClientError``
    and not an ``OSError``. It used to fall through every handler here and reach
    the CLI as an unhandled exception -- the one thing a check that answers
    honestly must not do.
    """
    from omnivia_core_client.errors import TransportError

    def fetch(url: str) -> Any:
        raise TransportError("the fetched document exceeds its size limit")

    result = _check(None, fetch=fetch)
    assert result.status == "check_failed"
    assert "size limit" in (result.reason or "")


def test_an_installed_version_outside_the_ordering_blames_the_installation() -> None:
    """A ``0.1.0rc1`` on disk is a fact about this installation, not the channel.

    It used to be compared outside every handler and escape as an uncaught
    refusal whose words sent the reader to the channel to look for a version that
    was never there.
    """
    installed = dict(INSTALLED)
    installed["omnivia-core-cli"] = "0.1.0rc1"
    result = _check(_channel("0.1.0"), installed=installed)
    assert result.status == "unsupported_install"
    assert "0.1.0rc1" in (result.reason or "")


def test_a_malformed_channel_answers_check_failed() -> None:
    result = _check(canonical_bytes({"schema_version": ENVELOPE_SCHEMA_VERSION}))
    assert result.status == "check_failed"


def test_an_unknown_schema_version_answers_check_failed() -> None:
    result = _check(_channel("0.1.0", schema_version="omnivia-core-update.v9"))
    assert result.status == "check_failed"


def test_an_unknown_channel_member_answers_check_failed() -> None:
    result = _check(_channel("0.1.0", auto_install=True))
    assert result.status == "check_failed"


def test_a_non_https_release_url_answers_check_failed() -> None:
    body = _body("0.1.0")
    assert body["release"] is not None
    body["release"]["release_url"] = "http://github.com/claytonread/omnivia-core/releases/tag/x"
    assert _check(_sign(body)).status == "check_failed"


def test_a_partial_package_map_answers_check_failed() -> None:
    body = _body("0.1.0")
    assert body["release"] is not None
    body["release"]["packages"].pop("omnivia-core-mcp")
    assert _check(_sign(body)).status == "check_failed"


def test_a_recommendation_without_a_bundle_digest_answers_check_failed() -> None:
    """An archive this installation cannot identify is one it must not unpack.

    The digest is optional in the legacy shape and required in the signed one, so
    a correctly signed envelope that omits it is sound and unusable.
    """
    body = _body("0.2.0")
    assert body["release"] is not None
    del body["release"]["bundle_sha256"]
    result = _check(_sign(body))
    assert result.status == "check_failed"


# --- the trust decision: every refusal is one honest status ---------------------------


@pytest.mark.parametrize(
    ("label", "document", "refusal"),
    [
        (
            "another product",
            _sign(_body(product_id="omnivia-apps"), domain_product="omnivia-apps"),
            "channel_wrong_product",
        ),
        (
            "another product's domain over this product's body",
            _sign(_body(), domain_product="omnivia-apps"),
            "channel_bad_signature",
        ),
        ("another channel", _sign(_body(channel="beta")), "channel_wrong_channel"),
        (
            "an unapproved key",
            _sign(
                _body(signing={"key_id": OTHER_KEY_ID, "algorithm": "ed25519"}),
                signer=OTHER_SIGNER,
            ),
            "channel_untrusted_key",
        ),
        (
            "a duplicated member name",
            b'{"channel":"stable","channel":"beta"}',
            "channel_duplicate_member",
        ),
        (
            "a non-canonical spelling",
            b'{\n  "schema_version": "omnivia-core-update.v2"\n}',
            "channel_noncanonical",
        ),
    ],
)
def test_a_document_that_cannot_be_believed_is_check_failed(
    label: str, document: bytes, refusal: str
) -> None:
    """One status for every trust refusal, and the code beside it.

    The status is what the CLI renders and the code is what a consumer branches
    on. Both are asserted because a refusal recovered by reading the prose is a
    refusal whose prose can never be improved.
    """
    result = _check(document)
    assert result.status == "check_failed", label
    with pytest.raises(UpdateCheckError) as refused:
        validate_channel_document(document, policy=POLICY, verified_at=NOW)
    assert refused.value.refusal == refusal, label


def test_an_envelope_read_outside_its_validity_window_is_refused() -> None:
    """The bound on how long a captured envelope is worth anything."""
    document = _channel("0.2.0")
    assert _check(document, checked_at="2026-09-15T00:00:00Z").status == "update_available"
    assert _check(document, checked_at="2026-11-01T00:00:00Z").status == "check_failed"
    assert _check(document, checked_at="2026-08-01T00:00:00Z").status == "check_failed"


@pytest.mark.parametrize(
    "window",
    [
        {"not_before": "2026-10-01T00:00:00Z", "not_after": "2027-01-01T00:00:00Z"},
        {"not_before": "2026-01-01T00:00:00Z", "not_after": "2026-02-01T00:00:00Z"},
        {"retired_at": "2026-06-01T00:00:00Z"},
    ],
    ids=["not-yet-valid", "expired", "retired-early"],
)
def test_a_key_outside_its_window_is_one_answer(window: dict[str, str]) -> None:
    """Unknown, not yet valid, expired and retired are the same answer.

    Nobody currently approved to issue a channel issued this one, and splitting
    that into four statuses would publish which of this installation's anchors is
    in which state.
    """
    policy = UpdateTrustPolicy(trusted_keys=(_anchor(KEY_ID, SIGNER, **window),))
    result = _check(_channel("0.2.0"), policy=policy)
    assert result.status == "check_failed"
    with pytest.raises(UpdateCheckError) as refused:
        validate_channel_document(_channel("0.2.0"), policy=policy, verified_at=NOW)
    assert refused.value.refusal == "channel_untrusted_key"


def test_rotation_needs_no_flag_day() -> None:
    """Two anchors approved at once, so either key may have signed."""
    policy = UpdateTrustPolicy(
        trusted_keys=(_anchor(KEY_ID, SIGNER), _anchor(OTHER_KEY_ID, OTHER_SIGNER))
    )
    second = _sign(
        _body("0.2.0", signing={"key_id": OTHER_KEY_ID, "algorithm": "ed25519"}),
        signer=OTHER_SIGNER,
    )
    assert _check(second, policy=policy).status == "update_available"
    assert _check(_channel("0.2.0"), policy=policy).status == "update_available"


def test_a_replayed_envelope_is_refused_against_a_durable_floor() -> None:
    """The rule a caller that keeps a floor gets. Nothing here keeps one yet."""
    policy = UpdateTrustPolicy(trusted_keys=(TRUSTED,), minimum_sequence=4)
    assert _check(_channel("0.2.0"), policy=policy).status == "check_failed"
    ahead = UpdateTrustPolicy(trusted_keys=(TRUSTED,), minimum_sequence=3)
    assert _check(_channel("0.2.0"), policy=ahead).status == "update_available"


def test_a_downgrade_is_refused_against_a_declared_floor() -> None:
    """A signature is not a reason to move backwards.

    The floor is caller-supplied and deliberately not derived from the installed
    versions: an installation ahead of its channel is ``ahead_of_channel``, and
    taking the floor from what is installed would turn that honest answer into a
    trust failure.
    """
    floor = UpdateTrustPolicy(
        trusted_keys=(TRUSTED,), version_floor=(("omnivia-core", "0.5.0"),)
    )
    assert _check(_channel("0.2.0"), policy=floor).status == "check_failed"
    assert _check(_channel("0.0.9")).status == "ahead_of_channel"


def test_the_legacy_unsigned_shape_is_refused_unless_a_policy_asks_for_it() -> None:
    """v1 is readable, and only by a policy that says the word.

    The same bytes, twice. Production refuses them; a policy built by
    ``development_trust_policy`` admits them. There is no argument on the
    production policy that changes its answer, which is the difference between a
    switch and a default.
    """
    document = _legacy("0.2.0")
    refused = _check(document, policy=PRODUCTION_TRUST_POLICY)
    assert refused.status == "check_failed"
    with pytest.raises(UpdateCheckError) as failure:
        validate_channel_document(
            document, policy=PRODUCTION_TRUST_POLICY, verified_at=NOW
        )
    assert failure.value.refusal == "channel_unsigned_refused"

    admitted = _check(document, policy=development_trust_policy())
    assert admitted.status == "update_available"
    assert admitted.bundle_sha256 is None


def test_the_production_policy_refuses_a_signed_envelope_with_no_anchor_bound() -> None:
    """The honest state of this repository, asserted rather than assumed.

    No production channel-signing key has been published, so the packaged anchor
    set is empty and production verification refuses every envelope -- including
    a correctly signed one. A fabricated public anchor would be worse than none,
    because it would look like verification.
    """
    assert PACKAGED_TRUSTED_KEYS == ()
    assert PRODUCTION_TRUST_POLICY.trusted_keys == ()
    assert PRODUCTION_TRUST_POLICY.accept_unsigned_v1 is False
    result = _check(_channel("0.2.0"), policy=PRODUCTION_TRUST_POLICY)
    assert result.status == "check_failed"
    with pytest.raises(UpdateCheckError) as refused:
        validate_channel_document(
            _channel("0.2.0"), policy=PRODUCTION_TRUST_POLICY, verified_at=NOW
        )
    assert refused.value.refusal == "channel_untrusted_key"


def test_a_development_policy_cannot_be_reached_from_the_production_one() -> None:
    """Frozen, so the switch is a constructor argument and never a mutation."""
    with pytest.raises((AttributeError, TypeError)):
        PRODUCTION_TRUST_POLICY.accept_unsigned_v1 = True  # type: ignore[misc]


# --- eligibility: the v0.4 §11.3 fallbacks -------------------------------------------


def test_an_editable_checkout_answers_unsupported_install_with_the_reason() -> None:
    installed = dict(INSTALLED)
    installed["omnivia-core-cli"] = "0.1.0+editable"
    result = _check(_channel("0.1.0"), installed=installed)
    assert result.status == "unsupported_install"
    assert "editable" in result.reason


def test_a_partial_installation_answers_unsupported_install() -> None:
    installed = dict(INSTALLED)
    del installed["omnivia-core-mcp"]
    result = _check(_channel("0.1.0"), installed=installed)
    assert result.status == "unsupported_install"
    assert "omnivia-core-mcp" in result.reason


def test_mixed_versions_answer_unsupported_install_not_update_available() -> None:
    installed = dict(INSTALLED)
    installed["omnivia-core"] = "0.2.0"
    installed["omnivia-core-cli"] = "0.0.9"
    result = _check(_channel("0.1.0"), installed=installed)
    assert result.status == "unsupported_install"


# --- the production provider ---------------------------------------------------------


def test_installed_packages_maps_missing_distributions_away(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = {"omnivia-core": "0.1.0"}

    def probe(name: str) -> tuple[str, str | None]:
        if name in versions:
            return versions[name], None
        raise importlib_error(name)

    def importlib_error(name: str) -> Exception:
        import importlib.metadata

        return importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(
        "omnivia_core_client.updates._probe_distribution",
        probe,
    )
    installed = installed_packages()
    assert installed == {"omnivia-core": "0.1.0"}


def test_the_default_channel_url_is_the_first_party_https_location() -> None:
    assert DEFAULT_UPDATE_CHANNEL_URL.startswith("https://raw.githubusercontent.com/")
    assert DEFAULT_UPDATE_CHANNEL_URL.endswith("channel.json")


def test_the_channel_size_limit_is_bounded() -> None:
    assert MAX_CHANNEL_BYTES == 64 * 1024


def test_the_bundle_size_limit_is_bounded() -> None:
    """The release asset gets a cap too. It had none, on either fetch path."""
    assert 0 < MAX_BUNDLE_BYTES <= 1024 * 1024 * 1024


def test_both_production_fetches_refuse_a_non_https_address() -> None:
    """Checked at the call site and inside the one approved reader."""
    from omnivia_core_client.http_transport import fetch_https_bytes
    from omnivia_core_client.updates import default_fetch_bundle, default_fetch_channel

    for fetch in (default_fetch_channel, default_fetch_bundle):
        with pytest.raises(UpdateCheckError):
            fetch("http://example.invalid/channel.json")
    from omnivia_core_client.errors import TransportError

    with pytest.raises(TransportError):
        fetch_https_bytes(
            "http://127.0.0.1/channel.json", timeout_seconds=1.0, max_bytes=16
        )


def test_the_approved_reader_declines_every_redirect() -> None:
    """The docstring used to claim this and the code used to follow them.

    ``urllib.request``'s default opener follows 3xx, including ``https`` to
    ``http``, choosing the next address from a header the peer wrote. Asserted on
    the handler rather than over a socket, because no test here opens one.
    """
    from omnivia_core_client.http_transport import _RefuseRedirect

    handler = _RefuseRedirect()
    assert (
        handler.redirect_request(None, None, 302, "Found", {}, "http://elsewhere.invalid")
        is None
    )


def test_the_validator_accepts_null_release_and_the_full_shape() -> None:
    paused = validate_channel_document(_channel(None), policy=POLICY, verified_at=NOW)
    assert paused is None
    release = validate_channel_document(
        _channel("0.2.0"), policy=POLICY, verified_at=NOW
    )
    assert release is not None
    assert release.version == "0.2.0"
    assert set(release.packages) == set(INSTALLED)
    assert release.bundle_sha256 == DIGEST
    assert release.signed is True
