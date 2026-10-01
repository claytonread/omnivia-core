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

The network fetch is an injected callable so tests never touch a live feed.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

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

_SCHEMA_VERSION: Final = "omnivia-core-update.v1"
_VERSION_RE: Final = re.compile(r"^(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*$")

_UPDATE_CHECK_ADAPTER_VERSION: Final = 1


class UpdateCheckError(Exception):
    """A check that cannot produce an honest answer, with its reason code."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _version_key(value: str) -> tuple[int, ...]:
    """Core's established dotted-integer ordering (contract-window rule)."""
    if not _VERSION_RE.match(value):
        raise UpdateCheckError(
            "check_failed", f"channel version {value!r} is not a dotted-integer version"
        )
    return tuple(int(part) for part in value.split("."))


@dataclass(frozen=True, slots=True)
class ChannelRelease:
    """The validated recommendation of one ``omnivia-core-update.v1`` channel."""

    version: str
    release_url: str
    packages: dict[str, str]
    bundle_sha256: str | None = None


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


def validate_channel_document(document: Any) -> ChannelRelease | None:
    """Validate one channel document, or raise :class:`UpdateCheckError`.

    The v0.4 §4.2 shape is exactly three members; ``release`` may be ``null``
    (the channel explicitly recommends nothing). Unknown members are refused:
    a feed that grew new meaning must version itself, not reinterpret old
    consumers.
    """
    if not isinstance(document, dict):
        raise UpdateCheckError("check_failed", "channel document is not a JSON object")
    if set(document) != {"schema_version", "channel", "release"}:
        raise UpdateCheckError(
            "check_failed",
            "channel document has unexpected or missing members: "
            f"{sorted(document)}",
        )
    if document["schema_version"] != _SCHEMA_VERSION:
        raise UpdateCheckError(
            "check_failed",
            f"channel schema_version {document['schema_version']!r} is not supported",
        )
    if document["channel"] != "stable":
        raise UpdateCheckError(
            "check_failed", f"channel {document['channel']!r} is not the stable channel"
        )
    release = document["release"]
    if release is None:
        return None
    if not isinstance(release, dict) or not {
        "version",
        "release_url",
        "packages",
    } <= set(release):
        raise UpdateCheckError("check_failed", "channel release member is malformed")
    unknown_members = set(release) - {
        "version",
        "release_url",
        "packages",
        # Deliberately added for the update path (v0.4 §5.2: add fields
        # deliberately): the built candidate bundle's checksum, so the
        # coordinator can verify the downloaded archive before staging it.
        "bundle_sha256",
    }
    if unknown_members:
        raise UpdateCheckError(
            "check_failed",
            f"channel release has unexpected members: {sorted(unknown_members)}",
        )
    version = release["version"]
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise UpdateCheckError(
            "check_failed", f"channel release version {version!r} is malformed"
        )
    packages = release["packages"]
    if not isinstance(packages, dict) or set(packages) != set(FIRST_PARTY_PACKAGES):
        raise UpdateCheckError(
            "check_failed", "channel release packages do not cover the five first-party packages"
        )
    for name, package_version in packages.items():
        if not isinstance(package_version, str) or not _VERSION_RE.match(package_version):
            raise UpdateCheckError(
                "check_failed",
                f"channel package {name!r} version {package_version!r} is malformed",
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
        raise UpdateCheckError(
            "check_failed", f"channel bundle_sha256 {bundle_sha256!r} is malformed"
        )
    return ChannelRelease(
        version=version,
        release_url=release_url,
        packages=dict(packages),
        bundle_sha256=bundle_sha256,
    )


def _https_refusal() -> UpdateCheckError:
    return UpdateCheckError(
        "check_failed", "channel release_url must be an HTTPS address"
    )


def default_fetch_channel(url: str) -> Any:
    """The production fetch: HTTPS only, bounded size and time.

    Routed through :mod:`omnivia_core_client.http_transport` — the one module
    this distribution may reach the network from — rather than importing
    ``urllib`` here.
    """
    from omnivia_core_client.http_transport import fetch_https_bytes

    if not url.startswith("https://"):
        raise UpdateCheckError("check_failed", "the channel URL must be HTTPS")
    return json.loads(
        fetch_https_bytes(url, timeout_seconds=10.0, max_bytes=MAX_CHANNEL_BYTES)
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
    fetch_channel: Callable[[str], Any],
    installed: dict[str, str],
    channel_url: str = DEFAULT_UPDATE_CHANNEL_URL,
    checked_at: str,
) -> UpdateCheckResult:
    """One full discovery pass, answering exactly one of the six statuses."""
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
        release = validate_channel_document(document)
    except UpdateCheckError as error:
        return UpdateCheckResult(
            status=error.status, reason=error.reason, **result_base
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return UpdateCheckResult(
            status="check_failed", reason=str(error), **result_base
        )
    if release is None:
        return UpdateCheckResult(status="no_release", reason=None, **result_base)
    comparisons: list[int] = []
    for name in FIRST_PARTY_PACKAGES:
        installed_key = _version_key(installed[name])
        recommended_key = _version_key(release.packages[name])
        comparisons.append(
            (installed_key > recommended_key)
            - (installed_key < recommended_key)
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
    return UpdateCheckResult(
        status=status,
        reason=None,
        candidate_version=release.version if status == "update_available" else None,
        release_url=release.release_url if status == "update_available" else None,
        bundle_sha256=release.bundle_sha256 if status == "update_available" else None,
        **result_base,
    )
