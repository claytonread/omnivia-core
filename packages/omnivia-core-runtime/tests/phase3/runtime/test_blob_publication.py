"""Qualification of the provider-neutral content-addressed blob publication primitive.

The primitive knows a blob root, a `sha256:` address and bytes. It is what makes those
bytes durable at that address, and it is the only thing that does: local capture and
connector synchronisation both reach it rather than carrying a copy of the sequence.

So what is qualified here is exactly that much. Publication is atomic and idempotent;
an object that is already there is verified byte for byte rather than rewritten; a
symlink or a directory standing at the address is refused rather than followed; a
digest outside the one internal address domain is refused before any path is touched;
and a fault in the write, the replace or the verification leaves no temporary file
behind under the blob root.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import ModuleType

import pytest
from omnivia_core_runtime.workspace import blob_publication
from omnivia_core_runtime.workspace.blob_publication import (
    BlobPublicationRefused,
    publish_blob,
)
from omnivia_core_runtime.workspace.filesystem import fsync_directory


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


CONTENT = b"provider-neutral bytes\n"
DIGEST = _digest(CONTENT)
OTHER_CONTENT = b"different bytes"
OTHER_DIGEST = _digest(OTHER_CONTENT)


def blobs_root(tmp_path: Path) -> Path:
    root = tmp_path / "blobs"
    root.mkdir()
    return root


def address(root: Path, digest: str = DIGEST) -> Path:
    return root / "sha256" / digest.removeprefix("sha256:")


def temporaries(root: Path) -> list[Path]:
    return sorted(path for path in (root / "sha256").iterdir() if path.suffix == ".tmp")


def test_publishes_bytes_at_their_address_owner_only(tmp_path: Path) -> None:
    """The object appears at its address, readable only by its owner, and nothing else."""
    root = blobs_root(tmp_path)
    published = publish_blob(root, DIGEST, CONTENT)

    assert published == address(root)
    assert published.read_bytes() == CONTENT
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "sha256").stat().st_mode) == 0o700
    assert temporaries(root) == []
    assert list((root / "sha256").iterdir()) == [published]


def test_republishing_the_same_bytes_verifies_rather_than_rewrites(
    tmp_path: Path,
) -> None:
    """Idempotent because the address is the content: the object is left exactly as is."""
    root = blobs_root(tmp_path)
    first = publish_blob(root, DIGEST, CONTENT)
    before = first.stat()

    again = publish_blob(root, DIGEST, CONTENT)

    assert again == first
    after = again.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert temporaries(root) == []

    # A second address in the same root is a second object, not a replacement.
    other = publish_blob(root, OTHER_DIGEST, OTHER_CONTENT)
    assert other != first
    assert first.read_bytes() == CONTENT


def test_concurrent_first_publications_share_the_fanout_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first-directory race is idempotent, while the winning object is verified."""
    root = blobs_root(tmp_path)
    directory = root / "sha256"
    rendezvous = Barrier(2)
    real_mkdir = Path.mkdir

    def racing_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path == directory:
            rendezvous.wait(timeout=5)
        real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    with ThreadPoolExecutor(max_workers=2) as executor:
        published = tuple(
            executor.map(lambda _attempt: publish_blob(root, DIGEST, CONTENT), range(2))
        )

    assert published == (address(root), address(root))
    assert address(root).read_bytes() == CONTENT
    assert temporaries(root) == []


def test_refuses_an_object_whose_bytes_are_not_the_ones_published(
    tmp_path: Path,
) -> None:
    """Whatever is already at the address must be the content, byte for byte."""
    root = blobs_root(tmp_path)
    publish_blob(root, DIGEST, CONTENT)
    target = address(root)

    for substituted in (b"", b"other bytes entirely", CONTENT[:-1], CONTENT + b"!"):
        target.write_bytes(substituted)
        with pytest.raises(BlobPublicationRefused, match="does not verify"):
            publish_blob(root, DIGEST, CONTENT)

    # And the refusal is about the bytes, not about the length alone: same length,
    # different content.
    target.write_bytes(bytes(len(CONTENT)))
    with pytest.raises(BlobPublicationRefused, match="does not verify"):
        publish_blob(root, DIGEST, CONTENT)


def test_refuses_a_symlink_or_a_directory_standing_at_the_address(
    tmp_path: Path,
) -> None:
    """Substitution is refused rather than followed to whatever it points at."""
    root = blobs_root(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(CONTENT)
    (root / "sha256").mkdir()
    target = address(root)
    target.symlink_to(elsewhere)

    # The bytes behind the link are the right bytes; the link is still refused.
    with pytest.raises(BlobPublicationRefused, match="cannot be read safely"):
        publish_blob(root, DIGEST, CONTENT)
    assert target.is_symlink()
    assert elsewhere.read_bytes() == CONTENT

    target.unlink()
    target.mkdir()
    with pytest.raises(BlobPublicationRefused, match="not one regular file"):
        publish_blob(root, DIGEST, CONTENT)


def test_refuses_a_hard_link_standing_at_the_address(tmp_path: Path) -> None:
    """One address must own one inode; another name can otherwise mutate its bytes."""
    root = blobs_root(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(CONTENT)
    (root / "sha256").mkdir()
    target = address(root)
    os.link(elsewhere, target)

    with pytest.raises(BlobPublicationRefused, match="not one regular file"):
        publish_blob(root, DIGEST, CONTENT)

    assert target.stat().st_nlink == 2
    assert elsewhere.read_bytes() == CONTENT


def test_refuses_a_digest_outside_the_internal_address_domain(tmp_path: Path) -> None:
    """One algorithm, one length, one letter case -- checked before any path is built."""
    root = blobs_root(tmp_path)
    for digest in (
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha512:" + "a" * 64,
        "a" * 64,
        "sha256:",
        "../escape",
        "",
    ):
        with pytest.raises(BlobPublicationRefused, match="accepted address domain"):
            publish_blob(root, digest, CONTENT)
    assert not (root / "sha256").exists()


def test_refuses_a_well_shaped_digest_that_does_not_match_the_content(
    tmp_path: Path,
) -> None:
    """Content-addressed publication means the digest is checked against the bytes,
    not just the address domain -- and checked before the blob directory exists."""
    root = blobs_root(tmp_path)

    with pytest.raises(BlobPublicationRefused, match="does not match"):
        publish_blob(root, OTHER_DIGEST, CONTENT)

    assert not (root / "sha256").exists()


def test_a_fault_leaves_no_temporary_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write, replace and verify faults all clean up, and none of them half-publish."""
    root = blobs_root(tmp_path)
    real_write = os.write
    real_replace = os.replace

    def failing_write(descriptor: int, data: object) -> int:
        raise OSError("injected write fault")

    monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(OSError, match="injected write fault"):
        publish_blob(root, DIGEST, CONTENT)
    monkeypatch.setattr(os, "write", real_write)
    assert temporaries(root) == []
    assert not address(root).exists()

    def failing_replace(source: object, destination: object) -> None:
        raise OSError("injected replace fault")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="injected replace fault"):
        publish_blob(root, DIGEST, CONTENT)
    monkeypatch.setattr(os, "replace", real_replace)
    assert temporaries(root) == []
    assert not address(root).exists()

    # A verification fault after a successful replace: the temporary is gone because it
    # became the object, and the refusal still reaches the caller.
    def replace_with_other_bytes(source: object, destination: object) -> None:
        real_replace(source, destination)
        Path(str(destination)).write_bytes(b"substituted after the replace")

    monkeypatch.setattr(os, "replace", replace_with_other_bytes)
    with pytest.raises(BlobPublicationRefused, match="does not verify"):
        publish_blob(root, DIGEST, CONTENT)
    assert temporaries(root) == []

    # The root is usable afterwards: nothing left a lock, a partial object or a stale
    # temporary in the way.
    monkeypatch.setattr(os, "replace", real_replace)
    address(root).unlink()
    assert publish_blob(root, DIGEST, CONTENT).read_bytes() == CONTENT


def test_the_primitive_writes_no_database_and_captures_no_source() -> None:
    """Bytes and a digest: no connection, no source path, no media type, no SQL."""
    assert blob_publication.__all__ == [
        "DIGEST_PATTERN",
        "BlobPublicationRefused",
        "blob_path",
        "publish_blob",
    ]
    assert list(inspect.signature(publish_blob).parameters) == [
        "blobs_root",
        "digest",
        "content",
    ]
    # `blob_path` is address arithmetic and is held to the same boundary: a root, a
    # digest, and nothing that could carry a connection, a source or a media type. It
    # opens nothing, which is why a reader that needs the bytes still has to read and
    # verify them itself.
    assert list(inspect.signature(blob_publication.blob_path).parameters) == [
        "blobs_root",
        "digest",
    ]

    # The only things it imports are the standard library's hashing and filesystem
    # machinery and the one workspace helper that makes a directory entry durable.
    modules = {
        name
        for name, value in vars(blob_publication).items()
        if isinstance(value, ModuleType) and not name.startswith("__")
    }
    assert modules == {"hashlib", "os", "re", "stat", "uuid"}
    assert blob_publication.fsync_directory is fsync_directory

    source = Path(blob_publication.__file__).read_text(encoding="utf-8")
    for absent in ("sqlite", "INSERT ", "SELECT ", "execute("):
        assert absent not in source, absent
