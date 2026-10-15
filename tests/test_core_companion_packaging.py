from __future__ import annotations

import importlib.util
import inspect
import json
import plistlib
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-core-companion-app.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_core_companion_app", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: `@dataclass` under `from __future__ import
    # annotations` resolves string annotations through `sys.modules`, so an
    # unregistered module cannot define one.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _executable(tmp_path: Path) -> Path:
    executable = tmp_path / "omnivia-core-status-menu"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return executable


def test_companion_bundle_has_stable_identity_and_unsigned_qualification_manifest(
    tmp_path: Path,
) -> None:
    module = _module()
    output = tmp_path / "candidate"
    app = module.build_companion_app(
        executable=_executable(tmp_path), output=output, version="0.6.5"
    )
    plist = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert plist == {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": "omnivia-core-status-menu",
        "CFBundleIdentifier": "com.omnivia.core.status",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "OmniVia Core",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.6.5",
        "CFBundleVersion": "0.6.5",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "Documents and folders",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["public.data", "public.folder"],
            }
        ],
        "NSServices": [
            {
                "NSMenuItem": {"default": "Add to OmniVia Core…"},
                "NSMessage": "addToOmniViaCore",
                "NSPortName": "OmniVia Core",
                "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
                "NSSendFileTypes": ["public.data", "public.directory"],
            }
        ],
    }
    executable = app / "Contents" / "MacOS" / "omnivia-core-status-menu"
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755
    manifest = json.loads((output / "companion-manifest.json").read_text(encoding="ascii"))
    assert manifest["bundle_id"] == "com.omnivia.core.status"
    assert manifest["signature_status"] == "unsigned"
    assert manifest["production_release_eligible"] is False
    assert len(manifest["executable_sha256"]) == 64


@pytest.mark.parametrize("version", ["0.6", "v0.6.5", "01.2.3", "0.6.5-rc1"])
def test_companion_packaging_refuses_non_release_versions(
    tmp_path: Path, version: str
) -> None:
    module = _module()
    with pytest.raises(module.PackagingError, match="version refused"):
        module.build_companion_app(
            executable=_executable(tmp_path),
            output=tmp_path / "candidate",
            version=version,
        )


def test_companion_packaging_refuses_overwrite_and_symlinked_input(tmp_path: Path) -> None:
    module = _module()
    output = tmp_path / "candidate"
    output.mkdir()
    with pytest.raises(module.PackagingError, match="output refused"):
        module.build_companion_app(
            executable=_executable(tmp_path), output=output, version="0.6.5"
        )

    real = _executable(tmp_path)
    linked = tmp_path / "linked-companion"
    linked.symlink_to(real)
    with pytest.raises(module.PackagingError, match="executable refused"):
        module.build_companion_app(
            executable=linked, output=tmp_path / "other", version="0.6.5"
        )


def test_companion_embeds_real_finder_and_share_extension_bundles(tmp_path: Path) -> None:
    module = _module()
    finder = tmp_path / "OmniViaFinderAction"
    share = tmp_path / "OmniViaShare"
    finder.write_bytes(b"finder-mach-o")
    share.write_bytes(b"share-mach-o")
    finder.chmod(0o755)
    share.chmod(0o755)

    output = tmp_path / "candidate"
    app = module.build_companion_app(
        executable=_executable(tmp_path),
        output=output,
        version="0.6.5",
        finder_extension_executable=finder,
        share_extension_executable=share,
    )

    finder_bundle = app / "Contents" / "PlugIns" / "Add to OmniVia Core.appex"
    finder_plist = plistlib.loads((finder_bundle / "Contents" / "Info.plist").read_bytes())
    assert finder_plist["CFBundleIdentifier"] == "com.omnivia.core.finder-action"
    assert finder_plist["NSExtension"]["NSExtensionPointIdentifier"] == "com.apple.ui-services"
    attributes = finder_plist["NSExtension"]["NSExtensionAttributes"]
    assert attributes["NSExtensionServiceAllowsFinderPreviewItem"] is True
    assert attributes["NSExtensionServiceRoleType"] == "NSExtensionServiceRoleTypeViewer"
    rule = attributes["NSExtensionActivationRule"]
    assert isinstance(rule, str)
    assert "TRUEPREDICATE" not in rule.upper()
    assert 'UTI-CONFORMS-TO "public.data"' in rule
    assert 'UTI-CONFORMS-TO "public.directory"' in rule
    assert "$extensionItem.attachments.@count >= 1" in rule
    assert "$extensionItem.attachments.@count <= 10" in rule
    assert ".@count == 1" in rule
    assert module.FINDER_EXTENSION_MAX_ITEMS == 10
    assert stat.S_IMODE(
        (finder_bundle / "Contents" / "MacOS" / "OmniViaFinderAction").stat().st_mode
    ) == 0o755

    share_bundle = app / "Contents" / "PlugIns" / "Share to OmniVia Core.appex"
    share_plist = plistlib.loads((share_bundle / "Contents" / "Info.plist").read_bytes())
    assert share_plist["CFBundleIdentifier"] == "com.omnivia.core.share"
    assert share_plist["NSExtension"]["NSExtensionPointIdentifier"] == "com.apple.share-services"
    assert share_plist["NSExtension"]["NSExtensionAttributes"]["NSExtensionActivationRule"] == {
        "NSExtensionActivationSupportsFileWithMaxCount": 10,
        "NSExtensionActivationSupportsImageWithMaxCount": 10,
        "NSExtensionActivationSupportsText": True,
        "NSExtensionActivationSupportsWebURLWithMaxCount": 10,
    }
    assert module.SHARE_EXTENSION_MAX_ITEMS == module.FINDER_EXTENSION_MAX_ITEMS == 10
    # No attachment or movie activation: this extension captures files, images,
    # plain text and web links, and declares exactly those.
    assert set(module.SHARE_EXTENSION_ACTIVATION_RULE) == {
        "NSExtensionActivationSupportsFileWithMaxCount",
        "NSExtensionActivationSupportsImageWithMaxCount",
        "NSExtensionActivationSupportsText",
        "NSExtensionActivationSupportsWebURLWithMaxCount",
    }

    manifest = json.loads((output / "companion-manifest.json").read_text(encoding="ascii"))
    assert [item["bundle_id"] for item in manifest["components"]] == [
        "com.omnivia.core.status",
        "com.omnivia.core.finder-action",
        "com.omnivia.core.share",
    ]


def test_companion_refuses_partial_extension_pair(tmp_path: Path) -> None:
    module = _module()
    finder = tmp_path / "OmniViaFinderAction"
    finder.write_bytes(b"finder-mach-o")
    finder.chmod(0o755)
    with pytest.raises(module.PackagingError, match="extension pair refused"):
        module.build_companion_app(
            executable=_executable(tmp_path),
            output=tmp_path / "candidate",
            version="0.6.5",
            finder_extension_executable=finder,
        )


def test_share_extension_entitlements_cannot_reach_the_network() -> None:
    """A shared link is captured as the text of the link.

    Nothing resolves or fetches it, and the entitlements are what makes that
    structural rather than a promise: the Share extension is sandboxed with no
    network client or server entitlement at all.
    """
    entitlements = plistlib.loads(
        (
            ROOT
            / "apps"
            / "core-status-menu-macos"
            / "Packaging"
            / "Share.entitlements"
        ).read_bytes()
    )
    assert entitlements["com.apple.security.app-sandbox"] is True
    assert not any(key.startswith("com.apple.security.network") for key in entitlements)
    # And it reaches the companion only through the App Group it shares.
    assert entitlements["com.apple.security.application-groups"] == [
        "group.com.omnivia.core"
    ]


def _candidate(tmp_path: Path, module: ModuleType, version: str = "0.6.5") -> Path:
    """Assemble a complete three-component candidate."""
    finder = tmp_path / f"OmniViaFinderAction-{version}"
    share = tmp_path / f"OmniViaShare-{version}"
    # Version-stamped bytes, because a real release builds different bytes: a
    # fixture that reused them would hide the digest half of a skew.
    finder.write_bytes(f"finder-mach-o-{version}".encode("ascii"))
    share.write_bytes(f"share-mach-o-{version}".encode("ascii"))
    finder.chmod(0o755)
    share.chmod(0o755)
    output = tmp_path / f"candidate-{version}"
    module.build_companion_app(
        executable=_executable(tmp_path),
        output=output,
        version=version,
        finder_extension_executable=finder,
        share_extension_executable=share,
    )
    return output


def _manifest(candidate: Path) -> dict[str, object]:
    return json.loads((candidate / "companion-manifest.json").read_text(encoding="ascii"))


def _rewrite(candidate: Path, manifest: dict[str, object]) -> None:
    (candidate / "companion-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )


def _component(manifest: dict[str, object], role: str) -> dict[str, object]:
    components = manifest["components"]
    assert isinstance(components, list)
    for entry in components:
        if entry["role"] == role:
            return entry
    raise AssertionError(f"no {role} component")


TEAM_ID = "TEAMID1234"
REQUIREMENT = f'anchor apple generic and certificate leaf[subject.OU] = "{TEAM_ID}"'


def _authority(module: ModuleType, **overrides: str) -> object:
    return module.SigningAuthority(
        **{"team_identifier": TEAM_ID, "requirement": REQUIREMENT, **overrides}
    )


def _bind_repository_authority(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    run: Callable[[Sequence[str]], tuple[int, str]] | None = None,
    *,
    owner: bool = True,
    authority: bool = True,
) -> None:
    """Stand in for the reviewed repository change that binds a release owner.

    Both halves are module constants, and that is the whole point: a test can
    only reach them the way a reviewer does -- by changing this repository --
    never through an argument, a keyword or a flag. `run` stands in for the
    platform tools, which cannot be made to report a real Developer ID
    signature in a test, and it is likewise reachable only from here.
    """

    monkeypatch.setattr(module, "APP_UPDATE_OWNER_BOUND", owner)
    if authority:
        monkeypatch.setattr(module, "REPOSITORY_SIGNING_AUTHORITY", _authority(module))
    if run is not None:
        monkeypatch.setattr(module, "_run_signing_command", run)


def _runner(
    *,
    refuses: str | None = None,
    unavailable: str | None = None,
    team: str = TEAM_ID,
    cdhash: bool = True,
) -> tuple[Callable[[Sequence[str]], tuple[int, str]], list[list[str]]]:
    """A stand-in for the platform signing authorities, with its call log.

    `refuses` and `unavailable` name a substring of the argv whose command exits
    non-zero or cannot be run at all. Everything else succeeds, and
    `codesign --display` answers in the form the real tool writes.
    """

    calls: list[list[str]] = []

    def run(command: Sequence[str]) -> tuple[int, str]:
        calls.append(list(command))
        joined = " ".join(command)
        if unavailable is not None and unavailable in joined:
            raise FileNotFoundError(command[0])
        if refuses is not None and refuses in joined:
            return 1, "test refusal"
        if "--display" in command:
            lines = [f"TeamIdentifier={team}", "Authority=Developer ID Application: Example"]
            if cdhash:
                lines.append("CDHash=" + "a" * 40)
            return 0, "\n".join(lines) + "\n"
        return 0, ""

    return run, calls


def test_manifest_binds_the_companion_and_both_extensions_as_one_component_set(
    tmp_path: Path,
) -> None:
    module = _module()
    manifest = _manifest(_candidate(tmp_path, module))

    assert manifest["schema_version"] == module.MANIFEST_SCHEMA_VERSION == 2
    assert manifest["compatibility_id"] == module.COMPONENT_SET_COMPATIBILITY_ID
    assert manifest["version"] == manifest["build_version"] == "0.6.5"
    assert manifest["minimum_macos"] == "13.0"
    # No independent updater, and no invented owner: the app's update owner is
    # unbound, the existing channel does not cover this set, and the installer
    # is owned outside this repository (WP-00 binding ledger row 13).
    assert manifest["update_owner"] == {
        "app_update_owner": "externally_unbound",
        "authority": (
            "docs/development/"
            "omnivia-core-macos-document-intake-wp-00-binding-ledger-2026-10-13.md#13"
        ),
        "existing_channel_covers_component_set": False,
        "independent_updater": False,
        "installer_owner_in_this_repository": False,
    }
    assert module.APP_UPDATE_OWNER_BOUND is False
    # Every field the manifest carries is a field this schema binds, and every
    # field it binds is present: nothing in a passing manifest is ignored.
    assert set(manifest) == set(module.MANIFEST_FIELDS)
    for entry in manifest["components"]:
        assert set(entry) == set(module.COMPONENT_FIELDS)
    # The top-level identity and digest fields are the companion's own.
    assert manifest["bundle_name"] == "OmniVia Core"
    assert manifest["bundle_path"] == "OmniVia Core.app"
    assert manifest["executable"] == "omnivia-core-status-menu"
    assert manifest["executable_sha256"] == _component(manifest, "companion")["executable_sha256"]
    # The build never certifies itself.
    assert manifest["production_release_eligible"] is False
    assert manifest["signature_status"] == "unsigned"
    assert manifest["notarization_status"] == "not_notarized"

    components = manifest["components"]
    assert isinstance(components, list)
    assert len(components) == 3
    for entry in components:
        assert entry["version"] == entry["build_version"] == "0.6.5"
        assert entry["minimum_macos"] == "13.0"
        assert len(entry["executable_sha256"]) == 64
        assert entry["executable_path"].startswith("OmniVia Core.app/Contents/")
    assert _component(manifest, "companion")["registration"] == (
        "service:OmniVia Core/addToOmniViaCore"
    )
    assert _component(manifest, "finder_action_extension")["registration"] == (
        "extension-point:com.apple.ui-services"
    )
    assert _component(manifest, "share_extension")["registration"] == (
        "extension-point:com.apple.share-services"
    )
    # Every declared registration is distinct: one set, no duplicate service.
    assert len({entry["registration"] for entry in components}) == 3


def test_verifier_accepts_a_coherent_unsigned_set_without_calling_it_releasable(
    tmp_path: Path,
) -> None:
    module = _module()
    report = module.verify_candidate(_candidate(tmp_path, module))
    assert report.findings == ()
    assert report.ok is True
    # Coherence is the local verdict and it is clean. Release eligibility is a
    # strictly narrower question and the answer is no.
    assert report.coherence_ok is True
    # No repository authority binds an expected identity, so nothing was run.
    assert report.signature_status == "unavailable"
    assert report.release_eligible is False


def test_release_qualification_names_every_blocker_on_an_unsigned_candidate(
    tmp_path: Path,
) -> None:
    module = _module()
    report = module.verify_candidate(
        _candidate(tmp_path, module), require_release_eligible=True
    )
    assert report.coherence_ok is True
    assert report.findings == (
        (
            "not release eligible: signing verification is unavailable -- no "
            "repository authority binds an expected signing identity"
        ),
        "not release eligible: the app update owner is not bound by repository authority",
    )
    assert report.release_eligible is False


def test_signing_verification_runs_the_platform_authorities_over_the_candidate(
    tmp_path: Path,
) -> None:
    """The verdict is the exit status of real commands over the real bundles."""
    module = _module()
    candidate = _candidate(tmp_path, module)
    run, calls = _runner()

    status, signed, findings = module._verify_signing(candidate, _authority(module), run)
    assert (status, signed, findings) == ("verified", True, [])

    programs = [call[0] for call in calls]
    assert programs.count("codesign") == 9  # three commands for each of three components
    assert programs[-2:] == ["spctl", "xcrun"]
    # Every command named a path inside this candidate, never a declared string.
    for call in calls:
        assert any(str(candidate) in argument for argument in call)
    app = str(candidate / "OmniVia Core.app")
    assert ["codesign", "--verify", "--deep", "--strict", app] in calls
    assert ["codesign", "--verify", f"-R={REQUIREMENT}", app] in calls
    assert ["codesign", "--display", "--verbose=4", app] in calls
    assert ["spctl", "--assess", "--type", "exec", "--verbose=4", app] in calls
    assert ["xcrun", "stapler", "validate", app] in calls
    appex = str(candidate / "OmniVia Core.app/Contents/PlugIns/Share to OmniVia Core.appex")
    assert ["codesign", "--verify", "--deep", "--strict", appex] in calls


@pytest.mark.parametrize(
    ("kwargs", "finding"),
    [
        ({"refuses": "--deep"}, "signing authority refused the candidate: seal companion"),
        (
            {"refuses": "-R="},
            "signing authority refused the candidate: designated requirement companion",
        ),
        (
            {"refuses": "spctl"},
            "signing authority refused the candidate: Gatekeeper assessment",
        ),
        (
            {"refuses": "stapler"},
            "signing authority refused the candidate: notarisation staple",
        ),
        ({"unavailable": "codesign"}, "signing authority unavailable: seal companion"),
        ({"unavailable": "spctl"}, "signing authority unavailable: Gatekeeper assessment"),
        ({"team": "OTHERTEAM1"}, "signing team identifier mismatch: companion"),
        ({"cdhash": False}, "signing reports no code directory hash: companion"),
    ],
)
def test_a_failing_platform_authority_is_an_explicit_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
    finding: str,
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    run, _ = _runner(**kwargs)  # type: ignore[arg-type]
    _bind_repository_authority(module, monkeypatch, run)

    report = module.verify_candidate(candidate)
    assert finding in report.findings
    assert report.signature_status == "unverified"
    assert report.release_eligible is False
    # The candidate itself is still coherent: a signing failure is not that.
    assert report.coherence_ok is True


@pytest.mark.parametrize(
    ("owner", "authority"),
    [(False, False), (True, False), (False, True)],
)
def test_only_the_repository_bound_authority_can_reach_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: bool, authority: bool
) -> None:
    """Partly bound is unavailable, with the platform tools reporting success.

    The runner below says every authority passed. It changes nothing: without
    both the expected identity and the update owner bound by repository
    authority there is nothing to check a candidate against, so the commands
    are never run and no signature is reported. `(False, False)` is this
    repository today (WP-00 binding ledger row 13).
    """
    module = _module()
    candidate = _candidate(tmp_path, module)
    run, calls = _runner()
    _bind_repository_authority(
        module, monkeypatch, run, owner=owner, authority=authority
    )

    report = module.verify_candidate(candidate, require_release_eligible=True)
    assert calls == []
    assert report.coherence_ok is True
    assert report.signature_status == "unavailable"
    assert report.release_eligible is False
    assert (
        "not release eligible: signing verification is unavailable -- no "
        "repository authority binds an expected signing identity"
    ) in report.findings


def test_eligibility_needs_coherence_signing_and_a_bound_update_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With both bindings made by authority, coherence and signing still gate."""
    module = _module()
    candidate = _candidate(tmp_path, module)
    run, _ = _runner()
    _bind_repository_authority(module, monkeypatch, run)

    report = module.verify_candidate(candidate, require_release_eligible=True)
    assert report.findings == ()
    assert report.signature_status == "verified"
    assert report.release_eligible is True

    # Signing alone removed.
    refusing, _ = _runner(refuses="stapler")
    monkeypatch.setattr(module, "_run_signing_command", refusing)
    report = module.verify_candidate(candidate)
    assert report.signature_status == "unverified"
    assert report.release_eligible is False

    # Coherence alone removed.
    monkeypatch.setattr(module, "_run_signing_command", run)
    (candidate / "OmniVia Core.app/Contents/MacOS/omnivia-core-status-menu").write_bytes(
        b"tampered"
    )
    report = module.verify_candidate(candidate, require_release_eligible=True)
    assert report.coherence_ok is False
    assert report.signature_status == "verified"
    assert report.release_eligible is False
    assert report.findings == (
        "component executable digest mismatch: companion",
        "not release eligible: the candidate is not coherent",
    )


def test_the_expected_signing_identity_is_not_caller_selectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller cannot nominate the identity its own candidate is checked against.

    There is no keyword for an expected identity and none for the commands that
    check it, so the two injection paths a production caller could reach are
    gone. Constructing a `SigningAuthority` is still possible -- it is how the
    repository declares its own -- and a caller's instance verifies nothing.
    """
    module = _module()
    candidate = _candidate(tmp_path, module)
    run, calls = _runner()

    assert set(inspect.signature(module.verify_candidate).parameters) == {
        "candidate",
        "require_release_eligible",
    }
    for rejected in (
        {"signing_authority": _authority(module)},
        {"expected_team_id": TEAM_ID, "expected_requirement": REQUIREMENT},
        {"run": run},
        {"authority": _authority(module), "run": run},
    ):
        with pytest.raises(TypeError):
            module.verify_candidate(candidate, **rejected)
    assert calls == []

    # The repository's own constant is what is read, and it is unbound; a
    # caller's instance of the same type is inert even with the update owner
    # bound and the platform tools reporting success.
    assert module.REPOSITORY_SIGNING_AUTHORITY is None
    assert module._repository_signing_authority() is None
    _bind_repository_authority(module, monkeypatch, run, authority=False)
    assert module._repository_signing_authority() is None
    report = module.verify_candidate(candidate, require_release_eligible=True)
    assert calls == []
    assert report.signature_status == "unavailable"
    assert report.release_eligible is False


def test_declarative_signing_evidence_has_no_path_into_the_verdict(
    tmp_path: Path,
) -> None:
    """A forged JSON record is not checked signing evidence.

    There is no parameter that accepts one, and planting one in the candidate
    only trips the symlink-and-manifest walk's own checks -- it never reports a
    signature.
    """
    module = _module()
    candidate = _candidate(tmp_path, module)
    forged = {
        policy.bundle_id: {
            "authority": "Developer ID Application: Example (TEAMID1234)",
            "cdhash": "a" * 40,
            "notarized": True,
            "team_identifier": TEAM_ID,
        }
        for policy in module.COMPONENT_SET
    }
    with pytest.raises(TypeError):
        module.verify_candidate(candidate, signature_evidence=forged)

    (candidate / "signature-evidence.json").write_text(json.dumps(forged), encoding="ascii")
    report = module.verify_candidate(candidate, require_release_eligible=True)
    assert report.signature_status == "unavailable"
    assert report.release_eligible is False
    assert report.findings == (
        (
            "not release eligible: signing verification is unavailable -- no "
            "repository authority binds an expected signing identity"
        ),
        "not release eligible: the app update owner is not bound by repository authority",
    )


def test_no_signing_authority_runs_no_command_and_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    run, calls = _runner()
    monkeypatch.setattr(module, "_run_signing_command", run)
    report = module.verify_candidate(_candidate(tmp_path, module))
    assert calls == []
    assert report.signature_status == "unavailable"
    assert report.release_eligible is False


def test_the_production_runner_fails_closed_off_macos_and_on_a_missing_tool() -> None:
    """The default runner is the only thing production signing can go through."""
    module = _module()
    if sys.platform == "darwin":
        with pytest.raises(FileNotFoundError):
            module._run_signing_command(["omnivia-no-such-signing-tool"])
        status, _ = module._run_signing_command(["false"])
        assert status != 0
    else:
        with pytest.raises(OSError, match="only on macOS"):
            module._run_signing_command(["codesign", "--version"])


@pytest.mark.parametrize(
    ("team", "requirement"),
    [("", REQUIREMENT), ("teamid1234", REQUIREMENT), ("TEAMID12345", REQUIREMENT), (TEAM_ID, "  ")],
)
def test_an_expected_identity_must_be_a_real_team_and_requirement(
    team: str, requirement: str
) -> None:
    module = _module()
    with pytest.raises(module.PackagingError, match="refused"):
        module.SigningAuthority(team_identifier=team, requirement=requirement)


@pytest.mark.parametrize(
    ("role", "skewed"),
    [
        ("companion", "0.6.6"),
        ("companion", "0.6.4"),
        ("finder_action_extension", "0.6.6"),
        ("share_extension", "0.6.4"),
    ],
)
def test_verifier_refuses_component_version_skew(
    tmp_path: Path, role: str, skewed: str
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    manifest = _manifest(candidate)
    _component(manifest, role)["version"] = skewed
    _rewrite(candidate, manifest)

    report = module.verify_candidate(candidate)
    # Refused as a set, and the member whose plist no longer agrees is named.
    assert "component version skew across the set" in report.findings
    assert f"component version differs from the manifest: {role}" in report.findings
    assert f"component CFBundleShortVersionString mismatch: {role}" in report.findings
    assert report.release_eligible is False


def test_verifier_refuses_build_version_and_minimum_os_skew(tmp_path: Path) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    manifest = _manifest(candidate)
    _component(manifest, "share_extension")["build_version"] = "0.6.5.1"
    _component(manifest, "finder_action_extension")["minimum_macos"] = "14.0"
    _rewrite(candidate, manifest)

    report = module.verify_candidate(candidate)
    assert "component version skew across the set" in report.findings
    assert (
        "component build_version differs from the manifest: share_extension"
        in report.findings
    )
    assert (
        "component minimum_macos differs from the manifest: finder_action_extension"
        in report.findings
    )


def test_verifier_refuses_a_missing_managed_extension(tmp_path: Path) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    shutil.rmtree(candidate / "OmniVia Core.app" / "Contents" / "PlugIns" / "Share to OmniVia Core.appex")
    manifest = _manifest(candidate)
    manifest["components"] = [
        entry for entry in manifest["components"] if entry["role"] != "share_extension"
    ]
    _rewrite(candidate, manifest)

    report = module.verify_candidate(candidate)
    assert report.findings == ("managed component missing: share_extension",)


def test_verifier_refuses_a_declared_component_whose_bundle_is_absent(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    shutil.rmtree(candidate / "OmniVia Core.app" / "Contents" / "PlugIns" / "Share to OmniVia Core.appex")

    report = module.verify_candidate(candidate)
    assert report.findings == (
        "component bundle missing: share_extension",
        "component executable missing: share_extension",
    )


def test_verifier_refuses_an_extra_bundle_and_a_duplicated_extension(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    plugins = candidate / "OmniVia Core.app" / "Contents" / "PlugIns"
    shutil.copytree(plugins / "Share to OmniVia Core.appex", plugins / "Copy.appex")

    report = module.verify_candidate(candidate)
    # One copy: two bundles with the same identifier and the same extension
    # point, at a path this set does not manage.
    assert "duplicate bundle identifier: com.omnivia.core.share" in report.findings
    assert (
        "duplicate service registration: extension-point:com.apple.share-services"
        in report.findings
    )
    assert (
        "managed bundle identifier at unmanaged path: "
        "OmniVia Core.app/Contents/PlugIns/Copy.appex" in report.findings
    )


def test_verifier_refuses_an_unmanaged_extension_and_registration(tmp_path: Path) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    stranger = (
        candidate / "OmniVia Core.app" / "Contents" / "PlugIns" / "Stranger.appex" / "Contents"
    )
    stranger.mkdir(parents=True)
    (stranger / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.example.stranger",
                "NSExtension": {"NSExtensionPointIdentifier": "com.apple.quicklook.preview"},
            }
        )
    )

    report = module.verify_candidate(candidate)
    assert (
        "unmanaged bundle in candidate: "
        "OmniVia Core.app/Contents/PlugIns/Stranger.appex" in report.findings
    )
    assert (
        "unmanaged service registration: extension-point:com.apple.quicklook.preview"
        in report.findings
    )


def test_verifier_refuses_a_duplicate_service_registration(tmp_path: Path) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    info = candidate / "OmniVia Core.app" / "Contents" / "Info.plist"
    plist = plistlib.loads(info.read_bytes())
    plist["NSServices"] = plist["NSServices"] * 2
    info.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))

    report = module.verify_candidate(candidate)
    assert (
        "duplicate service registration: service:OmniVia Core/addToOmniViaCore"
        in report.findings
    )
    assert "component registration set mismatch: companion" in report.findings


def test_verifier_refuses_a_second_distinct_companion_service(tmp_path: Path) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    info = candidate / "OmniVia Core.app" / "Contents" / "Info.plist"
    plist = plistlib.loads(info.read_bytes())
    plist["NSServices"] = [
        *plist["NSServices"],
        {
            "NSMenuItem": {"default": "Add to OmniVia Core (again)…"},
            "NSMessage": "addToOmniViaCoreAgain",
            "NSPortName": "OmniVia Core",
            "NSSendFileTypes": ["public.data"],
        },
    ]
    info.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))

    report = module.verify_candidate(candidate)
    assert (
        "unmanaged service registration: service:OmniVia Core/addToOmniViaCoreAgain"
        in report.findings
    )
    assert "component registration set mismatch: companion" in report.findings


@pytest.mark.parametrize(
    "role",
    ["companion", "finder_action_extension", "share_extension"],
)
def test_verifier_refuses_a_tampered_executable(tmp_path: Path, role: str) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    policy = next(item for item in module.COMPONENT_SET if item.role == role)
    target = candidate / policy.executable_path
    target.write_bytes(target.read_bytes() + b"\x00tampered")

    report = module.verify_candidate(candidate)
    assert report.findings == (f"component executable digest mismatch: {role}",)


def test_verifier_refuses_an_unknown_or_newer_manifest_schema_without_rewriting_it(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    path = candidate / "companion-manifest.json"
    manifest = _manifest(candidate)
    manifest["schema_version"] = 3
    manifest["unknown_future_field"] = {"kept": True}
    _rewrite(candidate, manifest)
    before = path.read_bytes()

    report = module.verify_candidate(candidate)
    assert report.findings == ("manifest schema unsupported: 3",)
    assert report.release_eligible is False
    # A downgraded reader preserves the newer envelope exactly: nothing is
    # rewritten, downgraded or field-stripped (section 18.3, END-05).
    assert path.read_bytes() == before


def test_verifier_refuses_a_candidate_whose_manifest_certifies_itself(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    manifest = _manifest(candidate)
    manifest["production_release_eligible"] = True
    manifest["signature_status"] = "signed"
    _rewrite(candidate, manifest)

    report = module.verify_candidate(candidate)
    assert report.findings == (
        "manifest signature_status mismatch",
        "manifest certifies its own release eligibility",
    )
    assert report.coherence_ok is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # Every security- and release-significant top-level field is bound, so
        # none of them is decorative data a candidate can edit freely.
        ("bundle_id", "com.example.other"),
        ("bundle_name", "Other"),
        ("bundle_path", "Other.app"),
        ("executable", "other-executable"),
        ("executable_sha256", "0" * 64),
        ("compatibility_id", "omnivia-core-companion-component-set/9"),
        ("candidate_status", "production"),
        ("notarization_status", "notarized"),
        ("update_owner", {"id": "own-updater"}),
        (
            "update_owner",
            {
                "app_update_owner": "omnivia-core-product-release",
                "authority": "docs/development/"
                "omnivia-core-macos-document-intake-wp-00-binding-ledger-2026-10-13.md#13",
                "existing_channel_covers_component_set": True,
                "independent_updater": False,
                "installer_owner_in_this_repository": False,
            },
        ),
    ],
)
def test_verifier_refuses_top_level_tampering(
    tmp_path: Path, key: str, value: object
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    manifest = _manifest(candidate)
    manifest[key] = value
    _rewrite(candidate, manifest)

    report = module.verify_candidate(candidate)
    assert report.coherence_ok is False
    if key == "executable_sha256":
        assert report.findings == (
            "manifest executable_sha256 is not the companion component digest",
        )
    else:
        assert report.findings == (f"manifest {key} mismatch",)


def test_verifier_refuses_unknown_and_missing_manifest_fields(tmp_path: Path) -> None:
    """Same-schema future authority is refused, not silently ignored."""
    module = _module()
    candidate = _candidate(tmp_path, module)
    manifest = _manifest(candidate)
    manifest["release_approved_by"] = "someone"
    manifest["signing_evidence"] = {"notarized": True}
    _rewrite(candidate, manifest)
    report = module.verify_candidate(candidate)
    assert report.findings == (
        "manifest field not bound by this schema: release_approved_by",
        "manifest field not bound by this schema: signing_evidence",
    )
    assert report.coherence_ok is False

    manifest = _manifest(candidate)
    del manifest["release_approved_by"]
    del manifest["signing_evidence"]
    del manifest["bundle_name"]
    _rewrite(candidate, manifest)
    report = module.verify_candidate(candidate)
    assert report.findings == (
        "manifest field missing: bundle_name",
        "manifest bundle_name mismatch",
    )

    # And inside a component declaration.
    manifest = _manifest(candidate)
    manifest["bundle_name"] = "OmniVia Core"
    _component(manifest, "share_extension")["signature_verified"] = True
    _rewrite(candidate, manifest)
    assert module.verify_candidate(candidate).findings == (
        "component field not bound by this schema: share_extension.signature_verified",
    )


def test_verifier_refuses_a_component_declared_outside_its_managed_path(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    manifest = _manifest(candidate)
    entry = _component(manifest, "share_extension")
    entry["bundle_path"] = "../elsewhere/Share to OmniVia Core.appex"
    entry["executable_path"] = "../elsewhere/Share to OmniVia Core.appex/Contents/MacOS/OmniViaShare"
    _rewrite(candidate, manifest)

    # The declared path is compared to the one accepted path, so an escape is
    # refused as a declaration rather than opened and then judged.
    assert module.verify_candidate(candidate).findings == (
        "component bundle_path mismatch: share_extension",
        "component executable_path mismatch: share_extension",
    )


def test_verifier_refuses_an_extension_left_behind_by_a_partial_update(
    tmp_path: Path,
) -> None:
    """The NAT-16 skew case as it actually arrives: assembled, not declared.

    An older extension bundle sits inside a newer app. Nothing edited the
    manifest, so the refusal has to come from the bundle on disk.
    """
    module = _module()
    old = _candidate(tmp_path, module, version="0.6.4")
    new = _candidate(tmp_path, module, version="0.6.5")
    appex = "OmniVia Core.app/Contents/PlugIns/Add to OmniVia Core.appex"
    shutil.rmtree(new / appex)
    shutil.copytree(old / appex, new / appex)

    report = module.verify_candidate(new)
    assert report.findings == (
        "component CFBundleShortVersionString mismatch: finder_action_extension",
        "component CFBundleVersion mismatch: finder_action_extension",
        "component executable digest mismatch: finder_action_extension",
    )
    assert report.release_eligible is False


def test_verifier_refuses_a_symlink_anywhere_in_the_candidate(tmp_path: Path) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    resources = candidate / "OmniVia Core.app" / "Contents" / "Resources"
    (resources / "escape").symlink_to(tmp_path)

    report = module.verify_candidate(candidate)
    assert report.findings == (
        "symlink refused: OmniVia Core.app/Contents/Resources/escape",
    )

    linked = tmp_path / "linked-candidate"
    linked.symlink_to(candidate)
    assert module.verify_candidate(linked).findings == ("candidate refused",)


def test_verifier_refuses_unreadable_relative_and_malformed_input(tmp_path: Path) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)

    assert module.verify_candidate(Path("candidate")).findings == ("candidate refused",)
    assert module.verify_candidate(tmp_path / "absent").findings == ("candidate refused",)

    (candidate / "companion-manifest.json").write_text("{not json", encoding="ascii")
    assert module.verify_candidate(candidate).findings == ("manifest unreadable",)

    (candidate / "companion-manifest.json").unlink()
    assert module.verify_candidate(candidate).findings == ("manifest refused",)


def test_verifier_refuses_a_duplicate_or_unmanaged_component_declaration(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    manifest = _manifest(candidate)
    manifest["components"] = [
        *manifest["components"],
        dict(_component(manifest, "share_extension")),
        {"role": "helper", "bundle_id": "com.omnivia.core.helper"},
    ]
    _rewrite(candidate, manifest)

    report = module.verify_candidate(candidate)
    assert "component declared twice: share_extension" in report.findings
    assert "component not managed by this set: helper" in report.findings


def test_verifier_refuses_a_candidate_past_its_entry_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    monkeypatch.setattr(module, "MAX_CANDIDATE_ENTRIES", 3)
    assert module.verify_candidate(candidate).findings == (
        "candidate exceeds the verifier's entry bound",
    )


def test_an_extensionless_build_is_not_a_coherent_component_set(tmp_path: Path) -> None:
    module = _module()
    output = tmp_path / "companion-only"
    module.build_companion_app(
        executable=_executable(tmp_path), output=output, version="0.6.5"
    )
    manifest = _manifest(output)
    assert [entry["role"] for entry in manifest["components"]] == ["companion"]

    report = module.verify_candidate(output)
    assert report.findings == (
        "managed component missing: finder_action_extension",
        "managed component missing: share_extension",
    )


def test_verifier_cli_is_fail_closed_and_reports_without_rewriting(tmp_path: Path) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    cli = ROOT / "scripts" / "verify-core-companion-bundle.py"

    clean = subprocess.run(
        [sys.executable, "-I", str(cli), str(candidate), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert clean.returncode == 0, clean.stderr
    assert json.loads(clean.stdout) == {
        "coherence_ok": True,
        "findings": [],
        "release_eligible": False,
        "signature_status": "unavailable",
    }

    required = subprocess.run(
        [sys.executable, "-I", str(cli), str(candidate), "--require-release-eligible"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert required.returncode == 1
    assert "signing verification is unavailable" in required.stderr
    assert "the app update owner is not bound by repository authority" in required.stderr

    # There is no flag by which a caller names the identity its own candidate is
    # checked against, with or without the release-eligibility demand.
    for nominated in (
        ["--expected-team-id", TEAM_ID],
        ["--expected-requirement", REQUIREMENT],
        ["--expected-team-id", TEAM_ID, "--expected-requirement", REQUIREMENT],
        [
            "--require-release-eligible",
            "--expected-team-id",
            TEAM_ID,
            "--expected-requirement",
            REQUIREMENT,
        ],
    ):
        refused = subprocess.run(
            [sys.executable, "-I", str(cli), str(candidate), *nominated],
            capture_output=True,
            text=True,
            check=False,
        )
        assert refused.returncode == 2
        assert "unrecognized arguments" in refused.stderr
        assert "verified" not in refused.stdout

    # Nor does the help text offer one.
    helped = subprocess.run(
        [sys.executable, "-I", str(cli), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert helped.returncode == 0
    assert "--expected-team-id" not in helped.stdout
    assert "--expected-requirement" not in helped.stdout

    # There is no flag by which a caller-authored file can report a signature.
    forged = tmp_path / "evidence.json"
    forged.write_text(
        json.dumps(
            {
                policy.bundle_id: {
                    "authority": "Developer ID Application: Example",
                    "cdhash": "a" * 40,
                    "notarized": True,
                    "team_identifier": TEAM_ID,
                }
                for policy in module.COMPONENT_SET
            }
        ),
        encoding="utf-8",
    )
    rejected = subprocess.run(
        [sys.executable, "-I", str(cli), str(candidate), "--signature-evidence", str(forged)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "unrecognized arguments" in rejected.stderr


def test_verifier_refuses_every_malformed_manifest_shape(tmp_path: Path) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    path = candidate / "companion-manifest.json"

    path.write_text("[]", encoding="ascii")
    assert module.verify_candidate(candidate).findings == ("manifest unreadable",)

    path.write_bytes('{"schema_version": 2, "café": 1}'.encode())
    assert module.verify_candidate(candidate).findings == ("manifest unreadable",)

    manifest = _manifest(_candidate(tmp_path, module, version="0.6.6"))
    del manifest["components"]
    _rewrite(candidate, manifest)
    assert module.verify_candidate(candidate).findings == (
        "manifest declares no component set",
    )

    manifest["components"] = ["not-a-mapping", {"bundle_id": "no-role"}]
    _rewrite(candidate, manifest)
    report = module.verify_candidate(candidate)
    assert report.findings.count("component declaration malformed") == 2
    assert "managed component missing: companion" in report.findings


def test_verifier_refuses_an_unreadable_bundle_plist(tmp_path: Path) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    relative = "OmniVia Core.app/Contents/PlugIns/Share to OmniVia Core.appex"
    (candidate / relative / "Contents" / "Info.plist").write_bytes(b"not a plist")

    report = module.verify_candidate(candidate)
    assert report.findings == (
        f"bundle metadata unreadable: {relative}",
        "component bundle missing: share_extension",
    )


def test_verifier_pins_the_release_version_form_and_the_deployment_target(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    manifest = _manifest(candidate)
    manifest["version"] = "0.6.5-rc1"
    _rewrite(candidate, manifest)
    report = module.verify_candidate(candidate)
    assert "manifest version is not a release version" in report.findings

    # A coherent set at a raised deployment target is still refused: an
    # increase is a reviewed change to the constant, not a manifest value.
    manifest = _manifest(candidate)
    manifest["version"] = "0.6.5"
    manifest["minimum_macos"] = "14.0"
    for entry in manifest["components"]:
        entry["minimum_macos"] = "14.0"
    _rewrite(candidate, manifest)
    report = module.verify_candidate(candidate)
    assert report.findings[0] == (
        "minimum macOS differs from the supported deployment target"
    )
    assert "component version skew across the set" not in report.findings
    assert module.MINIMUM_MACOS == "13.0"
