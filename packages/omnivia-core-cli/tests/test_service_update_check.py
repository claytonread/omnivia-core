"""The ``service update-check`` machinery (spec v0.4 §5; A01/A02/A21).

No test here touches a live feed: the fetch is an injected callable, the
installed-version provider is an injected callable, and every channel document
is an in-memory fixture. What is pinned is the honesty of the six statuses
(A01 ordering, A02 failures), the strict channel schema, the editable-checkout
eligibility gate, and the "never up to date on failure" rule.
"""

from __future__ import annotations

from typing import Any

import pytest
from omnivia_core_cli.updates import (
    DEFAULT_UPDATE_CHANNEL_URL,
    MAX_CHANNEL_BYTES,
    UpdateCheckError,
    check_for_updates,
    installed_packages,
    validate_channel_document,
)

INSTALLED = {
    "omnivia-core": "0.1.0",
    "omnivia-core-runtime": "0.1.0",
    "omnivia-core-client": "0.1.0",
    "omnivia-core-cli": "0.1.0",
    "omnivia-core-mcp": "0.1.0",
}
NOW = "2026-09-30T00:00:00Z"


def _channel(version: str | None = "0.1.0") -> dict[str, Any]:
    release: dict[str, Any] | None = None
    if version is not None:
        release = {
            "version": version,
            "release_url": f"https://github.com/claytonread/omnivia-core/releases/tag/core-v{version}",
            "packages": {name: version for name in INSTALLED},
        }
    return {"schema_version": "omnivia-core-update.v1", "channel": "stable", "release": release}


def _check(
    channel: Any,
    *,
    installed: dict[str, str] | None = None,
    fetch: Any = None,
) -> Any:
    def fetch_stub(url: str) -> Any:
        if fetch is not None:
            return fetch(url)
        return channel

    return check_for_updates(
        fetch_channel=fetch_stub,
        installed=INSTALLED if installed is None else installed,
        checked_at=NOW,
    )


# --- A01: ordering -------------------------------------------------------------------


def test_equal_versions_answer_up_to_date() -> None:
    assert _check(_channel("0.1.0")).status == "up_to_date"


def test_a_newer_recommendation_answers_update_available_with_the_candidate() -> None:
    result = _check(_channel("0.2.0"))
    assert result.status == "update_available"
    assert result.candidate_version == "0.2.0"
    assert result.release_url.endswith("/core-v0.2.0")


def test_an_older_recommendation_answers_ahead_of_channel() -> None:
    assert _check(_channel("0.0.9")).status == "ahead_of_channel"


def test_an_explicitly_paused_channel_answers_no_release() -> None:
    assert _check(_channel(None)).status == "no_release"


def test_version_ordering_is_dotted_integer_not_string_sorting() -> None:
    # 0.1.10 sorts after 0.1.9 numerically; string sorting would put "0.1.10" first.
    assert _check(_channel("0.1.10")).status == "update_available"


# --- A02: honest failures ------------------------------------------------------------


def test_a_network_failure_answers_check_failed_never_up_to_date() -> None:
    def failing(url: str) -> Any:
        raise UpdateCheckError("check_failed", "the channel was unreachable")

    result = _check(_channel("0.1.0"), fetch=failing)
    assert result.status == "check_failed"
    assert result.reason == "the channel was unreachable"


def test_a_malformed_channel_answers_check_failed() -> None:
    result = _check({"schema_version": "omnivia-core-update.v1", "channel": "stable"})
    assert result.status == "check_failed"


def test_an_unknown_schema_version_answers_check_failed() -> None:
    channel = _channel("0.1.0")
    channel["schema_version"] = "omnivia-core-update.v9"
    result = _check(channel)
    assert result.status == "check_failed"


def test_an_unknown_channel_member_answers_check_failed() -> None:
    channel = _channel("0.1.0")
    channel["auto_install"] = True
    result = _check(channel)
    assert result.status == "check_failed"


def test_a_non_https_release_url_answers_check_failed() -> None:
    channel = _channel("0.1.0")
    release = channel["release"]
    release["release_url"] = "http://github.com/claytonread/omnivia-core/releases/tag/x"
    result = _check(channel)
    assert result.status == "check_failed"


def test_a_partial_package_map_answers_check_failed() -> None:
    channel = _channel("0.1.0")
    channel["release"]["packages"].pop("omnivia-core-mcp")
    result = _check(channel)
    assert result.status == "check_failed"


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
        "omnivia_core_cli.updates._probe_distribution",
        probe,
    )
    installed = installed_packages()
    assert installed == {"omnivia-core": "0.1.0"}


def test_the_default_channel_url_is_the_first_party_https_location() -> None:
    assert DEFAULT_UPDATE_CHANNEL_URL.startswith("https://raw.githubusercontent.com/")
    assert DEFAULT_UPDATE_CHANNEL_URL.endswith("channel.json")


def test_the_channel_size_limit_is_bounded() -> None:
    assert MAX_CHANNEL_BYTES == 64 * 1024


def test_the_validator_accepts_null_release_and_the_full_shape() -> None:
    paused = validate_channel_document(_channel(None))
    assert paused is None
    release = validate_channel_document(_channel("0.2.0"))
    assert release is not None
    assert release.version == "0.2.0"
    assert set(release.packages) == set(INSTALLED)
