#!/usr/bin/env python3
"""Build the deterministic unsigned OmniVia Core status companion app bundle.

The build also writes the component-set manifest that binds the companion and
both managed extensions as one coherent unit (section 18.1 to 18.4, WP-14).
`verify_candidate` checks an assembled candidate against that manifest and is
the fail-closed gate behind `scripts/verify-core-companion-bundle.py`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

BUNDLE_ID: Final = "com.omnivia.core.status"
BUNDLE_NAME: Final = "OmniVia Core"
EXECUTABLE_NAME: Final = "omnivia-core-status-menu"
MINIMUM_MACOS: Final = "13.0"
COMPANION_SERVICE_MESSAGE: Final = "addToOmniViaCore"
FINDER_EXTENSION_ID: Final = "com.omnivia.core.finder-action"
FINDER_EXTENSION_EXECUTABLE: Final = "OmniViaFinderAction"
FINDER_EXTENSION_BUNDLE_NAME: Final = "Add to OmniVia Core"
FINDER_EXTENSION_POINT: Final = "com.apple.ui-services"
SHARE_EXTENSION_ID: Final = "com.omnivia.core.share"
SHARE_EXTENSION_EXECUTABLE: Final = "OmniViaShare"
SHARE_EXTENSION_BUNDLE_NAME: Final = "Share to OmniVia Core"
SHARE_EXTENSION_POINT: Final = "com.apple.share-services"
FINDER_EXTENSION_MAX_ITEMS: Final = 10
# One bounded production activation rule, never TRUEPREDICATE. It admits
# exactly one extension item, every one of whose attachments conforms to
# public.data (a file's content) or public.directory (a folder, which
# public.folder conforms to), between one and FINDER_EXTENSION_MAX_ITEMS of
# them. That bound is the handoff mailbox's slot bound, so the Quick Action
# cannot offer a selection the companion would refuse.
#
# public.url also conforms to public.data, so the rule alone does not restrict
# activation to file URLs. The extension does: it refuses any provider that
# does not conform to public.file-url before it offers a single slot.
FINDER_EXTENSION_ACTIVATION_RULE: Final = (
    "SUBQUERY ("
    "extensionItems, $extensionItem,"
    " SUBQUERY ("
    "$extensionItem.attachments, $attachment,"
    ' ANY $attachment.registeredTypeIdentifiers UTI-CONFORMS-TO "public.data"'
    ' OR ANY $attachment.registeredTypeIdentifiers UTI-CONFORMS-TO "public.directory"'
    ").@count == $extensionItem.attachments.@count"
    " AND $extensionItem.attachments.@count >= 1"
    f" AND $extensionItem.attachments.@count <= {FINDER_EXTENSION_MAX_ITEMS}"
    ").@count == 1"
)
SHARE_EXTENSION_MAX_ITEMS: Final = 10
# A bounded dictionary rule, never TRUEPREDICATE. The Share extension captures
# copies of four things and declares exactly those: files, images, plain text,
# and web (http/https) links. Every count is the handoff slot bound, so the
# share sheet cannot offer a selection the extension would refuse. A web URL is
# captured as the text of the link: the extension has no network entitlement
# and resolves nothing.
SHARE_EXTENSION_ACTIVATION_RULE: Final = {
    "NSExtensionActivationSupportsFileWithMaxCount": SHARE_EXTENSION_MAX_ITEMS,
    "NSExtensionActivationSupportsImageWithMaxCount": SHARE_EXTENSION_MAX_ITEMS,
    "NSExtensionActivationSupportsText": True,
    "NSExtensionActivationSupportsWebURLWithMaxCount": SHARE_EXTENSION_MAX_ITEMS,
}
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_CDHASH = re.compile(r"^[0-9a-f]{40,64}$")

MANIFEST_NAME: Final = "companion-manifest.json"
MANIFEST_SCHEMA_VERSION: Final = 2
# Exactly one schema is read. A newer or unknown schema is preserved unread and
# refused: nothing in this gate rewrites, downgrades or field-strips a manifest
# it does not understand (section 18.3).
SUPPORTED_MANIFEST_SCHEMA_VERSIONS: Final = frozenset({MANIFEST_SCHEMA_VERSION})
# The identifier a reader checks before it trusts any other field. It changes
# when the component set itself changes -- a member added, removed or renamed --
# so a manifest from a different set is refused rather than partially matched.
COMPONENT_SET_COMPATIBILITY_ID: Final = "omnivia-core-companion-component-set/1"
# This feature introduces no updater. The manifest declares who owns the update
# instead, and a candidate claiming its own updater is refused (section 18.3).
# The installed-product installer is not owned by this repository; see the WP-00
# binding ledger row 13.
UPDATE_OWNER: Final = {
    "id": "omnivia-core-product-release",
    "independent_updater": False,
    "installer_owner_in_this_repository": False,
    "updates_component_set_as_one_unit": True,
}
# A bound on the candidate walk so an unreadable or hostile tree cannot make the
# gate run unbounded. A real candidate holds a few dozen entries.
MAX_CANDIDATE_ENTRIES: Final = 4096


@dataclass(frozen=True)
class ComponentPolicy:
    """One managed member of the companion component set.

    `bundle_path` and `executable` are the only accepted locations: a declared
    path that is not exactly this one is refused, which is what makes a path
    escape unrepresentable rather than merely detected.
    """

    role: str
    bundle_id: str
    bundle_path: str
    executable: str
    registration: str

    @property
    def executable_path(self) -> str:
        return f"{self.bundle_path}/Contents/MacOS/{self.executable}"


COMPONENT_SET: Final = (
    ComponentPolicy(
        role="companion",
        bundle_id=BUNDLE_ID,
        bundle_path=f"{BUNDLE_NAME}.app",
        executable=EXECUTABLE_NAME,
        registration=f"service:{BUNDLE_NAME}/{COMPANION_SERVICE_MESSAGE}",
    ),
    ComponentPolicy(
        role="finder_action_extension",
        bundle_id=FINDER_EXTENSION_ID,
        bundle_path=f"{BUNDLE_NAME}.app/Contents/PlugIns/{FINDER_EXTENSION_BUNDLE_NAME}.appex",
        executable=FINDER_EXTENSION_EXECUTABLE,
        registration=f"extension-point:{FINDER_EXTENSION_POINT}",
    ),
    ComponentPolicy(
        role="share_extension",
        bundle_id=SHARE_EXTENSION_ID,
        bundle_path=f"{BUNDLE_NAME}.app/Contents/PlugIns/{SHARE_EXTENSION_BUNDLE_NAME}.appex",
        executable=SHARE_EXTENSION_EXECUTABLE,
        registration=f"extension-point:{SHARE_EXTENSION_POINT}",
    ),
)
# The whole skew policy, as one predicate: every member of an installable set
# carries the same compatibility id, marketing version, build version and
# minimum macOS as the manifest that binds them. Any difference -- a companion
# newer than an extension, one extension left behind by a partial update -- is
# refused as a set, never adopted member by member (NAT-16).
COHERENT_FIELDS: Final = ("version", "build_version", "minimum_macos")


class PackagingError(Exception):
    pass


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_executable(path: Path, label: str) -> None:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise PackagingError(f"{label} executable refused")


def _write_extension(
    *,
    plugins: Path,
    source: Path,
    bundle_name: str,
    executable_name: str,
    bundle_id: str,
    version: str,
    extension: dict[str, object],
) -> Path:
    bundle = plugins / f"{bundle_name}.appex"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True, mode=0o755)
    target = macos / executable_name
    shutil.copyfile(source, target, follow_symlinks=False)
    os.chmod(target, 0o755)
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": executable_name,
        "CFBundleIdentifier": bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": bundle_name,
        "CFBundlePackageType": "XPC!",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": MINIMUM_MACOS,
        "NSExtension": extension,
    }
    plist_path = bundle / "Contents" / "Info.plist"
    plist_path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML, sort_keys=True))
    os.chmod(plist_path, 0o644)
    return target


def build_companion_app(
    *,
    executable: Path,
    output: Path,
    version: str,
    finder_extension_executable: Path | None = None,
    share_extension_executable: Path | None = None,
) -> Path:
    _validate_executable(executable, "companion")
    if (finder_extension_executable is None) != (share_extension_executable is None):
        raise PackagingError("extension pair refused")
    if finder_extension_executable is not None and share_extension_executable is not None:
        _validate_executable(finder_extension_executable, "Finder Action")
        _validate_executable(share_extension_executable, "Share")
    if _SEMVER.fullmatch(version) is None:
        raise PackagingError("companion version refused")
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise PackagingError("companion output refused")

    app = output / f"{BUNDLE_NAME}.app"
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    plugins = app / "Contents" / "PlugIns"
    macos.mkdir(parents=True, mode=0o755)
    resources.mkdir(mode=0o755)
    target = macos / EXECUTABLE_NAME
    shutil.copyfile(executable, target, follow_symlinks=False)
    os.chmod(target, 0o755)

    major, minor, patch = (int(member) for member in version.split("."))
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": BUNDLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": f"{major}.{minor}.{patch}",
        "LSMinimumSystemVersion": MINIMUM_MACOS,
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
                "NSMessage": COMPANION_SERVICE_MESSAGE,
                "NSPortName": BUNDLE_NAME,
                "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
                "NSSendFileTypes": ["public.data", "public.directory"],
            }
        ],
    }
    info = app / "Contents" / "Info.plist"
    info.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))
    os.chmod(info, 0o644)

    digests = {COMPONENT_SET[0].role: _file_digest(target)}
    if finder_extension_executable is not None and share_extension_executable is not None:
        plugins.mkdir(mode=0o755)
        finder_target = _write_extension(
            plugins=plugins,
            source=finder_extension_executable,
            bundle_name=FINDER_EXTENSION_BUNDLE_NAME,
            executable_name=FINDER_EXTENSION_EXECUTABLE,
            bundle_id=FINDER_EXTENSION_ID,
            version=version,
            extension={
                "NSExtensionPointIdentifier": FINDER_EXTENSION_POINT,
                "NSExtensionPrincipalClass": "FinderActionViewController",
                "NSExtensionAttributes": {
                    "NSExtensionActivationRule": FINDER_EXTENSION_ACTIVATION_RULE,
                    "NSExtensionServiceAllowsFinderPreviewItem": True,
                    "NSExtensionServiceRoleType": "NSExtensionServiceRoleTypeViewer",
                },
            },
        )
        share_target = _write_extension(
            plugins=plugins,
            source=share_extension_executable,
            bundle_name=SHARE_EXTENSION_BUNDLE_NAME,
            executable_name=SHARE_EXTENSION_EXECUTABLE,
            bundle_id=SHARE_EXTENSION_ID,
            version=version,
            extension={
                "NSExtensionPointIdentifier": SHARE_EXTENSION_POINT,
                "NSExtensionPrincipalClass": "ShareViewController",
                "NSExtensionAttributes": {
                    "NSExtensionActivationRule": SHARE_EXTENSION_ACTIVATION_RULE,
                },
            },
        )
        digests["finder_action_extension"] = _file_digest(finder_target)
        digests["share_extension"] = _file_digest(share_target)

    manifest = {
        "bundle_id": BUNDLE_ID,
        "bundle_name": BUNDLE_NAME,
        "bundle_path": f"{BUNDLE_NAME}.app",
        "build_version": version,
        "candidate_status": "qualification_only",
        "compatibility_id": COMPONENT_SET_COMPATIBILITY_ID,
        "components": [
            {
                "bundle_id": policy.bundle_id,
                "bundle_path": policy.bundle_path,
                "build_version": version,
                "executable": policy.executable,
                "executable_path": policy.executable_path,
                "executable_sha256": digests[policy.role],
                "minimum_macos": MINIMUM_MACOS,
                "registration": policy.registration,
                "role": policy.role,
                "version": version,
            }
            for policy in COMPONENT_SET
            if policy.role in digests
        ],
        "executable": EXECUTABLE_NAME,
        "executable_sha256": digests[COMPONENT_SET[0].role],
        "minimum_macos": MINIMUM_MACOS,
        # The build never certifies itself. Release eligibility is reported by
        # `verify_candidate` and only against checked signing evidence.
        "notarization_status": "not_notarized",
        "production_release_eligible": False,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "signature_status": "unsigned",
        "update_owner": UPDATE_OWNER,
        "version": version,
    }
    (output / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )
    return app


@dataclass(frozen=True)
class VerificationReport:
    """The verdict on one assembled candidate.

    `findings` empty means the candidate matches its manifest and the declared
    component set. `signature_status` is `unverified` whenever no caller-supplied
    signing evidence was checked, and `release_eligible` is never true in that
    case: an unsigned candidate is not release eligible and this gate does not
    pretend otherwise.
    """

    findings: tuple[str, ...]
    signature_status: str
    release_eligible: bool

    @property
    def ok(self) -> bool:
        return not self.findings


def _refused(finding: str) -> VerificationReport:
    return VerificationReport((finding,), "unverified", False)


def _read_plist(path: Path) -> dict[str, object] | None:
    try:
        loaded = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return loaded if isinstance(loaded, dict) else None


def _observed_bundles(
    candidate: Path, findings: list[str]
) -> dict[str, dict[str, object]]:
    """Every bundle under the candidate, keyed by its relative bundle path.

    A bundle is any `Contents/Info.plist`. Reading them all -- rather than only
    the declared paths -- is what makes an unmanaged or duplicated bundle
    visible instead of merely absent from the manifest.
    """

    bundles: dict[str, dict[str, object]] = {}
    for plist_path in sorted(candidate.rglob("Info.plist")):
        if plist_path.parent.name != "Contents" or not plist_path.is_file():
            continue
        relative = plist_path.parent.parent.relative_to(candidate).as_posix()
        plist = _read_plist(plist_path)
        if plist is None:
            findings.append(f"bundle metadata unreadable: {relative}")
            continue
        bundles[relative] = plist
    return bundles


def _registrations(plist: Mapping[str, object]) -> list[str]:
    found: list[str] = []
    services = plist.get("NSServices")
    if isinstance(services, list):
        for service in services:
            if isinstance(service, Mapping):
                port = service.get("NSPortName")
                message = service.get("NSMessage")
                found.append(f"service:{port}/{message}")
    extension = plist.get("NSExtension")
    if isinstance(extension, Mapping):
        found.append(f"extension-point:{extension.get('NSExtensionPointIdentifier')}")
    return found


def _check_signature_evidence(
    evidence: Mapping[str, object] | None, findings: list[str]
) -> tuple[str, bool]:
    """Report signing only from evidence a caller supplied and this gate read.

    Nothing here runs `codesign` or `spctl`; the evidence is the record of a run
    that did. Absent evidence is not a finding -- a local qualification candidate
    is legitimately unsigned -- but it can never report `verified`.
    """

    if evidence is None:
        return "unverified", False
    # Scoped to this evidence: a coherence finding elsewhere in the candidate
    # does not make the signing record unread, and an unread signing record is
    # not hidden by a clean candidate.
    own: list[str] = []
    expected = {policy.bundle_id for policy in COMPONENT_SET}
    covered = set(evidence)
    for bundle_id in sorted(expected - covered):
        own.append(f"signing evidence missing for component: {bundle_id}")
    for bundle_id in sorted(covered - expected):
        own.append(f"signing evidence for unmanaged component: {bundle_id}")
    teams: set[str] = set()
    for bundle_id in sorted(expected & covered):
        record = evidence[bundle_id]
        if not isinstance(record, Mapping):
            own.append(f"signing evidence malformed: {bundle_id}")
            continue
        authority = record.get("authority")
        team = record.get("team_identifier")
        cdhash = record.get("cdhash")
        if not isinstance(authority, str) or not authority:
            own.append(f"signing evidence has no authority: {bundle_id}")
        if not isinstance(team, str) or not team:
            own.append(f"signing evidence has no team identifier: {bundle_id}")
        else:
            teams.add(team)
        if not isinstance(cdhash, str) or _CDHASH.fullmatch(cdhash) is None:
            own.append(f"signing evidence has no code directory hash: {bundle_id}")
        if record.get("notarized") is not True:
            own.append(f"signing evidence is not notarized: {bundle_id}")
    if len(teams) > 1:
        own.append("signing evidence spans more than one team identifier")
    findings.extend(own)
    return ("verified", True) if not own else ("unverified", False)


def verify_candidate(
    candidate: Path,
    *,
    signature_evidence: Mapping[str, object] | None = None,
    require_release_eligible: bool = False,
) -> VerificationReport:
    """Validate an assembled candidate against its component-set manifest.

    Fail-closed and bounded: every unreadable input, every mismatch and every
    unmanaged or duplicated member is a finding, and there is no skip path. No
    finding reproduces bytes; each names a component role or a relative path.
    """

    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
        return _refused("candidate refused")
    manifest_path = candidate / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return _refused("manifest refused")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _refused("manifest unreadable")
    if not isinstance(manifest, dict):
        return _refused("manifest unreadable")
    schema = manifest.get("schema_version")
    if schema not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        # Refused whole and left exactly as written. A downgraded reader must not
        # mutate a newer envelope into an older one (section 18.3).
        return _refused(f"manifest schema unsupported: {schema!r}")

    findings: list[str] = []
    if manifest.get("compatibility_id") != COMPONENT_SET_COMPATIBILITY_ID:
        findings.append("component set compatibility identifier mismatch")
    if manifest.get("update_owner") != UPDATE_OWNER:
        findings.append("update owner declaration mismatch")
    if manifest.get("production_release_eligible") is not False:
        findings.append("manifest certifies its own release eligibility")
    if manifest.get("signature_status") != "unsigned":
        findings.append("manifest claims a signature this build cannot produce")
    if _SEMVER.fullmatch(str(manifest.get("version"))) is None:
        # The same version form the build refuses, so the two gates cannot
        # disagree about what a release version is.
        findings.append("manifest version is not a release version")
    if manifest.get("minimum_macos") != MINIMUM_MACOS:
        # Pinned to the supported deployment target rather than merely
        # self-consistent: an increase is a reviewed change to this constant,
        # not a value a candidate can carry in on its own (section 18.1).
        findings.append("minimum macOS differs from the supported deployment target")

    for entries, path in enumerate(candidate.rglob("*"), start=1):
        if entries > MAX_CANDIDATE_ENTRIES:
            return _refused("candidate exceeds the verifier's entry bound")
        if path.is_symlink():
            findings.append(f"symlink refused: {path.relative_to(candidate).as_posix()}")

    declared: dict[str, Mapping[str, object]] = {}
    components = manifest.get("components")
    if not isinstance(components, list):
        return _refused("manifest declares no component set")
    for entry in components:
        if not isinstance(entry, Mapping):
            findings.append("component declaration malformed")
            continue
        role = entry.get("role")
        if not isinstance(role, str):
            findings.append("component declaration malformed")
        elif role in declared:
            findings.append(f"component declared twice: {role}")
        else:
            declared[role] = entry
    policies = {policy.role: policy for policy in COMPONENT_SET}
    for role in sorted(set(policies) - set(declared)):
        findings.append(f"managed component missing: {role}")
    for role in sorted(set(declared) - set(policies)):
        findings.append(f"component not managed by this set: {role}")

    bundles = _observed_bundles(candidate, findings)
    seen_bundle_ids: dict[str, list[str]] = {}
    for relative, plist in sorted(bundles.items()):
        identifier = plist.get("CFBundleIdentifier")
        seen_bundle_ids.setdefault(str(identifier), []).append(relative)
    for identifier, paths in sorted(seen_bundle_ids.items()):
        if len(paths) > 1:
            findings.append(f"duplicate bundle identifier: {identifier}")
    managed_paths = {policy.bundle_path for policy in COMPONENT_SET}
    managed_ids = {policy.bundle_id for policy in COMPONENT_SET}
    for relative, plist in sorted(bundles.items()):
        if relative in managed_paths:
            continue
        if str(plist.get("CFBundleIdentifier")) in managed_ids:
            findings.append(f"managed bundle identifier at unmanaged path: {relative}")
        else:
            findings.append(f"unmanaged bundle in candidate: {relative}")

    seen_registrations: dict[str, list[str]] = {}
    for relative, plist in sorted(bundles.items()):
        for registration in _registrations(plist):
            seen_registrations.setdefault(registration, []).append(relative)
    for registration, paths in sorted(seen_registrations.items()):
        if len(paths) > 1:
            findings.append(f"duplicate service registration: {registration}")
    for registration in sorted(
        set(seen_registrations) - {policy.registration for policy in COMPONENT_SET}
    ):
        findings.append(f"unmanaged service registration: {registration}")

    coherent: set[tuple[object, ...]] = set()
    for role, policy in sorted(policies.items()):
        entry = declared.get(role)
        if entry is None:
            continue
        coherent.add(tuple(entry.get(field) for field in COHERENT_FIELDS))
        for field, expected in (
            ("bundle_id", policy.bundle_id),
            ("bundle_path", policy.bundle_path),
            ("executable", policy.executable),
            ("executable_path", policy.executable_path),
            ("registration", policy.registration),
        ):
            if entry.get(field) != expected:
                findings.append(f"component {field} mismatch: {role}")
        for field in COHERENT_FIELDS:
            if entry.get(field) != manifest.get(field):
                findings.append(f"component {field} differs from the manifest: {role}")

        bundle = bundles.get(policy.bundle_path)
        if bundle is None:
            findings.append(f"component bundle missing: {role}")
        else:
            for key, want in (
                ("CFBundleIdentifier", policy.bundle_id),
                ("CFBundleExecutable", policy.executable),
                ("CFBundleShortVersionString", entry.get("version")),
                ("CFBundleVersion", entry.get("build_version")),
                ("LSMinimumSystemVersion", entry.get("minimum_macos")),
            ):
                if bundle.get(key) != want:
                    findings.append(f"component {key} mismatch: {role}")
            observed = _registrations(bundle)
            if observed != [policy.registration]:
                findings.append(f"component registration set mismatch: {role}")

        executable = candidate / policy.executable_path
        if executable.is_symlink() or not executable.is_file():
            findings.append(f"component executable missing: {role}")
        elif _file_digest(executable) != entry.get("executable_sha256"):
            findings.append(f"component executable digest mismatch: {role}")

    if len(coherent) > 1:
        findings.append("component version skew across the set")

    signature_status, signed = _check_signature_evidence(signature_evidence, findings)
    release_eligible = signed and not findings
    if require_release_eligible and not release_eligible:
        findings.append("candidate is not release eligible")
    return VerificationReport(tuple(findings), signature_status, release_eligible)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--finder-extension-executable", required=True, type=Path)
    parser.add_argument("--share-extension-executable", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        build_companion_app(
            executable=arguments.executable,
            output=arguments.output,
            version=arguments.version,
            finder_extension_executable=arguments.finder_extension_executable,
            share_extension_executable=arguments.share_extension_executable,
        )
    except (OSError, PackagingError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
