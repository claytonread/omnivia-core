"""Production Release Package scanning: bytes, not file names.

Development exclusion needs *both* source-graph exclusion and packaged-byte
absence. Checking member names proves neither: a production bundle called
``app.js`` can contain a whole test-driver API and pass a name check
untouched. So this scans the real contents -- member path plus bytes, plus the
source graph -- and fails closed.

Three things are proved here:

- **byte-level detection** -- development and conformance controls are found
  inside benignly named bundles, minified chunks, generated output, source
  maps, Python, JSON, plain text and nested archive members;
- **fail-closed inventory handling** -- a package whose contents cannot be
  trusted to be complete and well formed is rejected rather than scanned
  optimistically. Malformed, unreadable, symlinked, duplicate and escaping
  entries are findings in themselves;
- **raw-byte secret detection** -- private-key blocks and the common
  credential/token prefixes and assignments are found, benign lookalikes are
  not, and no diagnostic ever reproduces the bytes it rejected.

The gate is for *production Module Release packages*. Core's own canonical
conformance resources stay packageable and are deliberately not run through
it; the last test in this module pins that distinction.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from omnivia_core.host_contract.v1 import publication as pub

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER = REPO_ROOT / "scripts" / "scan-release-package.py"


def _load_scanner_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_release_package", SCANNER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scanner = _load_scanner_module()


def zip_bytes(members: dict[str, bytes], encrypted: frozenset[str] = frozenset()) -> bytes:
    """Return a ZIP where the named members declare ZIP encryption in both headers.

    ``zipfile`` clears the general-purpose bit flag when it writes, so the
    encryption bit is set afterwards in each named member's local file header
    and central directory entry. The member bodies stay plaintext, which is
    deliberate: the gate must classify a member from what its headers declare,
    without depending on whether an attempted read happens to fail.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    raw = bytearray(buffer.getvalue())

    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as archive:
        for info in archive.infolist():
            if info.filename in encrypted:
                raw[info.header_offset + 6] |= 0x01

    end = raw.rfind(b"PK\x05\x06")
    assert end >= 0
    position = int.from_bytes(raw[end + 16 : end + 20], "little")
    while raw[position : position + 4] == b"PK\x01\x02":
        name_length = int.from_bytes(raw[position + 28 : position + 30], "little")
        extra_length = int.from_bytes(raw[position + 30 : position + 32], "little")
        comment_length = int.from_bytes(raw[position + 32 : position + 34], "little")
        name = bytes(raw[position + 46 : position + 46 + name_length]).decode("utf-8")
        if name in encrypted:
            raw[position + 8] |= 0x01
        position += 46 + name_length + extra_length + comment_length
    return bytes(raw)


PRIVATE_KEY = (
    b"-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----\n"
)
OPENSSH_KEY = b"-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA\n"
PGP_KEY = b"-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBGF\n"


def member(path: str, data: bytes, **overrides: object) -> pub.PackageMember:
    return pub.PackageMember(path=path, data=data, **overrides)  # type: ignore[arg-type]


def inventory(*members: pub.PackageMember, **overrides: object) -> pub.PackageInventory:
    base: dict[str, object] = {
        "members": members,
        "source_symbols": (),
        "source_graph_exhaustive": True,
        "exhaustive": True,
    }
    base.update(overrides)
    return pub.PackageInventory(**base)  # type: ignore[arg-type]


def codes(findings: tuple[pub.PackageFinding, ...]) -> set[str]:
    return {finding.code for finding in findings}


CLEAN = (
    member("index.html", b"<!doctype html><title>Documents</title>"),
    member("assets/app.js", b'export const routeId="documents.home";'),
    member("manifest.json", json.dumps({"appId": "app.documents"}).encode("utf-8")),
)


# --------------------------------------------------------------------------
# A clean production package passes
# --------------------------------------------------------------------------


def test_a_clean_production_package_reports_nothing() -> None:
    assert pub.scan_release_package(inventory(*CLEAN)) == ()


def test_an_empty_but_exhaustive_package_reports_nothing() -> None:
    assert pub.scan_release_package(inventory()) == ()


# --------------------------------------------------------------------------
# Development and conformance controls, found in the bytes
# --------------------------------------------------------------------------


def test_a_test_driver_api_inside_a_benignly_named_bundle_is_found() -> None:
    """The exact false pass the independent review executed: ``app.js`` whose
    *content* is a test-driver API, whose *name* says nothing.
    """
    findings = pub.scan_release_package(
        inventory(
            member("assets/app.js", b"function send(r){return TestDriverRequest(r)}")
        )
    )
    assert codes(findings) == {"development_control_in_packaged_bytes"}
    assert findings[0].member == "member[0]"
    assert findings[0].detector == "TestDriverRequest"
    assert findings[0].offset == 24
    assert "assets/app.js" not in repr(findings[0])


@pytest.mark.parametrize(
    ("label", "path", "data", "detector", "offset"),
    [
        (
            "minified",
            "assets/chunk-a1b2c3.js",
            b'!function(){var e="testDriverRequest"}();',
            "TestDriverRequest",
            19,
        ),
        (
            "generated",
            "dist/generated.js",
            b"/*gen*/const M={DevelopmentProfile:1};",
            "DevelopmentProfile",
            16,
        ),
        (
            "source-map",
            "assets/app.js.map",
            b'{"sources":["src/test-driver-request.ts"]}',
            "TestDriverRequest",
            17,
        ),
        (
            "python",
            "server/helpers.py",
            b"class DevelopmentProfileLoader:\n    pass\n",
            "DevelopmentProfile",
            6,
        ),
        (
            "json",
            "config/runtime.json",
            b'{"scenario":"shell-scenario"}',
            "ShellScenario",
            13,
        ),
        (
            "text",
            "NOTES.txt",
            b"remember to strip the test_driver_result path\n",
            "TestDriverResult",
            22,
        ),
        ("upper", "assets/legacy.js", b"SHELLSCENARIO_ID=1", "ShellScenario", 0),
    ],
)
def test_a_development_control_is_found_whatever_it_is_hiding_in(
    label: str, path: str, data: bytes, detector: str, offset: int
) -> None:
    findings = pub.scan_release_package(inventory(member(path, data)))
    assert codes(findings) == {"development_control_in_packaged_bytes"}, label
    assert findings[0].member == "member[0]"
    assert findings[0].detector == detector
    assert findings[0].offset == offset
    assert path not in repr(findings[0])


def test_a_development_control_inside_a_nested_archive_member_is_found() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("inner/app.js", 'const d = "TestDriverResult";')
    findings = pub.scan_release_package(
        inventory(member("vendor/bundle.zip", buffer.getvalue()))
    )
    assert codes(findings) == {"development_control_in_packaged_bytes"}
    assert findings[0].member == "member[0]!member[0]"
    assert findings[0].detector == "TestDriverResult"
    assert findings[0].offset == 11
    assert "vendor/bundle.zip" not in repr(findings[0])
    assert "inner/app.js" not in repr(findings[0])


def test_zip_recognition_belongs_to_the_recogniser_and_nowhere_else() -> None:
    """``_ARCHIVE_MAGIC`` was a second, unused copy of one of these signatures.

    It was referenced by nothing -- not the recogniser, not the readers, not the
    public surface -- so removing it removes a spelling that could drift away
    from the one that actually decides. This pins the decision that stayed: all
    three ZIP signatures and the suffix are still what makes a member an
    archive, and the removed name is not coming back as a live input.
    """
    assert not hasattr(pub, "_ARCHIVE_MAGIC")
    assert "_ARCHIVE_MAGIC" not in vars(pub)
    assert "_ARCHIVE_MAGIC" not in pub.__all__

    inner = zip_bytes({"inner/app.js": b'const d = "TestDriverResult";'})
    assert inner.startswith(b"PK\x03\x04")
    for label, data, path in (
        ("local file header", inner, "vendor/bundle.bin"),
        ("empty central directory", b"PK\x05\x06" + inner[4:], "vendor/bundle.bin"),
        ("spanning signature", b"PK\x07\x08" + inner[4:], "vendor/bundle.bin"),
        ("suffix alone", inner, "vendor/bundle.zip"),
    ):
        assert pub._archive_kind(path, data) == "zip", label

    findings = pub.scan_release_package(inventory(member("vendor/bundle.bin", inner)))
    assert codes(findings) == {"development_control_in_packaged_bytes"}
    assert findings[0].member == "member[0]!member[0]"


def test_a_development_control_in_the_source_graph_is_a_finding_on_its_own() -> None:
    findings = pub.scan_release_package(
        inventory(*CLEAN, source_symbols=("HostRequest", "TestDriverRequest"))
    )
    assert codes(findings) == {"development_control_in_source_graph"}
    assert "TestDriverRequest" in findings[0].detail


def test_both_halves_are_checked_independently() -> None:
    findings = pub.scan_release_package(
        inventory(
            member("assets/app.js", b'const d="ShellScenario";'),
            source_symbols=("DevelopmentProfile",),
        )
    )
    assert codes(findings) == {
        "development_control_in_source_graph",
        "development_control_in_packaged_bytes",
    }


def test_a_disabled_development_flag_does_not_excuse_packaged_bytes() -> None:
    """A runtime flag proves nothing about what shipped."""
    findings = pub.scan_release_package(
        inventory(
            member("assets/app.js", b"TestDriverRequest"),
            development_flag_enabled=False,
        )
    )
    assert codes(findings) == {"development_control_in_packaged_bytes"}


def test_the_scanned_markers_cover_every_governed_development_record() -> None:
    for record in pub.DEVELOPMENT_ONLY_RECORDS:
        findings = pub.scan_release_package(
            inventory(member("assets/app.js", record.encode("ascii")))
        )
        assert codes(findings) == {"development_control_in_packaged_bytes"}, record


# --------------------------------------------------------------------------
# Untrustworthy inventories fail closed
# --------------------------------------------------------------------------


def test_an_inventory_that_is_not_declared_exhaustive_is_rejected() -> None:
    findings = pub.scan_release_package(pub.PackageInventory(members=CLEAN))
    assert "incomplete_inventory" in codes(findings)


def test_an_inventory_of_the_wrong_type_is_rejected() -> None:
    findings = pub.scan_release_package(None)  # type: ignore[arg-type]
    assert codes(findings) == {"incomplete_inventory"}


def test_a_symlinked_member_is_rejected() -> None:
    findings = pub.scan_release_package(
        inventory(member("assets/link.js", b"", is_symlink=True))
    )
    assert "symlinked_member" in codes(findings)


def test_a_duplicate_member_path_is_rejected() -> None:
    findings = pub.scan_release_package(
        inventory(member("assets/app.js", b"a"), member("assets/app.js", b"b"))
    )
    assert "duplicate_member" in codes(findings)


@pytest.mark.parametrize(
    "path",
    [
        "../escape.js",
        "assets/../../escape.js",
        "/etc/passwd",
        "C:\\windows\\system32",
        "assets\\app.js",
        "./assets/app.js",
        "assets//app.js",
    ],
)
def test_an_escaping_or_non_canonical_member_path_is_rejected(path: str) -> None:
    findings = pub.scan_release_package(inventory(member(path, b"ok")))
    assert "escaping_member" in codes(findings), path


@pytest.mark.parametrize(
    ("label", "entry"),
    [
        ("not-a-member", "assets/app.js"),
        ("path-not-a-string", pub.PackageMember(path=b"x", data=b"ok")),  # type: ignore[arg-type]
        ("data-not-bytes", pub.PackageMember(path="assets/app.js", data="ok")),  # type: ignore[arg-type]
        ("empty-path", pub.PackageMember(path="", data=b"ok")),
        ("nul-in-path", pub.PackageMember(path="assets/\x00.js", data=b"ok")),
    ],
)
def test_a_malformed_member_is_rejected(label: str, entry: object) -> None:
    findings = pub.scan_release_package(inventory(entry))  # type: ignore[arg-type]
    assert "malformed_member" in codes(findings), label


def test_an_unreadable_member_is_rejected() -> None:
    findings = pub.scan_release_package(
        inventory(member("vendor/bundle.zip", b"PK\x03\x04 truncated and unreadable"))
    )
    assert "unreadable_member" in codes(findings)


def test_a_package_over_the_scanner_bound_is_rejected_rather_than_partly_scanned() -> (
    None
):
    oversized = member("assets/huge.js", b"x" * (pub.MAX_SCANNED_MEMBER_BYTES + 1))
    findings = pub.scan_release_package(inventory(oversized))
    assert "incomplete_inventory" in codes(findings)


# --------------------------------------------------------------------------
# Raw-byte secret detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("detector", "data"),
    [
        ("private_key_block", PRIVATE_KEY),
        ("private_key_block", OPENSSH_KEY),
        ("pgp_private_key_block", PGP_KEY),
        ("aws_access_key_id", b'{"id":"AKIAIOSFODNN7EXAMPLE"}'),
        ("github_token", b"ghp_" + b"a" * 36),
        ("slack_token", b"xoxb-123456789012-abcdefABCDEF"),
        ("google_api_key", b"AIza" + b"B" * 35),
        ("stripe_secret_key", b"sk_live_" + b"4" * 24),
        (
            "json_web_token",
            (
                b"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                b"eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkEifQ."
                b"SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
            ),
        ),
        ("credential_assignment", b'const conf = {"apiKey": "aB3dE6gH9jK2mN5pQ8sT"};'),
        ("credential_assignment", b"client_secret = 'zY9xW8vU7tS6rQ5pO4nM3lK2'\n"),
        ("basic_auth_url", b"https://deployer:hunter2hunter2@registry.example.com/v2/"),
    ],
)
def test_a_likely_secret_in_packaged_bytes_is_found(detector: str, data: bytes) -> None:
    findings = pub.scan_release_package(inventory(member("assets/app.js", data)))
    assert codes(findings) == {"likely_secret_in_packaged_bytes"}
    assert findings[0].detector == detector
    assert findings[0].offset >= 0


@pytest.mark.parametrize(
    ("label", "data"),
    [
        ("label-only", b'const passwordFieldLabel = "Password";'),
        ("short-value", b'{"secret": "abc"}'),
        ("empty-value", b'{"apiKey": ""}'),
        ("env-reference", b"const apiKey = process.env.API_KEY;"),
        ("prose", b"# Put your API key in the console, never in the repository.\n"),
        ("prefix-alone", b'const vendors = ["AKIA", "ghp_", "sk_live_"];'),
        (
            "public-key",
            b"-----BEGIN PUBLIC KEY-----\nMIIBIjAN\n-----END PUBLIC KEY-----\n",
        ),
        (
            "certificate",
            b"-----BEGIN CERTIFICATE-----\nMIIDdzCC\n-----END CERTIFICATE-----\n",
        ),
        (
            "url-in-password-field",
            b'{"password": "https://example.com/reset-password"}',
        ),
        ("identifier-value", b'{"secretName": "documents-storage-credential-name"}'),
    ],
)
def test_a_benign_lookalike_is_not_reported_as_a_secret(
    label: str, data: bytes
) -> None:
    findings = pub.scan_release_package(inventory(member("assets/app.js", data)))
    assert findings == (), f"{label}: {[finding.detail for finding in findings]}"


def test_the_secret_detector_set_is_declared() -> None:
    assert pub.SECRET_DETECTORS == (
        "private_key_block",
        "pgp_private_key_block",
        "aws_access_key_id",
        "github_token",
        "slack_token",
        "google_api_key",
        "stripe_secret_key",
        "json_web_token",
        "credential_assignment",
        "basic_auth_url",
    )


def test_no_finding_ever_reproduces_the_bytes_it_rejected() -> None:
    secret = b"ghp_" + b"z" * 36
    findings = pub.scan_release_package(
        inventory(
            member("assets/app.js", secret),
            member("assets/key.pem", PRIVATE_KEY),
            member("assets/other.js", b'{"apiKey": "aB3dE6gH9jK2mN5pQ8sT"}'),
        )
    )
    assert len(findings) == 3
    rendered = "\n".join(
        f"{finding.code} {finding.member} {finding.detail}" for finding in findings
    )
    for leaked in (b"ghp_zzzz", b"MIIEvQIBADANBgkq", b"aB3dE6gH9jK2mN5pQ8sT"):
        assert leaked.decode("ascii") not in rendered
    assert "hunter" not in rendered


def test_the_scan_codes_are_a_fixed_declared_set() -> None:
    assert pub.PACKAGE_SCAN_CODES == (
        "incomplete_inventory",
        "malformed_member",
        "unreadable_member",
        "symlinked_member",
        "duplicate_member",
        "escaping_member",
        "development_control_in_source_graph",
        "development_control_in_packaged_bytes",
        "likely_secret_in_packaged_bytes",
    )


def test_the_old_name_only_check_is_gone() -> None:
    """A name-only check that still existed would still be the easy call to make."""
    from omnivia_core.host_contract import v1

    assert not hasattr(pub, "production_exclusion_findings")
    assert "production_exclusion_findings" not in v1.__all__


# --------------------------------------------------------------------------
# The executable gate
# --------------------------------------------------------------------------


def test_core_scanner_truthfully_records_the_downstream_g1_condition() -> None:
    documentation = SCANNER.read_text(encoding="utf-8")
    assert "Core's executable contract decision gate" in documentation
    assert "does not certify a Platform production build" in documentation
    assert "downstream G1 condition" in documentation


def run_scanner(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    resolved_arguments = list(arguments)
    if resolved_arguments and "--source-symbols" not in resolved_arguments:
        source_graph = Path(resolved_arguments[0]).parent / "source-symbols.txt"
        source_graph.write_text("", encoding="utf-8")
        resolved_arguments.extend(("--source-symbols", str(source_graph)))
    return subprocess.run(
        [sys.executable, str(SCANNER), *resolved_arguments],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )


def write_package(root: Path, files: dict[str, bytes]) -> Path:
    package = root / "package"
    for relative, data in files.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return package


def test_the_scanner_cli_passes_a_clean_directory(tmp_path: Path) -> None:
    """Legacy package witnesses provide an explicit exhaustive empty source graph."""
    package = write_package(
        tmp_path,
        {
            "index.html": b"<!doctype html>",
            "assets/app.js": b'export const routeId="documents.home";',
        },
    )
    result = run_scanner(str(package))
    assert result.returncode == 0, result.stderr
    assert "no findings" in result.stdout


def test_the_scanner_cli_fails_a_directory_with_a_test_driver_in_a_bundle(
    tmp_path: Path,
) -> None:
    package = write_package(
        tmp_path, {"assets/app.js": b"function s(r){return TestDriverRequest(r)}"}
    )
    result = run_scanner(str(package))
    assert result.returncode == 1
    assert "development_control_in_packaged_bytes" in result.stdout


def test_the_scanner_cli_fails_a_directory_carrying_a_private_key(
    tmp_path: Path,
) -> None:
    package = write_package(tmp_path, {"assets/id_rsa": PRIVATE_KEY})
    result = run_scanner(str(package))
    assert result.returncode == 1
    assert "likely_secret_in_packaged_bytes" in result.stdout
    assert "MIIEvQIBADANBgkq" not in result.stdout


def test_the_scanner_cli_scans_a_zip_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("index.html", "<!doctype html>")
        archive.writestr("assets/app.js", 'const d = "shell-scenario";')
    result = run_scanner(str(archive_path))
    assert result.returncode == 1
    assert "development_control_in_packaged_bytes" in result.stdout


def test_the_scanner_cli_passes_a_clean_zip_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("index.html", "<!doctype html>")
    result = run_scanner(str(archive_path))
    assert result.returncode == 0, result.stderr


def test_the_scanner_cli_names_the_encrypted_member_rather_than_failing_the_package(
    tmp_path: Path,
) -> None:
    """An encrypted member is classified from its header, not from a failed read.

    Both outcomes fail closed. Only this one says *which* member could not be
    scanned: reading an encrypted member raises, and letting that raise
    propagate collapses the whole package into one generic refusal that reports
    no member and emits no machine-readable report at all.
    """
    archive_path = tmp_path / "release.zip"
    archive_path.write_bytes(
        zip_bytes(
            {"assets/app.js": b'const routeId="documents.home";'},
            frozenset({"assets/app.js"}),
        )
    )
    result = run_scanner(str(archive_path), "--json")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [finding["code"] for finding in report["findings"]] == ["unreadable_member"]
    assert report["findings"][0]["member"] == "member[0]"
    assert report["findings"][0]["detail"] == "encrypted member is unreadable"
    assert report["scanned"] == 1
    assert "assets/app.js" not in result.stdout
    assert "assets/app.js" not in result.stderr


def test_the_scanner_cli_still_scans_the_readable_members_beside_an_encrypted_one(
    tmp_path: Path,
) -> None:
    """The encrypted member is one finding, not an excuse to stop scanning."""
    archive_path = tmp_path / "release.zip"
    archive_path.write_bytes(
        zip_bytes(
            {
                "assets/app.js": b"TestDriverRequest",
                "vendor/locked.bin": b"opaque",
            },
            frozenset({"vendor/locked.bin"}),
        )
    )

    result = run_scanner(str(archive_path), "--json")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert {finding["code"] for finding in report["findings"]} == {
        "development_control_in_packaged_bytes",
        "unreadable_member",
    }
    assert report["scanned"] == 2


def test_the_reader_classifies_an_encrypted_member_deterministically(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "release.zip"
    archive_path.write_bytes(
        zip_bytes({"assets/app.js": PRIVATE_KEY}, frozenset({"assets/app.js"}))
    )

    first = scanner.read_package(archive_path)
    second = scanner.read_package(archive_path)
    assert first == second
    assert first == (
        pub.PackageMember(path="assets/app.js", data=b"", is_encrypted=True),
    )
    findings = pub.scan_release_package(
        pub.PackageInventory(
            members=first,
            source_symbols=(),
            source_graph_exhaustive=True,
            exhaustive=True,
        )
    )
    assert codes(findings) == {"unreadable_member"}


def test_the_reader_never_reads_an_encrypted_member_body(tmp_path: Path) -> None:
    """A member the gate refuses is a member whose bytes it never held."""
    archive_path = tmp_path / "release.zip"
    archive_path.write_bytes(
        zip_bytes({"assets/app.js": PRIVATE_KEY}, frozenset({"assets/app.js"}))
    )
    members = scanner.read_package(archive_path)
    assert members[0].data == b""
    assert PRIVATE_KEY not in b"".join(entry.data for entry in members)


def test_the_scanner_cli_rejects_an_archive_member_that_escapes(tmp_path: Path) -> None:
    archive_path = tmp_path / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.js", "ok")
    result = run_scanner(str(archive_path))
    assert result.returncode == 1
    assert "escaping_member" in result.stdout


def test_the_scanner_cli_rejects_a_symlink_in_a_directory(tmp_path: Path) -> None:
    package = write_package(tmp_path, {"index.html": b"<!doctype html>"})
    (package / "link.html").symlink_to(package / "index.html")
    result = run_scanner(str(package))
    assert result.returncode == 1
    assert "symlinked_member" in result.stdout


def test_the_scanner_cli_fails_closed_on_a_path_that_is_not_a_package(
    tmp_path: Path,
) -> None:
    result = run_scanner(str(tmp_path / "nothing-here"))
    assert result.returncode == 1


def test_the_scanner_cli_reports_the_source_graph_too(tmp_path: Path) -> None:
    package = write_package(tmp_path, {"index.html": b"<!doctype html>"})
    symbols = tmp_path / "symbols.txt"
    symbols.write_text("HostRequest\nTestDriverRequest\n", encoding="utf-8")
    result = run_scanner(str(package), "--source-symbols", str(symbols))
    assert result.returncode == 1
    assert "development_control_in_source_graph" in result.stdout


def test_the_scanner_cli_fails_closed_on_malformed_source_graph_bytes(
    tmp_path: Path,
) -> None:
    secret = "ghp_" + "g" * 36
    package = write_package(tmp_path, {"index.html": b"<!doctype html>"})
    symbols = tmp_path / f"{secret}.txt"
    symbols.write_bytes(b"HostRequest\n\xff\n")
    result = run_scanner(str(package), "--source-symbols", str(symbols))
    assert result.returncode == 1
    assert "source symbol inventory is unreadable" in result.stderr
    assert "Traceback" not in result.stderr
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_the_scanner_cli_emits_machine_readable_findings(tmp_path: Path) -> None:
    package = write_package(tmp_path, {"assets/app.js": b"TestDriverRequest"})
    result = run_scanner(str(package), "--json")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["findings"][0]["code"] == "development_control_in_packaged_bytes"
    assert report["findings"][0]["member"] == "member[0]"
    assert report["findings"][0]["detector"] == "TestDriverRequest"
    assert report["findings"][0]["offset"] == 0
    assert "assets/app.js" not in result.stdout
    assert "assets/app.js" not in result.stderr
    assert report["scanned"] == 1


def test_the_scanner_cli_never_echoes_a_secret_shaped_package_name(
    tmp_path: Path,
) -> None:
    secret = "ghp_" + "n" * 36
    archive_path = tmp_path / f"{secret}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("assets/app.js", "TestDriverRequest")
    result = run_scanner(str(archive_path), "--json")
    assert result.returncode == 1
    assert "development_control_in_packaged_bytes" in result.stdout
    assert secret not in result.stdout
    assert secret not in result.stderr


# --------------------------------------------------------------------------
# Core's own conformance resources stay packageable
# --------------------------------------------------------------------------


def test_core_does_not_run_its_own_canonical_resources_through_this_gate() -> None:
    """The gate is for production Module Release packages.

    Core's canonical fixtures are conformance material and are *meant* to name
    the development records -- which is exactly why running Core's own resource
    tree through this scanner reports findings. Both facts are pinned here so
    neither can be changed by accident: the detector really does fire on real
    conformance content, and nothing in Core's packaging applies it to Core.
    """
    canonical = REPO_ROOT / "contracts" / "host" / "v1"
    members = tuple(
        pub.PackageMember(
            path=path.relative_to(canonical).as_posix(), data=path.read_bytes()
        )
        for path in sorted(canonical.rglob("*.json"))
    )
    findings = pub.scan_release_package(
        pub.PackageInventory(
            members=members,
            source_symbols=(),
            source_graph_exhaustive=True,
            exhaustive=True,
        )
    )
    assert "development_control_in_packaged_bytes" in codes(findings)

    manifest = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "scan-release-package" not in manifest
    force_include = "contracts/host/v1/fixtures"
    assert force_include in manifest, "the canonical fixtures must still be packaged"
