"""Durable publication of content-addressed bytes into one workspace blob root.

Bytes and a digest, nothing else. This primitive knows no source, no provider, no
media type, no evidence identity and no database: it is the one place that makes a
`sha256:` address durable on disk, so every lane that needs that -- local capture,
connector synchronisation, and whatever else later addresses bytes by digest -- gets
the same atomic-rename-and-fsync sequence rather than a second copy of it.

Publication is idempotent because the address is the content. Re-publishing bytes that
are already there verifies the object byte for byte and returns it; it never rewrites
one. A crash can therefore leave an unreferenced object, but never an object whose
bytes are not the ones its address names.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Final

from omnivia_core_runtime.workspace.filesystem import fsync_directory

#: The one internal blob address domain: SHA-256, lowercase hex, no other algorithm and
#: no other letter case, because both sides of a comparison must recompute it exactly.
DIGEST_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")

_CHUNK: Final = 1024 * 1024
_GENERIC_READ: Final = 0x80000000
_FILE_SHARE_READ: Final = 0x00000001
_OPEN_EXISTING: Final = 3
_FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x00000400


class BlobPublicationRefused(RuntimeError):
    """Bytes cannot be published, or what is already published is not those bytes."""


def _windows_file_api() -> Any:
    """A configured ``CreateFileW`` API for opening the named object itself."""
    import ctypes

    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("Windows file API is unavailable")
    try:
        kernel32 = loader("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int32
    except Exception as failure:
        raise OSError("Windows file API is unavailable") from failure
    return kernel32


def _handle_value(handle: object) -> int | None:
    value = getattr(handle, "value", handle)
    return value if isinstance(value, int) else None


def _open_windows_blob(
    path: Path,
    *,
    api: Any | None = None,
    descriptor_from_handle: Any | None = None,
) -> int:
    """Open the path's object, not a reparse target, and transfer handle ownership."""
    import ctypes
    import msvcrt

    kernel32 = _windows_file_api() if api is None else api
    handle = kernel32.CreateFileW(
        str(path),
        _GENERIC_READ,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = _handle_value(handle)
    if value in (None, 0, ctypes.c_void_p(-1).value):
        raise OSError("blob could not be opened without following links")
    converter = (
        msvcrt.open_osfhandle  # type: ignore[attr-defined]
        if descriptor_from_handle is None
        else descriptor_from_handle
    )
    try:
        # Ownership of the native handle passes to the CRT descriptor on success.
        return int(converter(value, os.O_RDONLY | getattr(os, "O_BINARY", 0)))
    except (OSError, ValueError, OverflowError) as failure:
        kernel32.CloseHandle(handle)
        raise OSError("blob handle could not become a descriptor") from failure


def _opened_blob(path: Path) -> int:
    """Open exactly one regular, single-linked object without following reparse data."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = (
            _open_windows_blob(path)
            if os.name == "nt"
            else os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
        )
    except OSError as error:
        raise BlobPublicationRefused(
            "the content-addressed blob cannot be read safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(path)
        attributes = int(getattr(opened, "st_file_attributes", 0)) | int(
            getattr(named, "st_file_attributes", 0)
        )
        safe = (
            stat.S_ISREG(opened.st_mode)
            and stat.S_ISREG(named.st_mode)
            and opened.st_nlink == 1
            and named.st_nlink == 1
            and attributes & _FILE_ATTRIBUTE_REPARSE_POINT == 0
            and (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
            == (named.st_dev, named.st_ino, stat.S_IFMT(named.st_mode))
        )
        if not safe:
            raise BlobPublicationRefused(
                "the content-addressed blob is not one regular file"
            )
    except BlobPublicationRefused:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise BlobPublicationRefused(
            "the content-addressed blob cannot be read safely"
        ) from error
    return descriptor


def blob_path(blobs_root: Path, digest: str) -> Path:
    """The path one `sha256:` address resolves to under `blobs_root`.

    Address arithmetic and nothing else: it opens no file, proves no bytes and states
    no opinion about whether anything is there. A caller that needs the object to be
    real reads it and verifies; a caller that needs to publish one calls
    :func:`publish_blob`, which is the only thing that writes.

    The digest is checked against the one accepted address domain first, so a caller's
    value cannot reach the filesystem as a path segment without passing
    `sha256:[0-9a-f]{64}` -- which admits no separator, no `..` and no absolute path.
    """
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise BlobPublicationRefused("blob digest is outside the accepted address domain")
    return blobs_root / "sha256" / digest.removeprefix("sha256:")


def _blob_path(blobs_root: Path, digest: str, content: bytes) -> Path:
    target = blob_path(blobs_root, digest)
    if hashlib.sha256(content).hexdigest() != target.name:
        raise BlobPublicationRefused("blob digest does not match the published bytes")
    return target


def _is_real_directory_no_follow(path: Path) -> bool:
    """Whether ``path`` is an existing directory, never a link/reparse point."""
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        == 0
    )


def _verify(path: Path, content: bytes) -> None:
    """Read the published object back and compare it with `content` byte for byte.

    Opened with `O_NOFOLLOW` and re-checked through the descriptor, so a symlink or a
    non-regular file standing where the object should be is refused rather than
    followed to whatever it points at.
    """
    descriptor = _opened_blob(path)
    try:
        offset = 0
        while True:
            chunk = os.read(descriptor, _CHUNK)
            if not chunk:
                break
            # A longer object fails here on the first byte past the end, because the
            # slice is then shorter than the chunk it is compared with.
            if content[offset : offset + len(chunk)] != chunk:
                raise BlobPublicationRefused(
                    "the content-addressed blob does not verify"
                )
            offset += len(chunk)
        if offset != len(content):
            raise BlobPublicationRefused("the content-addressed blob does not verify")
    finally:
        os.close(descriptor)


def publish_blob(blobs_root: Path, digest: str, content: bytes) -> Path:
    """Publish `content` at its `sha256:` address, before anything may refer to it.

    Owner-only temporary file, fsync, atomic replace, directory fsync: the object
    appears at its address whole or not at all, and is durable once it does. The
    temporary file is removed whatever the write, the replace or the verification does,
    so a fault leaves no partial object behind under the blob root.
    """
    target = _blob_path(blobs_root, digest, content)
    directory = target.parent
    try:
        directory.mkdir(mode=0o700, parents=False)
    except FileExistsError as error:
        # Two first publications may both observe the absent fanout directory.
        # Accept the race winner's directory, but never a file, link or reparse
        # point that appeared at the name instead.
        if not _is_real_directory_no_follow(directory):
            raise BlobPublicationRefused(
                "the content-addressed blob directory is not one real directory"
            ) from error
    else:
        fsync_directory(directory.parent)
    if target.exists() or target.is_symlink():
        _verify(target, content)
        return target

    temporary = directory / f".{target.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:  # pragma: no cover - defensive operating-system failure
                    raise OSError("short write while publishing blob")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
        fsync_directory(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    _verify(target, content)
    return target


__all__ = [
    "DIGEST_PATTERN",
    "BlobPublicationRefused",
    "blob_path",
    "publish_blob",
]
