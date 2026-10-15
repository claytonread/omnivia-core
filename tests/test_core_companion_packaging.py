from __future__ import annotations

import importlib.util
import json
import plistlib
import shutil
import stat
import subprocess
import sys
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


def _evidence(module: ModuleType, **overrides: object) -> dict[str, object]:
    record = {
        "authority": "Developer ID Application: Example (TEAMID1234)",
        "cdhash": "a" * 40,
        "notarized": True,
        "team_identifier": "TEAMID1234",
    }
    return {
        policy.bundle_id: {**record, **overrides}
        for policy in module.COMPONENT_SET
    }


def test_manifest_binds_the_companion_and_both_extensions_as_one_component_set(
    tmp_path: Path,
) -> None:
    module = _module()
    manifest = _manifest(_candidate(tmp_path, module))

    assert manifest["schema_version"] == module.MANIFEST_SCHEMA_VERSION == 2
    assert manifest["compatibility_id"] == module.COMPONENT_SET_COMPATIBILITY_ID
    assert manifest["version"] == manifest["build_version"] == "0.6.5"
    assert manifest["minimum_macos"] == "13.0"
    # No independent updater, and the installed-product installer is owned
    # outside this repository.
    assert manifest["update_owner"] == {
        "id": "omnivia-core-product-release",
        "independent_updater": False,
        "installer_owner_in_this_repository": False,
        "updates_component_set_as_one_unit": True,
    }
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


def test_verifier_accepts_a_coherent_set_and_never_calls_it_release_eligible(
    tmp_path: Path,
) -> None:
    module = _module()
    report = module.verify_candidate(_candidate(tmp_path, module))
    assert report.findings == ()
    assert report.ok is True
    # Unsigned. Nothing read a signature, so nothing claims one.
    assert report.signature_status == "unverified"
    assert report.release_eligible is False


def test_release_qualification_refuses_an_unsigned_candidate(tmp_path: Path) -> None:
    module = _module()
    report = module.verify_candidate(
        _candidate(tmp_path, module), require_release_eligible=True
    )
    assert report.findings == ("candidate is not release eligible",)
    assert report.release_eligible is False


def test_release_eligibility_requires_checked_evidence_for_every_component(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)

    full = _evidence(module)
    report = module.verify_candidate(
        candidate, signature_evidence=full, require_release_eligible=True
    )
    assert report.findings == ()
    assert report.signature_status == "verified"
    assert report.release_eligible is True

    partial = dict(full)
    del partial["com.omnivia.core.share"]
    report = module.verify_candidate(candidate, signature_evidence=partial)
    assert report.findings == (
        "signing evidence missing for component: com.omnivia.core.share",
    )
    assert report.signature_status == "unverified"
    assert report.release_eligible is False

    report = module.verify_candidate(
        candidate, signature_evidence=_evidence(module, notarized=False)
    )
    assert all("not notarized" in finding for finding in report.findings)
    assert len(report.findings) == 3
    assert report.release_eligible is False

    stranger = dict(full)
    stranger["com.omnivia.core.status"] = {**stranger["com.omnivia.core.status"]}
    stranger["com.omnivia.core.status"]["team_identifier"] = "OTHERTEAM1"
    report = module.verify_candidate(candidate, signature_evidence=stranger)
    assert "signing evidence spans more than one team identifier" in report.findings
    assert report.release_eligible is False

    extra = dict(full)
    extra["com.omnivia.core.unmanaged"] = full["com.omnivia.core.share"]
    report = module.verify_candidate(candidate, signature_evidence=extra)
    assert report.findings == (
        "signing evidence for unmanaged component: com.omnivia.core.unmanaged",
    )


def test_a_coherence_finding_does_not_make_verified_signing_evidence_unread(
    tmp_path: Path,
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    (candidate / "OmniVia Core.app" / "Contents" / "MacOS" / "omnivia-core-status-menu").write_bytes(
        b"tampered"
    )
    report = module.verify_candidate(candidate, signature_evidence=_evidence(module))
    assert report.findings == ("component executable digest mismatch: companion",)
    assert report.signature_status == "verified"
    assert report.release_eligible is False


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
        "manifest certifies its own release eligibility",
        "manifest claims a signature this build cannot produce",
    )


@pytest.mark.parametrize(
    ("key", "value", "finding"),
    [
        (
            "compatibility_id",
            "omnivia-core-companion-component-set/9",
            "component set compatibility identifier mismatch",
        ),
        ("update_owner", {"id": "own-updater"}, "update owner declaration mismatch"),
    ],
)
def test_verifier_refuses_a_foreign_set_or_updater_declaration(
    tmp_path: Path, key: str, value: object, finding: str
) -> None:
    module = _module()
    candidate = _candidate(tmp_path, module)
    manifest = _manifest(candidate)
    manifest[key] = value
    _rewrite(candidate, manifest)

    assert module.verify_candidate(candidate).findings == (finding,)


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
        "findings": [],
        "release_eligible": False,
        "signature_status": "unverified",
    }

    required = subprocess.run(
        [sys.executable, "-I", str(cli), str(candidate), "--require-release-eligible"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert required.returncode == 1
    assert "candidate is not release eligible" in required.stderr

    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(_evidence(module)), encoding="utf-8")
    signed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(cli),
            str(candidate),
            "--signature-evidence",
            str(evidence),
            "--require-release-eligible",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert signed.returncode == 0, signed.stderr
    assert json.loads(signed.stdout)["release_eligible"] is True

    evidence.write_text("[]", encoding="utf-8")
    malformed = subprocess.run(
        [sys.executable, "-I", str(cli), str(candidate), "--signature-evidence", str(evidence)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed.returncode == 1
    assert "signature evidence unreadable" in malformed.stderr


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
