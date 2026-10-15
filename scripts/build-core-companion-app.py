#!/usr/bin/env python3
"""Build the deterministic unsigned OmniVia Core status companion app bundle.

The build also writes the component-set manifest that binds the companion and
both managed extensions as one coherent unit (section 18.1 to 18.4, WP-14).
`verify_candidate` checks an assembled candidate against that manifest and is
the fail-closed gate behind `scripts/verify-core-companion-bundle.py`.

That gate reports two separate verdicts. Coherence is local and answerable here.
Release eligibility is not: it needs `codesign`, `spctl` and `stapler` to
succeed over the exact candidate against the expected identity
`REPOSITORY_SIGNING_AUTHORITY` declares, and an app update owner that repository
authority has bound. Neither can be declared in the manifest, in any
caller-authored file, or in any argument a caller passes: the expected identity
is reviewed configuration in this module and nothing else, and today it is
`None`, so signing verification is unavailable and no candidate is release
eligible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
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
_TEAM_IDENTIFIER = re.compile(r"^[A-Z0-9]{10}$")
# Parsed out of `codesign --display --verbose=4`, which writes them one per line.
_TEAM_LINE = re.compile(r"^TeamIdentifier=(.+)$", re.MULTILINE)
_CDHASH_LINE = re.compile(r"^CDHash=[0-9a-f]{40,64}$", re.MULTILINE)

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
# This feature introduces no updater, and no owner of the app's update is named
# here, because no repository authority binds one. WP-00 binding ledger row 13:
# the existing channel (`scripts/publish-update-channel.py`) carries five Python
# wheels and not the app, "the companion has no update path", and "which
# repository owns the signed installer and the app bundle is not established
# here". The manifest records that absence rather than inventing an owner
# (section 18.3).
UPDATE_OWNER: Final = {
    "app_update_owner": "externally_unbound",
    "authority": (
        "docs/development/"
        "omnivia-core-macos-document-intake-wp-00-binding-ledger-2026-10-13.md#13"
    ),
    "existing_channel_covers_component_set": False,
    "independent_updater": False,
    "installer_owner_in_this_repository": False,
}
# Derived, so it cannot drift from the declaration above. Release eligibility
# stays refused while the app's update owner is unbound: binding it is a
# reviewed change to `UPDATE_OWNER` citing a repository authority, never a value
# a candidate manifest can carry in on its own.
APP_UPDATE_OWNER_BOUND: Final = UPDATE_OWNER["app_update_owner"] != "externally_unbound"
# Every top-level manifest field this schema binds. A manifest carrying a field
# outside this set is refused: a passing manifest holds no field that a reader
# ignores, so no field can be decorative mutable data (section 18.3).
MANIFEST_FIELDS: Final = frozenset(
    {
        "bundle_id",
        "bundle_name",
        "bundle_path",
        "build_version",
        "candidate_status",
        "compatibility_id",
        "components",
        "executable",
        "executable_sha256",
        "minimum_macos",
        "notarization_status",
        "production_release_eligible",
        "schema_version",
        "signature_status",
        "update_owner",
        "version",
    }
)
COMPONENT_FIELDS: Final = frozenset(
    {
        "bundle_id",
        "bundle_path",
        "build_version",
        "executable",
        "executable_path",
        "executable_sha256",
        "minimum_macos",
        "registration",
        "role",
        "version",
    }
)
CANDIDATE_STATUS: Final = "qualification_only"
NOTARIZATION_STATUS: Final = "not_notarized"
SIGNATURE_STATUS: Final = "unsigned"
# A bound on the candidate walk so an unreadable or hostile tree cannot make the
# gate run unbounded. A real candidate holds a few dozen entries.
MAX_CANDIDATE_ENTRIES: Final = 4096
# A bound on each platform signing authority, so a wedged tool cannot hang the
# gate. Verification of a real app bundle takes well under a second.
SIGNING_COMMAND_TIMEOUT_SECONDS: Final = 120


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
        "candidate_status": CANDIDATE_STATUS,
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
        # The build never certifies itself, and these three record only what the
        # build produced. Release eligibility is decided by `verify_candidate`
        # running the platform signing authorities over the candidate; it is
        # never read out of this file.
        "notarization_status": NOTARIZATION_STATUS,
        "production_release_eligible": False,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "signature_status": SIGNATURE_STATUS,
        "update_owner": UPDATE_OWNER,
        "version": version,
    }
    (output / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )
    return app


@dataclass(frozen=True)
class SigningAuthority:
    """What the release owner says this candidate must be signed as.

    Both halves come from the real release owner, never from the candidate and
    never from a caller: the expected Apple Team ID and the
    designated-requirement expression that `codesign -R=` must be satisfied by.
    The only instance this gate verifies against is
    `REPOSITORY_SIGNING_AUTHORITY` below. Constructing one elsewhere -- in a
    test, in a one-off script -- checks nothing and reaches no verdict.
    """

    team_identifier: str
    requirement: str

    def __post_init__(self) -> None:
        if _TEAM_IDENTIFIER.fullmatch(self.team_identifier) is None:
            raise PackagingError("expected team identifier refused")
        if not self.requirement.strip():
            raise PackagingError("expected designated requirement refused")


# The expected signing identity this gate checks against, and the only identity
# it will ever check against: repository-owned reviewed configuration, with the
# provenance of the binding recorded here. It is `None` because no such identity
# exists to record. WP-00 binding ledger row 13: "No designated signing identity
# or production signing exists in this repository: the bundle is unsigned and
# qualification-only", and signing identities and the production signing
# pipeline are established outside this repository.
#
# So there is nothing to verify a candidate against, and `verify_candidate`
# takes no argument by which a caller could supply one. Binding this is the same
# reviewed change, citing the same repository authority, that binds
# `UPDATE_OWNER`: an expected identity is the release owner's statement, and
# row 13 leaves that owner unestablished.
REPOSITORY_SIGNING_AUTHORITY: Final[SigningAuthority | None] = None


def _repository_signing_authority() -> SigningAuthority | None:
    """The identity to verify against, or `None` when there is none.

    The two bindings go together or not at all: while the app's update owner is
    externally unbound there is no release owner to have named an expected
    identity, so signing verification stays unavailable even if this constant
    were populated on its own.
    """

    if not APP_UPDATE_OWNER_BOUND:
        return None
    return REPOSITORY_SIGNING_AUTHORITY


# One platform signing authority: the argv to run, and the exit status and
# combined output it produced. A parameter of the private `_verify_signing`
# only, so unit tests can drive the parsing and the refusal paths. No public
# function takes one: `verify_candidate` always runs `_run_signing_command`,
# which is the only thing that can make a candidate release eligible.
CommandRunner = Callable[[Sequence[str]], tuple[int, str]]


def _run_signing_command(command: Sequence[str]) -> tuple[int, str]:
    """Run one real platform signing authority.

    Fail-closed: off macOS, and when the tool is absent or wedged, this raises
    and the caller turns that into a finding. There is no path by which an
    unavailable authority reports success.
    """

    if sys.platform != "darwin":
        raise OSError("platform signing authorities exist only on macOS")
    completed = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
        timeout=SIGNING_COMMAND_TIMEOUT_SECONDS,
    )
    return completed.returncode, completed.stdout + completed.stderr


@dataclass(frozen=True)
class VerificationReport:
    """The verdict on one assembled candidate.

    `coherence_ok` is the local question: does the candidate match its manifest
    and the declared component set. It is the useful verdict for an unsigned
    qualification build and says nothing about release.

    `release_eligible` is the release question, and it is a strictly narrower
    one: coherent, *and* signing verified by the platform authorities run over
    this candidate, *and* an app update owner bound by repository authority.

    `signature_status` is `unavailable` when no repository-bound expected
    identity exists to check against, so the authorities were never run -- the
    state this repository is in today. It is `unverified` when they ran and any
    one of them failed or could not be reached, and `verified` only when all of
    them succeeded.
    """

    findings: tuple[str, ...]
    coherence_ok: bool
    signature_status: str
    release_eligible: bool

    @property
    def ok(self) -> bool:
        return not self.findings


def _refused(finding: str) -> VerificationReport:
    return VerificationReport((finding,), False, "unavailable", False)


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


def _verify_signing(
    candidate: Path, authority: SigningAuthority | None, run: CommandRunner
) -> tuple[str, bool, list[str]]:
    """Verify signing by running the platform authorities over this candidate.

    Nothing is taken on a caller's word. The verdict is the exit status of
    `codesign`, `spctl` and `stapler` run over the exact bundles inside
    `candidate`: a static seal and designated-identity check on every component
    against the repository-bound expected Team ID and designated requirement,
    then the Gatekeeper assessment and the notarisation staple on the containing
    app. Every failure, and every authority that could not be run at all, is an
    explicit finding; none of them can report `verified`.

    No authority to check against is not a finding -- a local qualification
    candidate is legitimately unsigned -- but nothing is run and nothing is
    claimed: it reports `unavailable`.
    """

    if authority is None:
        return "unavailable", False, []
    findings: list[str] = []

    def check(command: list[str], label: str) -> str | None:
        try:
            status, output = run(command)
        except (OSError, subprocess.SubprocessError):
            # Off macOS, tool absent, or wedged past the bound. Fail closed.
            findings.append(f"signing authority unavailable: {label}")
            return None
        if status != 0:
            findings.append(f"signing authority refused the candidate: {label}")
            return None
        return output

    for policy in COMPONENT_SET:
        bundle = str(candidate / policy.bundle_path)
        role = policy.role
        if check(["codesign", "--verify", "--deep", "--strict", bundle], f"seal {role}") is None:
            continue
        if (
            check(
                ["codesign", "--verify", f"-R={authority.requirement}", bundle],
                f"designated requirement {role}",
            )
            is None
        ):
            continue
        details = check(["codesign", "--display", "--verbose=4", bundle], f"identity {role}")
        if details is None:
            continue
        team = _TEAM_LINE.search(details)
        if team is None or team.group(1).strip() != authority.team_identifier:
            findings.append(f"signing team identifier mismatch: {role}")
        if _CDHASH_LINE.search(details) is None:
            findings.append(f"signing reports no code directory hash: {role}")

    app = str(candidate / COMPONENT_SET[0].bundle_path)
    check(["spctl", "--assess", "--type", "exec", "--verbose=4", app], "Gatekeeper assessment")
    check(["xcrun", "stapler", "validate", app], "notarisation staple")

    if findings:
        return "unverified", False, findings
    return "verified", True, []


def verify_candidate(
    candidate: Path, *, require_release_eligible: bool = False
) -> VerificationReport:
    """Validate an assembled candidate against its component-set manifest.

    Fail-closed and bounded: every unreadable input, every mismatch and every
    unmanaged or duplicated member is a finding, and there is no skip path. No
    finding reproduces bytes; each names a component role or a relative path.

    The candidate path and `require_release_eligible` are the whole input. The
    expected signing identity is not a parameter: it comes from
    `REPOSITORY_SIGNING_AUTHORITY` and the update-owner binding, so no caller
    can nominate the identity its own candidate is checked against, and neither
    can a caller substitute the commands that check it. While that constant is
    `None` the platform authorities are not run, the signature is `unavailable`
    and no candidate is release eligible.
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
    # Strict in both directions at this schema: an unbound field is refused
    # rather than ignored, so same-schema future authority cannot slip a field
    # past this reader, and a missing field cannot pass as absent-is-fine. A
    # *newer* schema was already refused whole above, unread and unmodified.
    for field in sorted(set(manifest) - MANIFEST_FIELDS):
        findings.append(f"manifest field not bound by this schema: {field}")
    for field in sorted(MANIFEST_FIELDS - set(manifest)):
        findings.append(f"manifest field missing: {field}")

    companion_policy = COMPONENT_SET[0]
    for field, pinned in (
        # The top-level identity and path fields are the companion's, and they
        # are pinned to the same constants its component entry is, so they
        # cannot disagree with it or be edited into something decorative.
        ("bundle_id", BUNDLE_ID),
        ("bundle_name", BUNDLE_NAME),
        ("bundle_path", companion_policy.bundle_path),
        ("executable", EXECUTABLE_NAME),
        ("compatibility_id", COMPONENT_SET_COMPATIBILITY_ID),
        ("candidate_status", CANDIDATE_STATUS),
        ("notarization_status", NOTARIZATION_STATUS),
        ("signature_status", SIGNATURE_STATUS),
        ("update_owner", UPDATE_OWNER),
    ):
        if manifest.get(field) != pinned:
            findings.append(f"manifest {field} mismatch")
    if manifest.get("production_release_eligible") is not False:
        findings.append("manifest certifies its own release eligibility")
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
    companion_entry = declared.get(companion_policy.role)
    if companion_entry is None or manifest.get("executable_sha256") != companion_entry.get(
        "executable_sha256"
    ):
        findings.append("manifest executable_sha256 is not the companion component digest")
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
        for field in sorted(set(entry) - COMPONENT_FIELDS):
            findings.append(f"component field not bound by this schema: {role}.{field}")
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

    # Coherence is settled here, before signing is looked at: a signing failure
    # is not an incoherent candidate, and a clean candidate does not excuse one.
    coherence_ok = not findings
    signature_status, signed, signing_findings = _verify_signing(
        candidate, _repository_signing_authority(), _run_signing_command
    )
    findings.extend(signing_findings)

    blockers: list[str] = []
    if not coherence_ok:
        blockers.append("not release eligible: the candidate is not coherent")
    if signature_status == "unavailable":
        # Nothing was run, so nothing failed; what is missing is the expected
        # identity itself, and only a reviewed repository change supplies it.
        blockers.append(
            "not release eligible: signing verification is unavailable -- no "
            "repository authority binds an expected signing identity"
        )
    elif not signed:
        blockers.append(
            "not release eligible: signing is not verified by the platform "
            "authorities over this candidate"
        )
    if not APP_UPDATE_OWNER_BOUND:
        blockers.append(
            "not release eligible: the app update owner is not bound by repository authority"
        )
    release_eligible = not blockers
    if require_release_eligible:
        findings.extend(blockers)
    return VerificationReport(
        tuple(findings), coherence_ok, signature_status, release_eligible
    )


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
