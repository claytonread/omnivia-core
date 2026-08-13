#!/usr/bin/env python3
"""Build and qualify one V06-7 Standard-profile wheel candidate.

The output is a platform candidate, not a production release.  It contains the
five first-party wheels, the exact reviewed MCP dependency closure for this
platform, a clean offline-install journey, checksums, SPDX 2.3 SBOM, license and
NOTICE material, compatibility evidence, and an explicit unsigned state.  The
script imports no OmniVia package; every product assertion crosses an installed
wheel entry point.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.parser
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import sysconfig
import tempfile
import venv
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
CONSTRAINTS: Final = REPO_ROOT / "scripts" / "mcp-wheelhouse-constraints.txt"
JOURNEY: Final = REPO_ROOT / "scripts" / "run-standard-journey.py"
LIFECYCLE: Final = REPO_ROOT / "scripts" / "run-standard-lifecycle.py"
FIRST_PARTY_PROJECTS: Final = (
    ("omnivia-core", REPO_ROOT),
    ("omnivia-core-runtime", REPO_ROOT / "packages" / "omnivia-core-runtime"),
    ("omnivia-core-client", REPO_ROOT / "packages" / "omnivia-core-client"),
    ("omnivia-core-cli", REPO_ROOT / "packages" / "omnivia-core-cli"),
    ("omnivia-core-mcp", REPO_ROOT / "packages" / "omnivia-core-mcp"),
)
FIRST_PARTY_NAMES: Final = frozenset(name for name, _path in FIRST_PARTY_PROJECTS)
METADATA_DIRECTORY: Final = "metadata"
EVIDENCE_DIRECTORY: Final = "evidence"
WHEEL_DIRECTORY: Final = "wheels"
LICENSE_DIRECTORY: Final = "licenses"
CHECKSUMS_FILE: Final = "checksums-sha256.txt"
METADATA_FILES: Final = (
    "build-provenance.json",
    "compatibility-matrix.json",
    "qualification-result.json",
    "release-manifest.json",
    "sbom.spdx.json",
    "signature-verification.json",
    "third-party-licenses.json",
    "NOTICE.txt",
)
MAX_LICENSE_BYTES: Final = 4 * 1024 * 1024


class CandidateError(RuntimeError):
    """The candidate cannot be built or does not satisfy its own manifest."""


@dataclass(frozen=True, slots=True)
class SourceState:
    revision: str
    source_date_epoch: int
    created: str
    dirty: bool


@dataclass(frozen=True, slots=True)
class WheelPackage:
    name: str
    normalized_name: str
    version: str
    filename: str
    sha256: str
    size: int
    requires_python: str | None
    requires_dist: tuple[str, ...]
    declared_license: str
    license_members: tuple[str, ...]


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _display(command: Sequence[str]) -> str:
    return shlex.join(command)


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {_display(command)}", flush=True)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise CandidateError(
            f"command exited {completed.returncode}: {_display(command)}\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
        )
    return completed


def _git(*arguments: str) -> str:
    return _run(["git", *arguments], cwd=REPO_ROOT).stdout.strip()


def _source_state(*, allow_dirty: bool) -> SourceState:
    revision = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    dirty = bool(status)
    if dirty and not allow_dirty:
        raise CandidateError(
            "the source tree is dirty; commit the candidate definition before building"
        )
    epoch_text = _git("show", "-s", "--format=%ct", "HEAD")
    try:
        epoch = int(epoch_text)
    except ValueError as error:
        raise CandidateError("the source revision has no usable commit timestamp") from error
    created = (
        dt.datetime.fromtimestamp(epoch, tz=dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return SourceState(revision, epoch, created, dirty)


def _empty_output(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise CandidateError("the candidate output directory must be absent or empty")
    path.mkdir(parents=True, exist_ok=True)


def _build_environment(source: SourceState) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["SOURCE_DATE_EPOCH"] = str(source.source_date_epoch)
    return environment


def _build_wheels(wheelhouse: Path, source: SourceState) -> None:
    environment = _build_environment(source)
    for name, project in FIRST_PARTY_PROJECTS:
        before = set(wheelhouse.glob("*.whl"))
        _run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(wheelhouse),
                str(project),
            ],
            cwd=REPO_ROOT,
            environment=environment,
        )
        built = set(wheelhouse.glob("*.whl")) - before
        if len(built) != 1:
            raise CandidateError(f"{name}: expected exactly one new wheel")

    mcp = next(
        path
        for path in wheelhouse.glob("*.whl")
        if path.name.startswith("omnivia_core_mcp-")
    )
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--constraint",
            str(CONSTRAINTS),
            "--dest",
            str(wheelhouse),
            "--find-links",
            str(wheelhouse),
            str(mcp),
        ],
        cwd=REPO_ROOT,
        environment=environment,
    )


def _metadata_member(archive: zipfile.ZipFile) -> str:
    members = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(members) != 1:
        raise CandidateError("wheel does not contain exactly one METADATA document")
    return members[0]


def _license_members(archive: zipfile.ZipFile, dist_info: str) -> tuple[str, ...]:
    accepted: list[str] = []
    prefix = f"{dist_info}/"
    for member in archive.namelist():
        if member.endswith("/") or not member.startswith(prefix):
            continue
        relative = member[len(prefix) :]
        basename = Path(relative).name.upper()
        if relative.startswith("licenses/") or basename.startswith(
            ("LICENSE", "COPYING", "NOTICE", "AUTHORS")
        ):
            accepted.append(member)
    return tuple(sorted(accepted))


def inspect_wheel(path: Path) -> WheelPackage:
    """Read one wheel's identity and license inventory without importing it."""
    with zipfile.ZipFile(path) as archive:
        metadata_member = _metadata_member(archive)
        metadata_bytes = archive.read(metadata_member)
        message = email.parser.BytesParser().parsebytes(metadata_bytes)
        name = message.get("Name")
        version = message.get("Version")
        if not name or not version:
            raise CandidateError(f"{path.name}: METADATA omits Name or Version")
        dist_info = metadata_member.rsplit("/", 1)[0]
        license_members = _license_members(archive, dist_info)
        declared = (
            message.get("License-Expression")
            or message.get("License")
            or "NOASSERTION"
        ).strip()
    return WheelPackage(
        name=name,
        normalized_name=_normalized(name),
        version=version,
        filename=path.name,
        sha256=_digest_file(path),
        size=path.stat().st_size,
        requires_python=message.get("Requires-Python"),
        requires_dist=tuple(sorted(message.get_all("Requires-Dist", []))),
        declared_license=declared or "NOASSERTION",
        license_members=license_members,
    )


def _constraint_versions(path: Path) -> dict[str, str]:
    constraints: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, version = line.partition("==")
        if not separator or not name or not version:
            raise CandidateError("the dependency constraints must contain exact pins only")
        normalized = _normalized(name)
        if normalized in constraints:
            raise CandidateError(f"duplicate dependency constraint for {normalized}")
        constraints[normalized] = version
    return constraints


def _inspect_closure(wheelhouse: Path) -> tuple[WheelPackage, ...]:
    packages = tuple(sorted((inspect_wheel(path) for path in wheelhouse.glob("*.whl")), key=lambda item: item.normalized_name))
    by_name = {package.normalized_name: package for package in packages}
    if len(by_name) != len(packages):
        raise CandidateError("the wheelhouse contains duplicate distributions")
    normalized_first_party = {_normalized(name) for name in FIRST_PARTY_NAMES}
    if normalized_first_party - set(by_name):
        raise CandidateError("the wheelhouse omits a Standard-profile distribution")
    constraints = _constraint_versions(CONSTRAINTS)
    third_party = set(by_name) - normalized_first_party
    if third_party != set(constraints):
        raise CandidateError(
            "the resolved third-party closure differs from the reviewed constraints"
        )
    for name, expected_version in constraints.items():
        if by_name[name].version != expected_version:
            raise CandidateError(
                f"{name}: resolved {by_name[name].version}, expected {expected_version}"
            )
    return packages


def _safe_license_name(member: str, ordinal: int) -> str:
    basename = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(member).name)
    return f"{ordinal:02d}-{basename or 'LICENSE'}"


def _extract_licenses(
    wheelhouse: Path,
    packages: Sequence[WheelPackage],
    license_root: Path,
) -> list[dict[str, Any]]:
    license_root.mkdir()
    records: list[dict[str, Any]] = []
    root_license = REPO_ROOT / "LICENSE"
    suite = license_root / "omnivia-core-suite"
    suite.mkdir()
    suite_license = suite / "LICENSE"
    suite_license.write_bytes(root_license.read_bytes())

    normalized_first_party = {_normalized(name) for name in FIRST_PARTY_NAMES}
    for package in packages:
        copied: list[dict[str, object]] = []
        if package.normalized_name in normalized_first_party:
            copied.append(
                {
                    "path": suite_license.relative_to(license_root.parent).as_posix(),
                    "sha256": _digest_file(suite_license),
                    "bytes": suite_license.stat().st_size,
                }
            )
        else:
            target = license_root / f"{package.normalized_name}-{package.version}"
            target.mkdir()
            with zipfile.ZipFile(wheelhouse / package.filename) as archive:
                for ordinal, member in enumerate(package.license_members, 1):
                    content = archive.read(member)
                    if len(content) > MAX_LICENSE_BYTES:
                        raise CandidateError(
                            f"{package.name}: embedded license file exceeds the bound"
                        )
                    destination = target / _safe_license_name(member, ordinal)
                    destination.write_bytes(content)
                    copied.append(
                        {
                            "path": destination.relative_to(license_root.parent).as_posix(),
                            "sha256": _digest_bytes(content),
                            "bytes": len(content),
                        }
                    )
        records.append(
            {
                "name": package.name,
                "version": package.version,
                "first_party": package.normalized_name in normalized_first_party,
                "declared_license": (
                    "MIT"
                    if package.normalized_name in normalized_first_party
                    else package.declared_license
                ),
                "license_files": copied,
                "wheel": package.filename,
            }
        )
    return records


def _python(virtual_environment: Path) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return virtual_environment / directory / executable


def _wheel_environment(virtual_environment: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    directory = virtual_environment / ("Scripts" if os.name == "nt" else "bin")
    environment["PATH"] = os.pathsep.join(
        [str(directory), environment.get("PATH", "")]
    )
    return environment


def _offline_qualification(
    wheelhouse: Path, evidence: Path, temporary: Path
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    virtual_environment = temporary / "standard-environment"
    venv.EnvBuilder(with_pip=True, clear=True).create(virtual_environment)
    environment = _wheel_environment(virtual_environment)
    python = _python(virtual_environment)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--only-binary=:all:",
            "--find-links",
            str(wheelhouse),
            *[name for name, _path in FIRST_PARTY_PROJECTS],
        ],
        cwd=temporary,
        environment=environment,
    )
    installed = _run(
        [str(python), "-m", "pip", "freeze", "--all"],
        cwd=temporary,
        environment=environment,
    )
    freeze = sorted(
        line.strip()
        for line in installed.stdout.splitlines()
        if line.strip() and not line.lower().startswith(("pip==", "setuptools=="))
    )
    _run(
        [str(python), str(JOURNEY), "--output", str(evidence)],
        cwd=temporary,
        environment=environment,
        timeout=300,
    )
    result_path = evidence / "standalone-journey-result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CandidateError("the standalone journey result is absent or malformed") from error
    if result.get("verdict") != "pass":
        raise CandidateError("the standalone journey did not pass")
    lifecycle_path = evidence / "standard-lifecycle-result.json"
    _run(
        [str(python), str(LIFECYCLE), "--output", str(lifecycle_path)],
        cwd=temporary,
        environment=environment,
        timeout=300,
    )
    try:
        lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CandidateError("the Standard lifecycle result is absent or malformed") from error
    if lifecycle.get("verdict") != "pass":
        raise CandidateError("the Standard lifecycle qualification did not pass")
    return result, lifecycle, freeze


def _package_document(package: WheelPackage) -> dict[str, object]:
    identifier = re.sub(r"[^A-Za-z0-9.-]", "-", package.normalized_name)
    return {
        "SPDXID": f"SPDXRef-Package-{identifier}",
        "name": package.name,
        "versionInfo": package.version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": (
            "MIT"
            if package.normalized_name
            in {_normalized(name) for name in FIRST_PARTY_NAMES}
            else package.declared_license or "NOASSERTION"
        ),
        "copyrightText": "NOASSERTION",
        "checksums": [{"algorithm": "SHA256", "checksumValue": package.sha256}],
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": (
                    f"pkg:pypi/{package.normalized_name}@{package.version}"
                ),
            }
        ],
    }


def _write_metadata(
    output: Path,
    source: SourceState,
    packages: Sequence[WheelPackage],
    licenses: Sequence[dict[str, Any]],
    journey: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    freeze: Sequence[str],
) -> None:
    metadata = output / METADATA_DIRECTORY
    metadata.mkdir()
    wheel_entries: list[dict[str, Any]] = [
        {
            "name": package.name,
            "version": package.version,
            "path": f"{WHEEL_DIRECTORY}/{package.filename}",
            "sha256": package.sha256,
            "bytes": package.size,
            "requires_python": package.requires_python,
            "requires_dist": list(package.requires_dist),
            "first_party": package.normalized_name
            in {_normalized(name) for name in FIRST_PARTY_NAMES},
        }
        for package in packages
    ]
    platform_id = platform.system().lower()
    machine = platform.machine().lower()
    python_version = platform.python_version()
    candidate_key = _digest_bytes(
        "\n".join(str(entry["sha256"]) for entry in wheel_entries).encode()
    )
    first_party_versions = {
        package.normalized_name: package.version
        for package in packages
        if package.normalized_name
        in {_normalized(name) for name in FIRST_PARTY_NAMES}
    }
    versions = journey.get("versions")
    workspace = journey.get("workspace")
    if not isinstance(versions, Mapping) or not isinstance(workspace, Mapping):
        raise CandidateError("the standalone journey omitted compatibility versions")
    api = versions.get("api")
    if not isinstance(api, Mapping):
        raise CandidateError("the standalone journey omitted its API window")
    compatibility_versions = {
        "first_party_distributions": first_party_versions,
        "core": first_party_versions[_normalized("omnivia-core")],
        "client": first_party_versions[_normalized("omnivia-core-client")],
        "workspace_format": workspace.get("format"),
        "workspace_contract": versions.get("workspace_contract"),
        "protocol": versions.get("protocol"),
        "api": {
            "minimum": api.get("minimum"),
            "maximum": api.get("maximum"),
        },
    }
    compatibility_api = compatibility_versions["api"]
    if not isinstance(compatibility_api, Mapping):  # pragma: no cover - local literal
        raise CandidateError("the compatibility API window is malformed")
    if any(
        value is None
        for value in (
            compatibility_versions["core"],
            compatibility_versions["client"],
            compatibility_versions["workspace_format"],
            compatibility_versions["workspace_contract"],
            compatibility_versions["protocol"],
            compatibility_api["minimum"],
            compatibility_api["maximum"],
        )
    ):
        raise CandidateError("the compatibility matrix contains an absent version")

    provenance = {
        "format": "omnivia.standard-build-provenance.v1",
        "source": {
            "revision": source.revision,
            "dirty": source.dirty,
            "source_date_epoch": source.source_date_epoch,
        },
        "builder": {
            "python": python_version,
            "implementation": platform.python_implementation(),
            "system": platform_id,
            "machine": machine,
            "sysconfig_platform": sysconfig.get_platform(),
        },
        "dependency_resolution": {
            "constraints": "scripts/mcp-wheelhouse-constraints.txt",
            "constraints_sha256": _digest_file(CONSTRAINTS),
            "exact_closure_verified": True,
        },
        "reproducibility": {
            "source_timestamp_applied": True,
            "offline_install_verified": True,
            "same_revision_same_platform_rebuild_expected": True,
            "cross_platform_wheels_intentionally_distinct": True,
        },
    }
    _write_json(metadata / "build-provenance.json", provenance)

    matrix_rows = []
    for system, runner in (
        ("linux", "ubuntu-latest"),
        ("darwin", "macos-latest"),
        ("windows", "windows-latest"),
    ):
        matrix_rows.append(
            {
                "system": system,
                "runner": runner,
                "python": "3.11",
                "status": (
                    "passed_in_this_candidate"
                    if system == platform_id
                    else "required_in_separate_matrix_row"
                ),
            }
        )
    compatibility = {
        "format": "omnivia.standard-compatibility-matrix.v1",
        "candidate_key": candidate_key,
        "rows": matrix_rows,
        "current": {
            "system": platform_id,
            "machine": machine,
            "python": python_version,
            "journey": "pass",
            "versions": compatibility_versions,
        },
        "complete_matrix_claimed": False,
    }
    _write_json(metadata / "compatibility-matrix.json", compatibility)

    qualification = {
        "format": "omnivia.standard-candidate-qualification.v1",
        "verdict": "pass",
        "candidate_key": candidate_key,
        "profile": [name for name, _path in FIRST_PARTY_PROJECTS],
        "wheel_count": len(packages),
        "offline_install": True,
        "installed_distributions": list(freeze),
        "standalone_journey": journey,
        "lifecycle": lifecycle,
        "skips": 0,
        "xfails": 0,
    }
    _write_json(metadata / "qualification-result.json", qualification)

    signature = {
        "format": "omnivia.signature-verification.v1",
        "status": "unsigned",
        "verification_performed": False,
        "production_release_eligible": False,
        "reason": (
            "No release signing identity or private key is configured. This is a "
            "qualification candidate and must not be represented as signed."
        ),
        "required_for_production_release": True,
    }
    _write_json(metadata / "signature-verification.json", signature)

    third_party_packages = [entry for entry in licenses if not entry["first_party"]]
    third_party = {
        "format": "omnivia.third-party-license-inventory.v1",
        "packages": third_party_packages,
        "first_party_license": f"{LICENSE_DIRECTORY}/omnivia-core-suite/LICENSE",
    }
    _write_json(metadata / "third-party-licenses.json", third_party)

    notice_lines = [
        "OmniVia Core Standard-profile qualification candidate",
        "",
        "This candidate is unsigned and is not a production release.",
        "Third-party packages included in the platform wheel closure:",
    ]
    for entry in third_party_packages:
        files = entry["license_files"]
        paths = ", ".join(str(item["path"]) for item in files) or "no embedded file"
        notice_lines.append(
            f"- {entry['name']} {entry['version']}: "
            f"{entry['declared_license']} ({paths})"
        )
    (metadata / "NOTICE.txt").write_text("\n".join(notice_lines) + "\n", encoding="utf-8")

    spdx_packages = [_package_document(package) for package in packages]
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "omnivia-core-standard-candidate",
        "documentNamespace": (
            "https://omnivia.dev/spdx/standard/"
            f"{source.revision}/{platform_id}-{machine}/{candidate_key}"
        ),
        "creationInfo": {
            "created": source.created,
            "creators": ["Tool: scripts/build-standard-candidate.py"],
        },
        "packages": spdx_packages,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": package["SPDXID"],
            }
            for package in spdx_packages
        ],
    }
    _write_json(metadata / "sbom.spdx.json", sbom)

    manifest = {
        "format": "omnivia.standard-release-manifest.v1",
        "candidate_status": "qualification_only",
        "production_release_eligible": False,
        "source_revision": source.revision,
        "candidate_key": candidate_key,
        "profile": [name for name, _path in FIRST_PARTY_PROJECTS],
        "wheels": wheel_entries,
        "evidence": [
            f"{EVIDENCE_DIRECTORY}/standalone-journey-result.json",
            f"{EVIDENCE_DIRECTORY}/standard-lifecycle-result.json",
        ],
        "metadata": [f"{METADATA_DIRECTORY}/{name}" for name in METADATA_FILES],
        "checksums": {
            "path": CHECKSUMS_FILE,
            "algorithm": "SHA256",
            "scope": "every regular candidate file except the checksum file itself",
        },
        "signing": {
            "status": "unsigned",
            "verification": f"{METADATA_DIRECTORY}/signature-verification.json",
        },
    }
    _write_json(metadata / "release-manifest.json", manifest)


def write_checksums(output: Path) -> None:
    """Write and then verify the deterministic whole-candidate checksum index."""
    checksum_path = output / CHECKSUMS_FILE
    files = sorted(
        path
        for path in output.rglob("*")
        if path.is_file() and path != checksum_path
    )
    lines = [
        f"{_digest_file(path)}  {path.relative_to(output).as_posix()}" for path in files
    ]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    verify_checksums(output)


def verify_checksums(output: Path) -> None:
    checksum_path = output / CHECKSUMS_FILE
    with checksum_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=" ", skipinitialspace=True))
    seen: set[str] = set()
    for row in rows:
        if len(row) != 2:
            raise CandidateError("the checksum index contains a malformed row")
        expected, relative = row
        if relative == CHECKSUMS_FILE or relative in seen:
            raise CandidateError("the checksum index contains a duplicate or itself")
        seen.add(relative)
        target = output / Path(relative)
        if not target.is_file() or _digest_file(target) != expected:
            raise CandidateError(f"checksum verification failed for {relative}")
    actual = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and path != checksum_path
    }
    if seen != actual:
        raise CandidateError("the checksum index does not cover the whole candidate")


def build_candidate(output: Path, *, allow_dirty: bool = False) -> None:
    source = _source_state(allow_dirty=allow_dirty)
    _empty_output(output)
    wheelhouse = output / WHEEL_DIRECTORY
    evidence = output / EVIDENCE_DIRECTORY
    license_root = output / LICENSE_DIRECTORY
    wheelhouse.mkdir()
    _build_wheels(wheelhouse, source)
    packages = _inspect_closure(wheelhouse)
    licenses = _extract_licenses(wheelhouse, packages, license_root)
    with tempfile.TemporaryDirectory(prefix="omnivia-standard-candidate-") as temporary:
        journey, lifecycle, freeze = _offline_qualification(
            wheelhouse, evidence, Path(temporary)
        )
    _write_metadata(output, source, packages, licenses, journey, lifecycle, freeze)
    write_checksums(output)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="build a local diagnostic candidate and record that it was dirty",
    )
    args = parser.parse_args(argv)
    try:
        build_candidate(args.output, allow_dirty=args.allow_dirty)
    except CandidateError as error:
        print(f"Standard candidate failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "format": "omnivia.standard-candidate-build.v1",
                "output": str(args.output),
                "verdict": "pass",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
