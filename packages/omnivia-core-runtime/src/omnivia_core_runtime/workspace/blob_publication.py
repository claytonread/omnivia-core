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
from typing import Final

from omnivia_core_runtime.workspace.filesystem import fsync_directory

#: The one internal blob address domain: SHA-256, lowercase hex, no other algorithm and
#: no other letter case, because both sides of a comparison must recompute it exactly.
DIGEST_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")

_CHUNK: Final = 1024 * 1024


class BlobPublicationRefused(RuntimeError):
    """Bytes cannot be published, or what is already published is not those bytes."""


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


def _verify(path: Path, content: bytes) -> None:
    """Read the published object back and compare it with `content` byte for byte.

    Opened with `O_NOFOLLOW` and re-checked through the descriptor, so a symlink or a
    non-regular file standing where the object should be is refused rather than
    followed to whatever it points at.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BlobPublicationRefused(
            "the content-addressed blob cannot be read safely"
        ) from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BlobPublicationRefused(
                "the content-addressed blob is not one regular file"
            )
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
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=False)
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
