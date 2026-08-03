"""Round-7 production-package evidence and archive-directory regressions."""

from __future__ import annotations

import io
import json
import os
import stat
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from omnivia_core.host_contract.v1 import publication as pub

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER = REPO_ROOT / "scripts" / "scan-release-package.py"


def finding_codes(findings: tuple[pub.PackageFinding, ...]) -> set[str]:
    return {finding.code for finding in findings}


def run_scanner(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(SCANNER), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )


def write_clean_package(root: Path) -> Path:
    package = root / "package"
    package.mkdir()
    (package / "index.html").write_bytes(b"<!doctype html>")
    return package


def write_source_graph(root: Path, symbols: str = "") -> Path:
    graph = root / "source-symbols.txt"
    graph.write_text(symbols, encoding="utf-8")
    return graph


def nested_zip(*entries: tuple[zipfile.ZipInfo, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for info, data in entries:
            archive.writestr(info, data)
    return payload.getvalue()


def zip_directory(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (mode << 16) | 0x10
    return info


def scan_nested_zip(data: bytes) -> tuple[pub.PackageFinding, ...]:
    return pub.scan_release_package(
        pub.PackageInventory(
            members=(pub.PackageMember(path="nested.zip", data=data),),
            source_symbols=(),
            source_graph_exhaustive=True,
            exhaustive=True,
        )
    )


def tar_directory(name: str, data: bytes = b"") -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return payload.getvalue()


def scan_nested_tar(data: bytes) -> tuple[pub.PackageFinding, ...]:
    return pub.scan_release_package(
        pub.PackageInventory(
            members=(pub.PackageMember(path="nested.archive", data=data),),
            source_symbols=(),
            source_graph_exhaustive=True,
            exhaustive=True,
        )
    )


def patch_zip_directory_metadata(
    data: bytes, *, hidden_size: bool = False, encrypted: bool = False
) -> bytes:
    patched = bytearray(data)
    local = patched.index(b"PK\x03\x04")
    central = patched.index(b"PK\x01\x02")
    if hidden_size:
        struct.pack_into("<I", patched, central + 24, 0)
    if encrypted:
        local_flags = struct.unpack_from("<H", patched, local + 6)[0] | 0x1
        central_flags = struct.unpack_from("<H", patched, central + 8)[0] | 0x1
        struct.pack_into("<H", patched, local + 6, local_flags)
        struct.pack_into("<H", patched, central + 8, central_flags)
    return bytes(patched)


def test_omitted_source_graph_evidence_fails_closed() -> None:
    findings = pub.scan_release_package(
        pub.PackageInventory(
            members=(pub.PackageMember(path="index.html", data=b"<!doctype html>"),),
            exhaustive=True,
        )
    )

    assert finding_codes(findings) == {"incomplete_inventory"}
    assert findings[0].detail == "source graph evidence is incomplete"


def test_explicit_exhaustive_empty_source_graph_is_valid_evidence() -> None:
    inventory = pub.PackageInventory(
        source_symbols=(),
        source_graph_exhaustive=True,
        exhaustive=True,
    )

    assert pub.scan_release_package(inventory) == ()


def test_cli_omitted_source_graph_fails_in_text_and_json_modes(tmp_path: Path) -> None:
    package = write_clean_package(tmp_path)

    text_result = run_scanner(str(package))
    json_result = run_scanner(str(package), "--json")

    assert text_result.returncode == 1
    assert "incomplete_inventory" in text_result.stdout
    assert "source graph evidence is incomplete" in text_result.stdout
    assert json_result.returncode == 1
    report = json.loads(json_result.stdout)
    assert [finding["code"] for finding in report["findings"]] == [
        "incomplete_inventory"
    ]


def test_cli_accepts_an_explicit_exhaustive_empty_source_graph(tmp_path: Path) -> None:
    package = write_clean_package(tmp_path)
    source_graph = write_source_graph(tmp_path)

    result = run_scanner(str(package), "--source-symbols", str(source_graph))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no findings" in result.stdout


def test_cli_rejects_an_escaping_top_level_zip_directory(tmp_path: Path) -> None:
    archive_path = tmp_path / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape/", b"")
    source_graph = write_source_graph(tmp_path)

    result = run_scanner(
        str(archive_path), "--source-symbols", str(source_graph), "--json"
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [finding["code"] for finding in report["findings"]] == ["escaping_member"]
    assert report["findings"][0]["member"] == "member[0]"
    assert "../escape" not in result.stdout
    assert result.stderr == ""


def test_cli_rejects_a_top_level_symlink_zip_directory(tmp_path: Path) -> None:
    archive_path = tmp_path / "release.zip"
    secret = "ghp_" + "l" * 36
    with zipfile.ZipFile(archive_path, "w") as archive:
        info = zipfile.ZipInfo(f"{secret}/")
        info.create_system = 3
        info.external_attr = (0o120777 << 16) | 0x10
        archive.writestr(info, b"")
    source_graph = write_source_graph(tmp_path)

    result = run_scanner(
        str(archive_path), "--source-symbols", str(source_graph), "--json"
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [finding["code"] for finding in report["findings"]] == ["symlinked_member"]
    assert report["findings"][0]["member"] == "member[0]"
    assert secret not in result.stdout
    assert result.stderr == ""


def test_cli_rejects_a_nonempty_top_level_zip_directory(tmp_path: Path) -> None:
    archive_path = tmp_path / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        info = zip_directory("payload/", stat.S_IFDIR | 0o755)
        archive.writestr(info, b"TestDriverRequest")
    source_graph = write_source_graph(tmp_path)

    result = run_scanner(
        str(archive_path), "--source-symbols", str(source_graph), "--json"
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [finding["code"] for finding in report["findings"]] == [
        "malformed_member"
    ]
    assert report["findings"][0]["member"] == "member[0]"


def test_nested_zip_accepts_an_empty_ordinary_directory() -> None:
    info = zip_directory("normal/", stat.S_IFDIR | 0o755)
    assert scan_nested_zip(nested_zip((info, b""))) == ()


@pytest.mark.parametrize(
    ("mode", "data", "expected"),
    [
        (stat.S_IFLNK | 0o777, b"", "symlinked_member"),
        (stat.S_IFCHR | 0o600, b"", "malformed_member"),
        (stat.S_IFDIR | 0o755, b"TestDriverRequest", "malformed_member"),
    ],
)
def test_nested_zip_rejects_nonordinary_directory_metadata(
    mode: int, data: bytes, expected: str
) -> None:
    info = zip_directory("entry/", mode)
    findings = scan_nested_zip(nested_zip((info, data)))
    assert [finding.code for finding in findings] == [expected]
    assert findings[0].member == "member[0]!member[0]"


def test_nested_zip_rejects_duplicate_and_noncanonical_directories() -> None:
    first = zip_directory("same/", stat.S_IFDIR | 0o755)
    duplicate = zip_directory("same/", stat.S_IFDIR | 0o755)
    escaping = zip_directory("../escape/", stat.S_IFDIR | 0o755)
    with pytest.warns(UserWarning, match="Duplicate name"):
        findings = scan_nested_zip(
            nested_zip((first, b""), (duplicate, b""), (escaping, b""))
        )
    assert [finding.code for finding in findings] == [
        "duplicate_member",
        "escaping_member",
    ]


def test_cli_rejects_a_nested_symlink_zip_directory(tmp_path: Path) -> None:
    inner = nested_zip(
        (zip_directory("linked/", stat.S_IFLNK | 0o777), b"")
    )
    archive_path = tmp_path / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("nested.zip", inner)
    source_graph = write_source_graph(tmp_path)

    result = run_scanner(
        str(archive_path), "--source-symbols", str(source_graph), "--json"
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [finding["code"] for finding in report["findings"]] == [
        "symlinked_member"
    ]
    assert report["findings"][0]["member"] == "member[0]!member[0]"


def test_nested_tar_accepts_an_empty_ordinary_directory() -> None:
    assert scan_nested_tar(tar_directory("normal/")) == ()


def test_nested_tar_rejects_a_nonempty_directory() -> None:
    findings = scan_nested_tar(tar_directory("payload/", b"TestDriverRequest"))
    assert [finding.code for finding in findings] == ["malformed_member"]
    assert findings[0].member == "member[0]!member[0]"


def test_cli_rejects_a_nonempty_top_level_tar_directory(tmp_path: Path) -> None:
    archive_path = tmp_path / "release.tar"
    archive_path.write_bytes(tar_directory("payload/", b"TestDriverRequest"))
    source_graph = write_source_graph(tmp_path)

    result = run_scanner(
        str(archive_path), "--source-symbols", str(source_graph), "--json"
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [finding["code"] for finding in report["findings"]] == [
        "malformed_member"
    ]
    assert report["findings"][0]["member"] == "member[0]"


def test_cli_rejects_a_top_level_zip_directory_with_hidden_size(
    tmp_path: Path,
) -> None:
    info = zip_directory("payload/", stat.S_IFDIR | 0o755)
    archive_path = tmp_path / "release.zip"
    archive_path.write_bytes(
        patch_zip_directory_metadata(
            nested_zip((info, b"TestDriverRequest")), hidden_size=True
        )
    )
    source_graph = write_source_graph(tmp_path)

    result = run_scanner(
        str(archive_path), "--source-symbols", str(source_graph), "--json"
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [finding["code"] for finding in report["findings"]] == [
        "malformed_member"
    ]
    assert report["findings"][0]["member"] == "member[0]"
    assert "TestDriverRequest" not in result.stdout


def test_cli_rejects_an_encrypted_top_level_zip_directory(tmp_path: Path) -> None:
    info = zip_directory("private/", stat.S_IFDIR | 0o755)
    archive_path = tmp_path / "release.zip"
    archive_path.write_bytes(
        patch_zip_directory_metadata(nested_zip((info, b"")), encrypted=True)
    )
    source_graph = write_source_graph(tmp_path)

    result = run_scanner(
        str(archive_path), "--source-symbols", str(source_graph), "--json"
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [finding["code"] for finding in report["findings"]] == [
        "unreadable_member"
    ]
    assert report["findings"][0]["member"] == "member[0]"


def test_cli_rejects_a_zip_directory_with_repeated_trailing_separators(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(zip_directory("dir//", stat.S_IFDIR | 0o755), b"")
    source_graph = write_source_graph(tmp_path)

    result = run_scanner(
        str(archive_path), "--source-symbols", str(source_graph), "--json"
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [finding["code"] for finding in report["findings"]] == [
        "escaping_member"
    ]
    assert report["findings"][0]["member"] == "member[0]"


def test_nested_tar_rejects_directory_alias_before_name_normalization() -> None:
    findings = scan_nested_tar(tar_directory("dir//"))
    assert [finding.code for finding in findings] == ["escaping_member"]
    assert findings[0].member == "member[0]!member[0]"


def compressed_tar_directory(
    mode: str,
    name: str,
    *,
    before: bytes = b"",
    after: bytes = b"",
) -> bytes:
    archive = io.BytesIO()

    def write_members(tar: tarfile.TarFile) -> None:
        if before:
            pad = tarfile.TarInfo("before.bin")
            pad.size = len(before)
            tar.addfile(pad, io.BytesIO(before))
        directory = tarfile.TarInfo(name)
        directory.type = tarfile.DIRTYPE
        directory.size = 0
        tar.addfile(directory)
        if after:
            trailer = tarfile.TarInfo("after.bin")
            trailer.size = len(after)
            tar.addfile(trailer, io.BytesIO(after))

    if mode == "w:gz":
        with tarfile.open(
            fileobj=archive, mode="w:gz", format=tarfile.USTAR_FORMAT
        ) as tar:
            write_members(tar)
    elif mode == "w:bz2":
        with tarfile.open(
            fileobj=archive, mode="w:bz2", format=tarfile.USTAR_FORMAT
        ) as tar:
            write_members(tar)
    elif mode == "w:xz":
        with tarfile.open(
            fileobj=archive, mode="w:xz", format=tarfile.USTAR_FORMAT
        ) as tar:
            write_members(tar)
    else:
        raise AssertionError("unsupported test compression mode")
    return archive.getvalue()


@pytest.mark.parametrize("mode", ["w:gz", "w:bz2", "w:xz"])
def test_nested_compressed_tar_accepts_an_ordinary_empty_directory(mode: str) -> None:
    findings = scan_nested_tar(compressed_tar_directory(mode, "payload/"))

    assert findings == ()


@pytest.mark.parametrize("mode", ["w:gz", "w:bz2", "w:xz"])
def test_nested_compressed_tar_rejects_a_raw_directory_alias_after_padding(
    mode: str,
) -> None:
    findings = scan_nested_tar(
        compressed_tar_directory(
            mode,
            "payload//",
            before=os.urandom(20_000),
            after=os.urandom(20_000),
        )
    )

    assert [finding.code for finding in findings] == ["escaping_member"]
    assert findings[0].member == "member[0]!member[1]"

def test_cli_rejects_a_tar_directory_alias_before_name_normalization(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "release.tar"
    archive_path.write_bytes(tar_directory("dir//"))
    source_graph = write_source_graph(tmp_path)

    result = run_scanner(
        str(archive_path), "--source-symbols", str(source_graph), "--json"
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [finding["code"] for finding in report["findings"]] == [
        "escaping_member"
    ]
    assert report["findings"][0]["member"] == "member[0]"
