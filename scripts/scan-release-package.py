#!/usr/bin/env python3
"""Scan a production Module Release package for excluded content (DOC-004 §AA).

Development and conformance controls -- development profiles, source mounts,
fixture injection and test-driver entry points -- are compiled out of
production customer builds, and a production package carries no credential
material. Proving either needs the packaged *bytes*, not the member names: a
bundle called ``app.js`` can contain an entire test-driver API and pass any
name check untouched.

This is Core's executable contract decision gate. When invoked, it reads a real
Release Package -- a directory, a ``.zip``, or a
``.tar``/``.tar.gz``/``.tgz`` -- exhaustively into a trusted
inventory and runs ``omnivia_core.host_contract.v1.publication`` over it, which
scans every member's bytes (including members of archives inside the package)
for development controls and likely secrets, and refuses members that are
malformed, unreadable, symlinked, duplicated or escaping.

This CLI does not certify a Platform production build and its presence does not
prove that Platform production packaging invokes it. Wiring that invocation to
the customer-artifact packaging path remains the downstream G1 condition.

Fail-closed and bounded. Any finding, any unreadable input, any path that is
not a package, and any package past the scanner's size bounds exits non-zero.
There is no skip path and no partial pass. No diagnostic it prints reproduces
the bytes or names it rejected: a finding carries an opaque member location,
the detector and a byte offset.

Scope: this gate is for *production Module Release packages*. It is not applied
to Core's own distribution, whose canonical conformance schema and fixtures are
governed resources that are meant to name the development records.

Standard library only, entirely offline.

    scripts/scan-release-package.py PATH [--source-symbols FILE] [--json]

Exit codes: ``0`` clean, ``1`` findings or unusable input, ``2`` bad usage.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Prefer this checkout when the script is executed from a source tree.  This
# also avoids an older installed ``omnivia_core`` package shadowing the lane.
source_root = REPO_ROOT / "src"
if source_root.is_dir():
    sys.path.insert(0, str(source_root))

# E402: this import must follow the `sys.path` insert above for the checkout to
# win over an installed copy, so it cannot move to the top of the file.
from omnivia_core.host_contract.v1 import publication  # noqa: E402


class UnreadablePackageError(Exception):
    """Raised when the path is not a Release Package this gate can read exhaustively."""


def _read_directory(root: Path) -> tuple[publication.PackageMember, ...]:
    members: list[publication.PackageMember] = []
    unreadable = False
    try:
        paths = sorted(root.rglob("*"))
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                members.append(
                    publication.PackageMember(path=relative, data=b"", is_symlink=True)
                )
                continue
            if path.is_dir():
                continue
            if not path.is_file():
                members.append(
                    publication.PackageMember(path=relative, data=b"", is_regular=False)
                )
                continue
            if path.stat().st_size > publication.MAX_SCANNED_MEMBER_BYTES:
                unreadable = True
                break
            members.append(
                publication.PackageMember(path=relative, data=path.read_bytes())
            )
    except OSError:
        unreadable = True
    if unreadable:
        raise UnreadablePackageError("directory package could not be exhaustively read")
    return tuple(members)


def _read_zip(path: Path) -> tuple[publication.PackageMember, ...]:
    members: list[publication.PackageMember] = []
    unreadable = False
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                is_symlink = file_type == stat.S_IFLNK
                is_directory = info.is_dir()
                is_regular = file_type in (0, stat.S_IFREG) or (
                    is_directory and file_type == stat.S_IFDIR
                )
                member_path = (
                    info.filename[:-1]
                    if is_directory and info.filename.endswith("/")
                    else info.filename
                )
                if is_directory:
                    members.append(
                        publication.PackageMember(
                            path=member_path,
                            data=(
                                b""
                                if info.file_size == 0 and info.compress_size == 0
                                else b"\x00"
                            ),
                            is_symlink=is_symlink,
                            is_regular=is_regular,
                            is_directory=True,
                            is_encrypted=bool(info.flag_bits & 0x1),
                        )
                    )
                    continue
                if is_symlink:
                    members.append(
                        publication.PackageMember(
                            path=info.filename, data=b"", is_symlink=True
                        )
                    )
                    continue
                if not is_regular:
                    members.append(
                        publication.PackageMember(
                            path=info.filename, data=b"", is_regular=False
                        )
                    )
                    continue
                if info.flag_bits & 0x1:
                    # An encrypted member is classified from its header rather
                    # than by attempting the read: ``ZipFile.read`` raises for a
                    # member this gate has no key for, which would collapse the
                    # whole package into one generic unreadable-package refusal.
                    # Both outcomes fail closed, but only this one names *which*
                    # member could not be scanned, and it does so identically
                    # whichever encryption the member used.
                    members.append(
                        publication.PackageMember(
                            path=info.filename, data=b"", is_encrypted=True
                        )
                    )
                    continue
                if info.file_size > publication.MAX_SCANNED_MEMBER_BYTES:
                    unreadable = True
                    break
                members.append(
                    publication.PackageMember(
                        path=info.filename, data=archive.read(info)
                    )
                )
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, EOFError):
        unreadable = True
    if unreadable:
        raise UnreadablePackageError("ZIP package could not be exhaustively read")
    return tuple(members)


def _tar_directory_has_alias(
    archive: tarfile.TarFile, info: tarfile.TarInfo
) -> bool:
    """Inspect the raw header before ``tarfile`` normalizes directory names."""
    stream = archive.fileobj
    if stream is None:
        raise OSError("tar stream is unavailable")
    position = stream.tell()
    try:
        stream.seek(info.offset)
        block = stream.read(tarfile.BLOCKSIZE)
    finally:
        stream.seek(position)
    if len(block) != tarfile.BLOCKSIZE:
        raise OSError("tar header is incomplete")
    raw_name = block[:100].split(b"\x00", 1)[0]
    raw_prefix = block[345:500].split(b"\x00", 1)[0]
    if raw_prefix:
        raw_name = raw_prefix + b"/" + raw_name
    pax_path = info.pax_headers.get("path")
    return raw_name.endswith(b"//") or (
        type(pax_path) is str and pax_path.endswith("//")
    )


def _read_tar(path: Path) -> tuple[publication.PackageMember, ...]:
    members: list[publication.PackageMember] = []
    unreadable = False
    try:
        with tarfile.open(path) as archive:
            for info in archive.getmembers():
                if info.issym() or info.islnk():
                    members.append(
                        publication.PackageMember(
                            path=info.name, data=b"", is_symlink=True
                        )
                    )
                    continue
                if info.isdir():
                    if info.size > publication.MAX_SCANNED_MEMBER_BYTES:
                        unreadable = True
                        break
                    members.append(
                        publication.PackageMember(
                            path=(
                                f"{info.name}/"
                                if _tar_directory_has_alias(archive, info)
                                else info.name
                            ),
                            data=b"" if info.size == 0 else b"\x00",
                            is_directory=True,
                        )
                    )
                    continue
                if not info.isfile():
                    members.append(
                        publication.PackageMember(
                            path=info.name, data=b"", is_regular=False
                        )
                    )
                    continue
                if info.size > publication.MAX_SCANNED_MEMBER_BYTES:
                    unreadable = True
                    break
                handle = archive.extractfile(info)
                if handle is None:
                    unreadable = True
                    break
                members.append(
                    publication.PackageMember(path=info.name, data=handle.read())
                )
    except (tarfile.TarError, OSError, EOFError):
        unreadable = True
    if unreadable:
        raise UnreadablePackageError("tar package could not be exhaustively read")
    return tuple(members)


def read_package(path: Path) -> tuple[publication.PackageMember, ...]:
    """Read a Release Package exhaustively, or refuse to read it at all."""
    if path.is_dir():
        return _read_directory(path)
    if not path.is_file():
        raise UnreadablePackageError("package is not a directory or archive")
    if zipfile.is_zipfile(path):
        return _read_zip(path)
    if tarfile.is_tarfile(path):
        return _read_tar(path)
    raise UnreadablePackageError(
        "package is not a directory, ZIP, or tar Release Package"
    )


def read_source_symbols(path: Path) -> tuple[str, ...]:
    """Read the production source graph's exported symbols, one per line."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        text = None
    if text is None:
        raise UnreadablePackageError("source symbol inventory is unreadable")
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "package", help="the Release Package directory, .zip, .tar or .tar.gz"
    )
    parser.add_argument(
        "--source-symbols",
        type=Path,
        default=None,
        help="a text file of production source-graph symbols, one per line",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit findings as a JSON report"
    )
    arguments = parser.parse_args(argv)

    try:
        members = read_package(Path(arguments.package))
        symbols: tuple[str, ...] | None = (
            read_source_symbols(arguments.source_symbols)
            if arguments.source_symbols is not None
            else None
        )
    except UnreadablePackageError as error:
        print(f"Release Package scan FAILED: {error}", file=sys.stderr)
        return 1

    findings = publication.scan_release_package(
        publication.PackageInventory(
            members=members,
            source_symbols=symbols,
            source_graph_exhaustive=arguments.source_symbols is not None,
            exhaustive=True,
        )
    )

    if arguments.json:
        print(
            json.dumps(
                {
                    "package": "<package>",
                    "scanned": len(members),
                    "findings": [
                        {
                            "code": finding.code,
                            "member": finding.member,
                            "detector": finding.detector,
                            "offset": finding.offset,
                            "detail": finding.detail,
                        }
                        for finding in findings
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1 if findings else 0

    if not findings:
        print(f"Release Package scan: no findings across {len(members)} member(s).")
        return 0

    print(f"Release Package scan FAILED with {len(findings)} finding(s):\n")
    for finding in findings:
        location = (
            f"{finding.member}@{finding.offset}"
            if finding.offset >= 0
            else finding.member
        )
        print(f"  - {finding.code} {location or '<package>'}: {finding.detail}")
    print(
        "\nA production Module Release package carries no development or conformance"
        "\ncontrol and no credential material. Rebuild it with those excluded."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
