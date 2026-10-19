"""The ``service update-check`` machinery: one channel, six honest statuses.

Implements the v0.4 shared-update-spec §5 for the Core installation: resolve
the installed first-party packages, fetch and validate this installation's
update channel, compare versions with Core's own dotted-integer ordering (the
same rule the contract-compatibility windows use — full PEP 440 pre/post
segments are deliberately out of scope until a channel needs them), and answer
with one of six statuses:

``up_to_date`` / ``update_available`` / ``ahead_of_channel`` /
``no_release`` / ``unsupported_install`` / ``check_failed``.

The failure statuses are the honest ones: a channel that cannot be fetched,
parsed, trusted or compared is never reported as "up to date". An ambiguous or
partial installation is ``unsupported_install`` with the explicit reason, not
an invented "modified installation" claim. The check installs nothing, stops
nothing and downloads no release assets: it is discovery only.

**Who may be believed is decided elsewhere.** :mod:`omnivia_core_client.update_trust`
owns that question -- the signed envelope v2, its product-specific domain
separation, the trust anchors, the validity and key windows, canonical form and
duplicated member names -- and this module owns the *shape* of a recommendation
and the version comparison. That is why the fetch seam hands over **bytes**
rather than a parsed document: a duplicated member name and a non-canonical
spelling are visible in the bytes and gone from the mapping, so a seam that
parsed first would make two of the refusals below unreachable.

Every refusal from either half is one status here -- ``check_failed`` -- because
a channel that cannot be trusted is not a channel that says this installation is
up to date. The six statuses are unchanged.

The network fetch is an injected callable so tests never touch a live feed.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core_client.errors import ClientError
from omnivia_core_client.update_trust import (
    ENVELOPE_SCHEMA_VERSION,
    PRODUCTION_TRUST_POLICY,
    AdmittedChannel,
    EnvelopeRefused,
    UpdateTrustPolicy,
    admit_channel_document,
)

#: The five first-party distributions the standard candidate installs. All five
#: must be present and comparable for one check result to be authoritative.
FIRST_PARTY_PACKAGES: Final = (
    "omnivia-core",
    "omnivia-core-runtime",
    "omnivia-core-client",
    "omnivia-core-cli",
    "omnivia-core-mcp",
)

#: The first-party channel location (v0.4 §4.2's single first-party HTTPS URL).
DEFAULT_UPDATE_CHANNEL_URL: Final = (
    "https://raw.githubusercontent.com/claytonread/omnivia-core-updates/main/channel.json"
)

#: A channel is a small static document; anything larger is refused before parsing.
MAX_CHANNEL_BYTES: Final = 64 * 1024

#: The candidate bundle's ceiling. The bundle is the whole standard candidate --
#: the five first-party wheels plus their pinned third-party closure, the licence
#: tree, the evidence and the metadata -- so this is tens of megabytes of real
#: content and the cap is generous. The cap exists at all because an unbounded
#: read of an asset this process did not author is the publisher, or anyone who
#: can answer for it, choosing how much memory the updater spends.
MAX_BUNDLE_BYTES: Final = 192 * 1024 * 1024

#: How long each bounded fetch may take. The channel is a few hundred bytes; the
#: bundle is a release asset over a public CDN.
CHANNEL_TIMEOUT_SECONDS: Final = 10.0
BUNDLE_TIMEOUT_SECONDS: Final = 600.0

_VERSION_RE: Final = re.compile(r"^(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*$")

_UPDATE_CHECK_ADAPTER_VERSION: Final = 1


class UpdateCheckError(Exception):
    """A check that cannot produce an honest answer, with its reason code.

    ``refusal`` is the closed, payload-free code from the published update
    contract, when there is one. It travels beside the prose rather than inside
    it so a consumer -- or a conformance test -- branches on the code and never
    on the wording: a refusal recovered by reading a sentence is a refusal whose
    sentence can never be improved.
    """

    def __init__(self, status: str, reason: str, *, refusal: str | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.refusal = refusal


def _version_key(value: str) -> tuple[int, ...]:
    """Core's established dotted-integer ordering (contract-window rule)."""
    if not _VERSION_RE.match(value):
        raise UpdateCheckError(
            "check_failed", f"channel version {value!r} is not a dotted-integer version"
        )
    return tuple(int(part) for part in value.split("."))


def _installed_version_key(value: str) -> tuple[int, ...]:
    """The same ordering, but saying whose version could not be ordered.

    One function per subject rather than one message for both: "channel version
    '0.1.0rc1' is not a dotted-integer version" about an *installed* package
    sends a reader to the channel to look for a version that was never there.
    """
    if not _VERSION_RE.match(value):
        raise UpdateCheckError(
            "unsupported_install",
            f"the installed version {value!r} is outside the dotted-integer "
            "ordering this check compares with",
        )
    return tuple(int(part) for part in value.split("."))


@dataclass(frozen=True, slots=True)
class ChannelRelease:
    """The validated recommendation of one ``omnivia-core-update.v1`` channel."""

    version: str
    release_url: str
    packages: dict[str, str]
    bundle_sha256: str | None = None
    #: Whether the channel document that carried this was signed. ``False`` only
    #: under the development policy, and recorded rather than discarded so the
    #: apply path can state what it is acting on.
    signed: bool = True


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    """The bounded check result the CLI renders (v0.4 §5.2's common meaning)."""

    product_id: str
    status: str
    reason: str | None
    installed: dict[str, str]
    checked_at: str
    candidate_version: str | None = None
    release_url: str | None = None
    bundle_sha256: str | None = None
    channel_url: str = ""
    #: The channel's own per-package version map for the candidate, carried
    #: through verbatim. Deliberately **not** on the wire: ``to_wire`` is the
    #: pinned CLI surface and this is for the coordinator, which must install the
    #: versions the channel named rather than five copies of one release number.
    candidate_packages: dict[str, str] | None = None

    def to_wire(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "update_check_adapter_version": _UPDATE_CHECK_ADAPTER_VERSION,
            "product_id": self.product_id,
            "status": self.status,
            "installed": dict(self.installed),
            "checked_at": self.checked_at,
            "channel_url": self.channel_url,
        }
        if self.reason is not None:
            document["reason"] = self.reason
        if self.candidate_version is not None:
            document["candidate_version"] = self.candidate_version
        if self.release_url is not None:
            document["release_url"] = self.release_url
        if self.bundle_sha256 is not None:
            document["bundle_sha256"] = self.bundle_sha256
        return document


def validate_channel_document(
    raw: bytes | str,
    *,
    policy: UpdateTrustPolicy = PRODUCTION_TRUST_POLICY,
    verified_at: str,
) -> ChannelRelease | None:
    """Admit one channel document's exact bytes, or raise :class:`UpdateCheckError`.

    Two halves, in order. :func:`~omnivia_core_client.update_trust.admit_channel_document`
    decides whether these bytes may be believed at all; this function then decides
    whether what they recommend is a shape this installation can act on.

    ``raw`` is the fetched bytes rather than a parsed document, because the first
    half cannot be done on a mapping -- see this module's docstring.

    The recommendation shape is strict in both directions: a missing member and
    an unknown member are equally refused, because an unknown member is inside
    the signed bytes and a feed that grew new meaning must version itself rather
    than reinterpret old consumers. ``release`` may be ``null``, which is the
    channel explicitly recommending nothing.
    """
    admitted: AdmittedChannel
    try:
        admitted = admit_channel_document(
            raw, policy=policy, verified_at=verified_at
        )
    except EnvelopeRefused as refusal:
        raise UpdateCheckError(
            "check_failed",
            f"the update channel was refused: {refusal.refusal}",
            refusal=refusal.refusal,
        ) from refusal

    release = admitted.release
    if release is None:
        return None
    # ``bundle_sha256`` is required by the signed envelope and optional only in
    # the legacy shape a development policy admits. An archive this installation
    # cannot identify before unpacking it is an archive it must not unpack, so
    # the production path has no branch in which the digest is absent.
    signed = admitted.schema_version == ENVELOPE_SCHEMA_VERSION
    required = {"version", "release_url", "packages"} | (
        {"bundle_sha256"} if signed else set()
    )
    if not required <= set(release):
        raise _release_refusal("channel release member is malformed")
    unknown_members = set(release) - {
        "version",
        "release_url",
        "packages",
        "bundle_sha256",
    }
    if unknown_members:
        raise _release_refusal(
            f"channel release has unexpected members: {sorted(unknown_members)}"
        )
    version = release["version"]
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise _release_refusal(f"channel release version {version!r} is malformed")
    packages = release["packages"]
    if not isinstance(packages, dict) or set(packages) != set(FIRST_PARTY_PACKAGES):
        raise _release_refusal(
            "channel release packages do not cover the five first-party packages"
        )
    for name, package_version in packages.items():
        if not isinstance(package_version, str) or not _VERSION_RE.match(package_version):
            raise _release_refusal(
                f"channel package {name!r} version {package_version!r} is malformed"
            )
    release_url = release["release_url"]
    if not isinstance(release_url, str) or not release_url.startswith("https://"):
        raise _https_refusal()
    bundle_sha256 = release.get("bundle_sha256")
    if bundle_sha256 is not None and (
        not isinstance(bundle_sha256, str) or len(bundle_sha256) != 71
        or not bundle_sha256.startswith("sha256:")
        or any(c not in "0123456789abcdef" for c in bundle_sha256[7:])
    ):
        raise _release_refusal(f"channel bundle_sha256 {bundle_sha256!r} is malformed")
    return ChannelRelease(
        version=version,
        release_url=release_url,
        packages=dict(packages),
        bundle_sha256=bundle_sha256,
        signed=signed,
    )


#: The one refusal code every recommendation-shape rejection carries. It is a
#: separate code from the trust vocabulary because it answers a separate
#: question: these bytes were believed, and what they recommend is not something
#: this installation can act on.
_RELEASE_REFUSAL: Final = "channel_release_malformed"


def _release_refusal(reason: str) -> UpdateCheckError:
    """One shape refusal, carrying the published code beside its prose."""
    return UpdateCheckError("check_failed", reason, refusal=_RELEASE_REFUSAL)


def _https_refusal() -> UpdateCheckError:
    return _release_refusal("channel release_url must be an HTTPS address")


def default_fetch_channel(url: str) -> bytes:
    """The production channel fetch: HTTPS only, bounded size and time.

    Returns the **bytes**, not a parsed document: the admission path judges the
    bytes, and parsing here would discard the duplicated member names and
    non-canonical spellings it exists to refuse.

    Routed through :mod:`omnivia_core_client.http_transport` — the one module
    this distribution may reach the network from — rather than importing
    ``urllib`` here.
    """
    from omnivia_core_client.http_transport import fetch_https_bytes

    if not url.startswith("https://"):
        raise UpdateCheckError("check_failed", "the channel URL must be HTTPS")
    return fetch_https_bytes(
        url, timeout_seconds=CHANNEL_TIMEOUT_SECONDS, max_bytes=MAX_CHANNEL_BYTES
    )


def default_fetch_bundle(url: str) -> bytes:
    """The production bundle fetch, through the same one approved transport.

    It exists because the alternative was each caller writing its own
    ``urlopen`` — and the CLI's did: no HTTPS check, no size cap, and redirects
    followed by the default opener, which is a release asset fetch that an
    attacker who can answer for the host can point anywhere and make any size.
    The digest check afterwards would still have caught a substituted archive,
    but only after it was already in this process's memory.
    """
    from omnivia_core_client.http_transport import fetch_https_bytes

    if not url.startswith("https://"):
        raise UpdateCheckError("check_failed", "the bundle URL must be HTTPS")
    return fetch_https_bytes(
        url, timeout_seconds=BUNDLE_TIMEOUT_SECONDS, max_bytes=MAX_BUNDLE_BYTES
    )


def installed_packages(
    *, probe: Callable[[str], tuple[str, str | None]] | None = None
) -> dict[str, str]:
    """The installed versions of the five first-party distributions.

    ``probe`` returns ``(version, direct_url_json_or_None)`` for one
    distribution name and exists so tests never depend on the running
    interpreter's metadata. A missing distribution is recorded as a missing
    version; the caller decides what a partial install means.
    """
    probe = probe or _probe_distribution
    installed: dict[str, str] = {}
    for name in FIRST_PARTY_PACKAGES:
        try:
            version, direct_url_json = probe(name)
        except importlib.metadata.PackageNotFoundError:
            # A missing first-party distribution is not invented as a version:
            # the caller decides what a partial install means.
            continue
        if version is None:
            continue
        if direct_url_json is not None:
            try:
                direct_url = json.loads(direct_url_json)
            except ValueError:
                direct_url = {}
            if direct_url.get("editable") is True or str(
                direct_url.get("url", "")
            ).startswith("file://"):
                # A source/editable checkout: the developer owns updates
                # through their own environment (v0.4 §11.3 fallback).
                installed[name] = f"{version}+editable"
                continue
        installed[name] = version
    return installed


def _probe_distribution(name: str) -> tuple[str, str | None]:
    distribution = importlib.metadata.distribution(name)
    version = distribution.version
    direct_url_json = distribution.read_text("direct_url.json")
    return version, direct_url_json


def check_for_updates(
    *,
    fetch_channel: Callable[[str], bytes],
    installed: dict[str, str],
    channel_url: str = DEFAULT_UPDATE_CHANNEL_URL,
    checked_at: str,
    policy: UpdateTrustPolicy = PRODUCTION_TRUST_POLICY,
) -> UpdateCheckResult:
    """One full discovery pass, answering exactly one of the six statuses.

    ``policy`` defaults to the production one, which requires the signed
    envelope. ``checked_at`` is also the instant the validity and key windows are
    judged at: one answer about one moment, rather than a result stamped with one
    time and verified against another.
    """
    result_base: dict[str, Any] = {
        "product_id": "omnivia-core",
        "installed": dict(installed),
        "checked_at": checked_at,
        "channel_url": channel_url,
    }

    editable = sorted(
        name for name, version in installed.items() if version.endswith("+editable")
    )
    if editable:
        return UpdateCheckResult(
            status="unsupported_install",
            reason=(
                "a source or editable checkout is owned by its own environment: "
                + ", ".join(editable)
            ),
            **result_base,
        )
    missing = sorted(
        name for name in FIRST_PARTY_PACKAGES if name not in installed
    )
    if missing:
        return UpdateCheckResult(
            status="unsupported_install",
            reason="partial installation: missing " + ", ".join(missing),
            **result_base,
        )

    try:
        document = fetch_channel(channel_url)
        release = validate_channel_document(
            document, policy=policy, verified_at=checked_at
        )
    except UpdateCheckError as error:
        return UpdateCheckResult(
            status=error.status, reason=error.reason, **result_base
        )
    except (ClientError, OSError, ValueError, json.JSONDecodeError) as error:
        # ``ClientError`` is here because the one approved reader raises
        # ``TransportError`` for an oversized or non-HTTPS document, and that is
        # a ``ClientError`` rather than an ``OSError``: without it an oversized
        # channel escaped all six statuses and reached the CLI as a traceback,
        # which is the one thing a check that answers honestly must not do.
        return UpdateCheckResult(
            status="check_failed", reason=str(error), **result_base
        )
    if release is None:
        return UpdateCheckResult(status="no_release", reason=None, **result_base)
    comparisons: list[int] = []
    try:
        for name in FIRST_PARTY_PACKAGES:
            installed_key = _installed_version_key(installed[name])
            recommended_key = _version_key(release.packages[name])
            comparisons.append(
                (installed_key > recommended_key)
                - (installed_key < recommended_key)
            )
    except UpdateCheckError as error:
        # An installed version outside Core's dotted-integer ordering -- a
        # ``0.1.0rc1``, a ``0.1.0.dev1`` -- is a fact about this installation,
        # not about the channel. It used to be compared outside this handler and
        # escaped as an uncaught refusal whose words blamed the channel.
        return UpdateCheckResult(
            status=error.status, reason=error.reason, **result_base
        )
    if all(value == 0 for value in comparisons):
        status = "up_to_date"
    elif any(value < 0 for value in comparisons) and not any(value > 0 for value in comparisons):
        status = "update_available"
    elif all(value >= 0 for value in comparisons) and any(value > 0 for value in comparisons):
        status = "ahead_of_channel"
    else:
        return UpdateCheckResult(
            status="unsupported_install",
            reason="mixed installed versions: some packages are newer and some older than the recommendation",
            **result_base,
        )
    available = status == "update_available"
    return UpdateCheckResult(
        status=status,
        reason=None,
        candidate_version=release.version if available else None,
        release_url=release.release_url if available else None,
        bundle_sha256=release.bundle_sha256 if available else None,
        # The channel's own map, verbatim. The coordinator installs and then
        # verifies these exact versions; synthesising five copies of
        # ``release.version`` would verify the staged wheels against a version
        # the channel never recommended.
        candidate_packages=dict(release.packages) if available else None,
        **result_base,
    )
