"""Whether one fetched channel document may be believed at all.

This is the trust half of the update path, and it is deliberately separate from
:mod:`omnivia_core_client.updates`, which owns the *shape* of a recommendation
and the version comparison. The split is where the two questions differ: nothing
here knows what a release recommends, and nothing there decides whether the
document arrived from the product's release authority.

**The production shape is the signed envelope v2.** The signature is Ed25519 over
``domain`` ``NUL`` ``product_id`` ``NUL`` ``canonical_json(envelope_without_signature)``.
The product id is in the domain prefix *as well as* inside the signed body, which
is the whole point of the prefix: a correctly signed channel for another product
cannot be replayed as one for this product even when the same key signed both,
because the bytes that key signed were never these bytes. :data:`SIGNATURE_DOMAIN`
carries its own version, so a v3 envelope signed by a still-approved key is not a
v2 envelope either.

**Strict in both directions.** A missing member and an unknown member are equally
refused. The second half is the one worth stating: an unknown member is inside the
signed bytes, so "ignore what you do not understand" would mean verifying a
signature over a document that was not read.

**The bytes are judged, not a parsed value.** The document is parsed through
Core's own :func:`~omnivia_core.contracts.v1.canonical_json.parse_json_document`,
which is the only point at which a duplicated member name is still visible --
``json.loads`` keeps the last occurrence and drops the rest, so a mapping built
from such a document has no single value to verify a signature over. The text is
then required to be the RFC 8785 canonical form of what it parsed to, so one
document has exactly one spelling and a signature over it cannot be lifted onto a
re-spelled copy.

**Trust anchors are an input, never a field of the document.** A channel that
could nominate the key that verifies it is self-certifying. Rotation is expressed
entirely by the key windows, so two keys may be approved at once and a transition
needs no flag day; unknown, not yet valid, expired and retired are one answer to a
consumer -- this channel was not issued by anybody currently approved to issue one.

**v1 is not a production shape.** The unsigned document that preceded the envelope
is admitted only by a policy that explicitly says it is a test or development
policy. :data:`PRODUCTION_TRUST_POLICY` does not, and there is no argument,
environment read or code path here that turns that on for it.

Standard library plus Core's public canonical-JSON module, and ``cryptography``
for the one thing the standard library has no Ed25519 for. That import is lazy and
its absence is a refusal rather than a pass: see :func:`_verify_ed25519`.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import ContractSemanticError
from omnivia_core.contracts.v1.canonical_json import (
    canonical_bytes,
    parse_json_document,
)

__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "LEGACY_SCHEMA_VERSION",
    "PACKAGED_TRUSTED_KEYS",
    "PRODUCTION_TRUST_POLICY",
    "SIGNATURE_ALGORITHM",
    "SIGNATURE_DOMAIN",
    "AdmittedChannel",
    "EnvelopeRefused",
    "TrustedUpdateKey",
    "UpdateTrustPolicy",
    "admit_channel_document",
    "decode_base64url",
    "development_trust_policy",
    "signing_input",
]

#: The signed production shape, and the unsigned one that preceded it.
ENVELOPE_SCHEMA_VERSION: Final = "omnivia-core-update.v2"
LEGACY_SCHEMA_VERSION: Final = "omnivia-core-update.v1"

#: Domain separation for the channel signature. The product id follows it in the
#: signing input, so the domain is per-product as well as per-version.
SIGNATURE_DOMAIN: Final = "omnivia.update-channel.v2"

#: The only signature algorithm v2 admits.
SIGNATURE_ALGORITHM: Final = "ed25519"

#: An Ed25519 public key is 32 bytes and a signature is 64, which in unpadded
#: base64url is exactly 43 and 86 characters. Named because the length is a shape
#: check and not only a bound.
_PUBLIC_KEY_BYTES: Final = 32
_SIGNATURE_BYTES: Final = 64

#: The envelope's exact member set, and the exact member set of its two objects.
_ENVELOPE_MEMBERS: Final = frozenset(
    {
        "schema_version",
        "product_id",
        "channel",
        "sequence",
        "issued_at",
        "not_before",
        "not_after",
        "release",
        "signing",
        "signature",
    }
)
_SIGNING_MEMBERS: Final = frozenset({"key_id", "algorithm"})
_LEGACY_MEMBERS: Final = frozenset({"schema_version", "channel", "release"})

#: ``sequence`` is a JSON integer, and the ceiling is the one every JSON consumer
#: can represent exactly. Above it two sequences can compare equal, which would
#: make a replay floor meaningless.
_MAX_SEQUENCE: Final = 9007199254740991

_KEY_ID_RE: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PRODUCT_ID_RE: Final = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
_CHANNEL_RE: Final = re.compile(r"\A[a-z0-9][a-z0-9-]{0,31}\Z")

#: The one instant grammar this contract admits: fixed width, UTC, seconds.
#:
#: Fixed width and UTC is what lets every comparison below be a *string*
#: comparison. That is not a shortcut around :mod:`datetime`; it is the reason the
#: grammar is pinned this narrowly. Two instants in this form order
#: lexicographically exactly as they order in time, with no timezone to be naive
#: about, no offset to normalise and no leap-second branch -- and anything outside
#: the grammar is refused before it is compared, so there is no value that could
#: order wrongly.
_INSTANT_RE: Final = re.compile(
    r"\A[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
    r"T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z\Z"
)

#: The base64url alphabet, and nothing else. No padding, no ``+``, no ``/``.
_BASE64URL_RE: Final = re.compile(r"\A[A-Za-z0-9_-]+\Z")

class EnvelopeRefused(Exception):
    """One closed refusal code, and nothing else.

    ``str()`` of it is the wire code, so a caller that logs the exception
    carelessly still cannot leak a document, a URL or key material through this
    type. The caller turns it into its own status; nothing here decides that.
    """

    def __init__(self, refusal: str) -> None:
        super().__init__(refusal)
        self.refusal = refusal


def _refuse(refusal: str) -> EnvelopeRefused:
    return EnvelopeRefused(refusal)


@dataclass(frozen=True, slots=True)
class TrustedUpdateKey:
    """One approved channel-signing key and the window it is approved in."""

    key_id: str
    public_key: bytes
    not_before: str
    not_after: str
    retired_at: str | None = None
    algorithm: str = SIGNATURE_ALGORITHM

    def __post_init__(self) -> None:
        # A malformed anchor raises rather than producing a refusal code: anchors
        # are the *caller's* authority, not channel metadata, so a broken one is a
        # defect in the calling program and must be loud.
        if self.algorithm != SIGNATURE_ALGORITHM:
            raise ValueError(f"unsupported signature algorithm {self.algorithm!r}")
        if _KEY_ID_RE.match(self.key_id) is None:
            raise ValueError("trust anchor key id is malformed")
        if len(self.public_key) != _PUBLIC_KEY_BYTES:
            raise ValueError("an Ed25519 public key is 32 bytes")
        for moment in (self.not_before, self.not_after, self.retired_at):
            if moment is not None and _INSTANT_RE.match(moment) is None:
                raise ValueError("trust anchor instants must be UTC second-precision")
        if self.not_before >= self.not_after:
            raise ValueError("trust anchor window is empty")

    @classmethod
    def from_document(cls, document: Any) -> TrustedUpdateKey:
        """Build one anchor from its published JSON shape."""
        expected = {"key_id", "algorithm", "public_key", "not_before", "not_after"}
        if not isinstance(document, dict):
            raise TypeError("a trust anchor document is a JSON object")
        if set(document) - (expected | {"retired_at"}) or not expected <= set(document):
            raise ValueError(
                f"trust anchor document has unexpected members: {sorted(document)}"
            )
        retired = document.get("retired_at")
        return cls(
            key_id=str(document["key_id"]),
            public_key=decode_base64url(
                str(document["public_key"]), expected_bytes=_PUBLIC_KEY_BYTES
            ),
            not_before=str(document["not_before"]),
            not_after=str(document["not_after"]),
            retired_at=None if retired is None else str(retired),
            algorithm=str(document["algorithm"]),
        )

    def usable_at(self, moment: str) -> bool:
        """Whether this key may verify a channel at ``moment``."""
        if not self.not_before <= moment < self.not_after:
            return False
        return self.retired_at is None or moment < self.retired_at


#: The channel-signing keys this distribution ships as trusted.
#:
#: **Empty, and honestly so.** No production update-signing key has been
#: published for this product yet, and a public key invented here would be a
#: trust anchor nobody holds the private half of -- which is worse than none,
#: because it looks like verification. Until one is bound, production
#: verification refuses every envelope with ``channel_untrusted_key``, which is
#: the correct answer to "was this issued by somebody approved to issue it" while
#: the answer is nobody. A caller that holds the authority passes its own anchors
#: in, and the corpus under ``contracts/update/v1`` carries test-only keys.
#:
#: Declared below :class:`TrustedUpdateKey` rather than above it so the
#: annotation is the class itself and not a string naming it.
PACKAGED_TRUSTED_KEYS: Final[tuple[TrustedUpdateKey, ...]] = ()


@dataclass(frozen=True, slots=True)
class UpdateTrustPolicy:
    """What this installation will believe, and from whom.

    ``accept_unsigned_v1`` is the only development switch, and it is deliberately
    a constructor argument of a frozen object rather than a module flag or an
    environment read: a test that wants the legacy shape builds a policy that says
    so, and no caller can persuade :data:`PRODUCTION_TRUST_POLICY` to say it.

    ``minimum_sequence`` and ``version_floor`` are the two replay floors, for a
    caller that keeps one durably. **Neither is populated by the check path, and
    that is deliberate twice over.** There is no durable record in this package of
    the last channel an installation accepted, so there is nothing honest to fill
    them from; and the installed versions are specifically *not* a version floor,
    because an installation ahead of its channel is the ``ahead_of_channel``
    status rather than a refusal, and deriving the floor from what is installed
    would turn that honest answer into a trust failure. The defence a caller
    without a durable floor still has is the validity window, plus the fact that
    an envelope recommending a version at or below the installed one produces no
    update at all. The rules are stated, enforced and published here so a caller
    that does keep a floor gets them, rather than being implied and absent.
    """

    trusted_keys: tuple[TrustedUpdateKey, ...] = PACKAGED_TRUSTED_KEYS
    expected_product_id: str = "omnivia-core"
    expected_channel: str = "stable"
    accept_unsigned_v1: bool = False
    minimum_sequence: int | None = None
    #: The release floor a recommendation may not go below, as ``(package,
    #: version)`` pairs. A tuple rather than a mapping because a policy is the
    #: rule a document is judged by, and a caller must not be able to edit one it
    #: has already handed over.
    version_floor: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if _PRODUCT_ID_RE.match(self.expected_product_id) is None:
            raise ValueError("the expected product id is malformed")
        if _CHANNEL_RE.match(self.expected_channel) is None:
            raise ValueError("the expected channel name is malformed")
        seen: set[str] = set()
        for key in self.trusted_keys:
            if key.key_id in seen:
                raise ValueError(f"two trust anchors share the key id {key.key_id!r}")
            seen.add(key.key_id)


#: The production policy: packaged anchors, the signed envelope, no legacy shape.
PRODUCTION_TRUST_POLICY: Final = UpdateTrustPolicy()


def development_trust_policy(**overrides: Any) -> UpdateTrustPolicy:
    """A policy that admits the unsigned v1 shape, for tests and development only.

    A named constructor rather than a keyword on the production policy, so every
    call site that admits an unsigned channel says the word "development" at the
    point where it does it.
    """
    return UpdateTrustPolicy(accept_unsigned_v1=True, **overrides)


def decode_base64url(text: str, *, expected_bytes: int) -> bytes:
    """Decode canonical unpadded base64url of exactly ``expected_bytes`` bytes.

    Canonical means one spelling. :func:`base64.urlsafe_b64decode` is looser than
    that in three ways that matter here, and each is refused rather than
    normalised:

    * the standard alphabet's ``+`` and ``/`` are not base64url, and
      ``urlsafe_b64decode`` does not reject them -- it translates them;
    * padding is not part of the canonical spelling, and a decoder that strips it
      accepts two spellings of one key;
    * the trailing bits of a final partial group are *unused*, and a decoder that
      ignores them accepts several texts that decode to the same bytes. A
      signature with more than one spelling is a signature a filter, a log line or
      a replay check cannot match, so the decode is re-encoded and compared.

    ``ValueError`` on any of those, because this is called both while validating a
    caller's own anchors -- where a malformed key is a defect in the calling
    program -- and from inside the envelope path, which turns it into a refusal.
    """
    if not isinstance(text, str) or _BASE64URL_RE.match(text) is None:
        raise ValueError("not canonical base64url")
    # Unpadded base64url of n bytes is exactly ceil(n * 4 / 3) characters.
    if len(text) != -(-expected_bytes * 4 // 3):
        raise ValueError(f"not {expected_bytes} bytes of canonical base64url")
    decoded = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(decoded) != expected_bytes:
        raise ValueError(f"not {expected_bytes} bytes of canonical base64url")
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != text:
        # Non-zero unused trailing bits: a second spelling of these bytes.
        raise ValueError("not canonical base64url")
    return decoded


def signing_input(body: dict[str, Any], *, product_id: str) -> bytes:
    """The exact bytes a channel signature covers.

    ``body`` is the envelope without its ``signature`` member. Each part of the
    prefix is NUL-terminated so no two different (domain, product) pairs can
    produce one byte string, which is the property that makes the separation real
    rather than decorative.
    """
    return b"".join(
        (
            SIGNATURE_DOMAIN.encode("ascii"),
            b"\x00",
            product_id.encode("ascii"),
            b"\x00",
            canonical_bytes(body),
        )
    )


def _verify_ed25519(public_key: bytes, signature: bytes, message: bytes) -> bool:
    """Whether ``signature`` is Ed25519-valid over ``message`` under ``public_key``.

    The import is lazy and inside the function because ``cryptography`` is an
    optional extra of this distribution rather than a dependency of it: this
    package's dependency edge is the public Core contract and nothing else, and
    the standard library has no Ed25519. Every real Core installation already has
    the library, because ``omnivia-core-runtime`` requires it and the update path
    refuses to run at all unless all five first-party distributions are installed.

    Its absence is a refusal, never a pass. A verifier that cannot verify has not
    verified, and the one thing this function must never do is answer ``True``
    because it could not ask.
    """
    verified = False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as failure:
        raise _refuse("channel_verifier_unavailable") from failure
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
        verified = True
    except (InvalidSignature, ValueError):
        verified = False
    return verified


@dataclass(frozen=True, slots=True)
class AdmittedChannel:
    """One channel document this installation may act on.

    ``release`` is the raw recommendation member, still to be validated for shape
    by :mod:`omnivia_core_client.updates`: this module decided the document may be
    believed, not what it says.
    """

    schema_version: str
    channel: str
    product_id: str
    release: dict[str, Any] | None
    sequence: int | None = None
    key_id: str | None = None
    signed: bool = False


def _parsed_and_canonical(raw: bytes | str) -> dict[str, Any]:
    """The document as a mapping, refusing anything no signature could cover.

    Three refusals, in the order they become invisible: invalid UTF-8 or invalid
    JSON, then a duplicated member name -- the only point at which it is still
    there -- then a text that is not the canonical form of what it parsed to.

    The canonical-form requirement is about the document having one spelling, not
    about the signature: the signing input is derived from the *parse*, so a
    re-spelled copy would verify either way. What it actually buys is that the
    number and string spellings outside the canonical subset never reach the
    checks below -- ``"sequence": 1e3`` parses to a float, and a float compared
    against a replay floor is a question nobody asked. A single trailing newline
    is stripped before the comparison and nothing else is: a byte of file framing
    is not a spelling of the document, and requiring its absence would make an
    editor's "insert final newline" a channel outage.
    """
    document: Any
    try:
        document = parse_json_document(raw)
    except ContractSemanticError as failure:
        if "duplicate object member name" in str(failure):
            raise _refuse("channel_duplicate_member") from failure
        raise _refuse("channel_malformed") from failure
    if not isinstance(document, dict):
        raise _refuse("channel_malformed")
    text = bytes(raw) if isinstance(raw, (bytes, bytearray)) else raw.encode("utf-8")
    try:
        canonical = canonical_bytes(document)
    except ContractSemanticError as failure:
        raise _refuse("channel_malformed") from failure
    if text.removesuffix(b"\n") != canonical:
        raise _refuse("channel_noncanonical")
    return document


def _instant(value: Any, refusal: str) -> str:
    if not isinstance(value, str) or _INSTANT_RE.match(value) is None:
        raise _refuse(refusal)
    return value


def admit_channel_document(
    raw: bytes | str, *, policy: UpdateTrustPolicy, verified_at: str
) -> AdmittedChannel:
    """Decide whether these exact fetched bytes may be believed, or refuse.

    ``verified_at`` is the instant the validity window and the key window are
    judged at, in the contract's UTC second-precision grammar. It is an explicit
    argument rather than a clock read, for the same reason the trust anchors are:
    one corpus then produces the same verdicts everywhere, and a caller cannot be
    told a different time by this module than by its own records.
    """
    moment = _instant(verified_at, "channel_malformed")
    document = _parsed_and_canonical(raw)
    version = document.get("schema_version")
    if version == LEGACY_SCHEMA_VERSION:
        if not policy.accept_unsigned_v1:
            raise _refuse("channel_unsigned_refused")
        return _admit_legacy(document, policy=policy)
    if version != ENVELOPE_SCHEMA_VERSION:
        raise _refuse("channel_unsupported_version")
    return _admit_envelope(document, policy=policy, moment=moment)


def _admit_legacy(
    document: dict[str, Any], *, policy: UpdateTrustPolicy
) -> AdmittedChannel:
    """The unsigned shape, under a policy that explicitly asked for it.

    Still strict about its members: the development policy loosens who signed the
    document, not what the document may contain.
    """
    if set(document) != _LEGACY_MEMBERS:
        raise _refuse("channel_malformed")
    channel = document["channel"]
    if not isinstance(channel, str) or _CHANNEL_RE.match(channel) is None:
        raise _refuse("channel_malformed")
    if channel != policy.expected_channel:
        raise _refuse("channel_wrong_channel")
    release = document["release"]
    if release is not None and not isinstance(release, dict):
        raise _refuse("channel_malformed")
    return AdmittedChannel(
        schema_version=LEGACY_SCHEMA_VERSION,
        channel=channel,
        product_id=policy.expected_product_id,
        release=release,
        signed=False,
    )


def _admit_envelope(
    document: dict[str, Any], *, policy: UpdateTrustPolicy, moment: str
) -> AdmittedChannel:
    """The signed shape. Every refusal below happens before the signature is
    trusted, and the signature itself is checked before any field is believed.
    """
    if set(document) != _ENVELOPE_MEMBERS:
        raise _refuse("channel_malformed")

    product_id = document["product_id"]
    if not isinstance(product_id, str) or _PRODUCT_ID_RE.match(product_id) is None:
        raise _refuse("channel_malformed")
    channel = document["channel"]
    if not isinstance(channel, str) or _CHANNEL_RE.match(channel) is None:
        raise _refuse("channel_malformed")
    sequence = document["sequence"]
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 1 <= sequence <= _MAX_SEQUENCE
    ):
        raise _refuse("channel_malformed")
    issued_at = _instant(document["issued_at"], "channel_malformed")
    not_before = _instant(document["not_before"], "channel_malformed")
    not_after = _instant(document["not_after"], "channel_malformed")
    if not_before >= not_after or issued_at > not_after:
        raise _refuse("channel_malformed")
    release = document["release"]
    if release is not None and not isinstance(release, dict):
        raise _refuse("channel_malformed")
    signing = document["signing"]
    if not isinstance(signing, dict) or set(signing) != _SIGNING_MEMBERS:
        raise _refuse("channel_malformed")
    key_id = signing["key_id"]
    if not isinstance(key_id, str) or _KEY_ID_RE.match(key_id) is None:
        raise _refuse("channel_malformed")
    if signing["algorithm"] != SIGNATURE_ALGORITHM:
        raise _refuse("channel_bad_signature")

    # Who this document claims to be for, before who signed it: a wrong-product
    # or wrong-channel envelope is refused on its own terms, and would also fail
    # the signature check below because the product id is in the signing input.
    if product_id != policy.expected_product_id:
        raise _refuse("channel_wrong_product")
    if channel != policy.expected_channel:
        raise _refuse("channel_wrong_channel")

    signature: bytes | None = None
    try:
        signature = decode_base64url(
            document["signature"], expected_bytes=_SIGNATURE_BYTES
        )
    except ValueError:
        signature = None
    if signature is None:
        raise _refuse("channel_bad_signature")

    approved = next(
        (key for key in policy.trusted_keys if key.key_id == key_id), None
    )
    if approved is None or not approved.usable_at(moment):
        # Unknown, not yet valid, expired and retired are one answer: this channel
        # was not issued by anybody currently approved to issue one.
        raise _refuse("channel_untrusted_key")

    body = {name: value for name, value in document.items() if name != "signature"}
    if not _verify_ed25519(
        approved.public_key, signature, signing_input(body, product_id=product_id)
    ):
        raise _refuse("channel_bad_signature")

    # Only now is anything this document says believed, so the window and the
    # replay floor are checked on verified facts rather than on claimed ones.
    if not not_before <= moment < not_after:
        raise _refuse("channel_outside_validity")
    if policy.minimum_sequence is not None and sequence <= policy.minimum_sequence:
        raise _refuse("channel_replayed")
    _refuse_downgrade(release, policy=policy)
    return AdmittedChannel(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        channel=channel,
        product_id=product_id,
        release=release,
        sequence=sequence,
        key_id=key_id,
        signed=True,
    )


def _version_key(value: Any) -> tuple[int, ...] | None:
    """Core's dotted-integer ordering, or ``None`` for anything outside it."""
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(".")
    if any(
        not part.isascii() or not part.isdigit() or (len(part) > 1 and part[0] == "0")
        for part in parts
    ):
        return None
    return tuple(int(part) for part in parts)


def _refuse_downgrade(release: Any, *, policy: UpdateTrustPolicy) -> None:
    """Refuse a recommendation that moves any package backwards.

    A correctly signed envelope that recommends an older release is the shape a
    captured one takes, and it is the shape a mistaken republication takes too.
    Either way the answer is the same: this installation does not move backwards
    on the strength of a signature.

    An unparseable version is left alone here. The shape validator refuses it with
    its own words, and a trust refusal for a malformed version would say the wrong
    thing about why the document was rejected.
    """
    if not policy.version_floor or not isinstance(release, dict):
        return
    packages = release.get("packages")
    if not isinstance(packages, dict):
        return
    for name, floor in policy.version_floor:
        recommended = _version_key(packages.get(name))
        installed = _version_key(floor)
        if recommended is not None and installed is not None and recommended < installed:
            raise _refuse("channel_downgrade")
