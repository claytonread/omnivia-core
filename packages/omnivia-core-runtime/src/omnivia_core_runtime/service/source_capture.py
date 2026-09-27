"""Service-owned capture of one local file as immutable workspace evidence.

This is a maintenance path, not an application operation.  It runs only while a
``ServiceRunner`` owns the workspace and therefore uses the same lifetime lock,
exclusive connection, lease, mutation guard and fencing transaction as the live
service.  The source path is input to this one process only: it is never persisted,
returned, logged or accepted over the frozen application catalogue.

Publishing the bytes is not part of that boundary and no longer lives here: it is
`workspace.blob_publication`, which knows only a blob root, a digest and bytes.
`publish_blob` is re-exported for the callers that already reach it through this
module, but there is one implementation of it and this is not it: this module's
`publish_blob` delegates to that implementation and translates its
`BlobPublicationRefused` into `SourceCaptureRefused`, so callers that already catch
`SourceCaptureRefused` through this module keep catching publication failures reached
through this facade.

`read_checkout_file` is a different, smaller thing that lives here because it is
service-owned and reads the same way: one file of an explicitly trusted checkout, by an
Engineering Memory repository path, verified against a digest the caller already holds.
It opens no workspace and writes nothing, and it is only the read step of capturing a
working tree: enumerating a checkout, recording a snapshot and publishing bytes are
other steps.
"""

from __future__ import annotations

import hashlib
import os
import re
import select
import stat
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Final

from omnivia_core.contracts.v1 import is_content_checksum, to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.storage import repository_identity
from omnivia_core_runtime.storage.decisions import canonical_document
from omnivia_core_runtime.storage.engineering_source import valid_path
from omnivia_core_runtime.workspace.blob_publication import BlobPublicationRefused
from omnivia_core_runtime.workspace.blob_publication import (
    publish_blob as _publish_blob,
)

SOURCE_CAPTURE_FORMAT: Final = "omnivia.source-capture-result.v1"
SOURCE_KIND: Final = "document"
MAX_SOURCE_BYTES: Final = 16 * 1024 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")


class SourceCaptureRefused(RuntimeError):
    """The requested source cannot be captured without weakening the boundary.

    Not a subclass of `BlobPublicationRefused`: a base-class refusal raised by the
    provider-neutral primitive is not an instance of this subclass, so that
    inheritance would not actually let a caller catching this also catch publication
    failures. Instead this module's `publish_blob` facade catches
    `BlobPublicationRefused` itself and raises this in its place.
    """


def publish_blob(blobs_root: Path, digest: str, content: bytes) -> Path:
    """Legacy facade over the one provider-neutral publication implementation.

    Translates `BlobPublicationRefused` into `SourceCaptureRefused` so callers that
    reach publication through this module keep catching `SourceCaptureRefused`.
    """
    try:
        return _publish_blob(blobs_root, digest, content)
    except BlobPublicationRefused as error:
        raise SourceCaptureRefused(str(error)) from error


@dataclass(frozen=True, slots=True)
class SourceCaptureResult:
    """The redacted, versioned result of one source-capture attempt."""

    status: str
    workspace_id: str | None
    source_id: str
    evidence_id: str | None = None
    content_digest: str | None = None
    content_length_bytes: int | None = None
    media_type: str | None = None
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in {"captured", "already_captured"}

    def to_dict(self) -> dict[str, object]:
        return {
            "format": SOURCE_CAPTURE_FORMAT,
            "status": self.status,
            "workspace_id": self.workspace_id,
            "source": {"kind": SOURCE_KIND, "source_id": self.source_id},
            "evidence_id": self.evidence_id,
            "content_digest": self.content_digest,
            "content_length_bytes": self.content_length_bytes,
            "media_type": self.media_type,
            "reason": self.reason,
        }


def _validate_request(source_id: str, media_type: str) -> None:
    if _IDENTIFIER.fullmatch(source_id) is None:
        raise SourceCaptureRefused(
            "source id is outside the accepted identifier domain"
        )
    if len(media_type) > 255 or _MEDIA_TYPE.fullmatch(media_type) is None:
        raise SourceCaptureRefused("media type is outside the accepted domain")


def _read_source(path: Path) -> bytes:
    """Read one stable, regular, bounded file without following a symlink."""
    try:
        before = path.lstat()
    except OSError as error:
        raise SourceCaptureRefused("source file is not available") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise SourceCaptureRefused("source must be one regular file")
    if before.st_size > MAX_SOURCE_BYTES:
        raise SourceCaptureRefused("source exceeds the capture size limit")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SourceCaptureRefused("source file cannot be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SourceCaptureRefused("source must be one regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SourceCaptureRefused("source changed while capture began")
        chunks: list[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    if len(content) > MAX_SOURCE_BYTES:
        raise SourceCaptureRefused("source exceeds the capture size limit")
    if (
        (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or len(content) != after.st_size
    ):
        raise SourceCaptureRefused("source changed while it was being captured")
    return content


def _existing_capture(
    runner: ServiceRunner,
    *,
    source_id: str,
    digest: str,
    length: int,
    media_type: str,
) -> SourceCaptureResult | None:
    assert runner.connection is not None and runner.workspace_id is not None
    rows = runner.connection.execute(
        "SELECT evidence_id, blob_content_digest, media_type "
        "FROM omnivia_evidence_artifacts "
        "WHERE workspace_id = ? AND source_kind = ? AND source_native_id = ? "
        "AND source_locator IS NULL AND source_retrieved_at_us IS NULL "
        "ORDER BY evidence_id ASC",
        (runner.workspace_id, SOURCE_KIND, source_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise SourceCaptureRefused("source identity is not unique in this workspace")
    evidence_id, stored_digest, stored_media_type = map(str, rows[0])
    if stored_digest != digest or stored_media_type != media_type:
        raise SourceCaptureRefused("source identity already names different evidence")
    row = runner.connection.execute(
        "SELECT content_length_bytes FROM omnivia_blob_objects "
        "WHERE workspace_id = ? AND content_digest = ?",
        (runner.workspace_id, digest),
    ).fetchone()
    if row is None or int(row[0]) != length:
        raise SourceCaptureRefused("the existing source has no matching blob identity")
    return SourceCaptureResult(
        status="already_captured",
        workspace_id=runner.workspace_id,
        source_id=source_id,
        evidence_id=evidence_id,
        content_digest=digest,
        content_length_bytes=length,
        media_type=media_type,
        reason="the exact source identity and content are already captured",
    )


def _ensure_blob(runner: ServiceRunner, digest: str, length: int, now_us: int) -> None:
    """Record one published blob and a verified integrity event; call inside a fence."""
    assert runner.connection is not None and runner.workspace_id is not None
    nonce = uuid.uuid4().hex
    integrity_event_id = f"bie-local-{nonce}"
    blob = runner.connection.execute(
        "SELECT content_length_bytes FROM omnivia_blob_objects "
        "WHERE workspace_id = ? AND content_digest = ?",
        (runner.workspace_id, digest),
    ).fetchone()
    if blob is None:
        runner.connection.execute(
            "INSERT INTO omnivia_blob_objects "
            "(workspace_id, content_digest, content_length_bytes, created_at_us, "
            "verified_at_us) VALUES (?, ?, ?, ?, ?)",
            (runner.workspace_id, digest, length, now_us, now_us),
        )
    elif int(blob[0]) != length:
        raise SourceCaptureRefused("the blob identity has a conflicting length")

    integrity_sequence = int(
        runner.connection.execute(
            "SELECT COALESCE(MAX(integrity_sequence), 0) + 1 "
            "FROM omnivia_blob_integrity_events "
            "WHERE workspace_id = ? AND content_digest = ?",
            (runner.workspace_id, digest),
        ).fetchone()[0]
    )
    runner.connection.execute(
        "INSERT INTO omnivia_blob_integrity_events "
        "(integrity_event_id, workspace_id, content_digest, integrity_sequence, "
        "outcome, observed_digest, observed_length_bytes, expected_length_bytes, "
        "inventory_id, checked_at_us) VALUES (?, ?, ?, ?, 'verified', ?, ?, ?, "
        "NULL, ?)",
        (
            integrity_event_id,
            runner.workspace_id,
            digest,
            integrity_sequence,
            digest,
            length,
            length,
            now_us,
        ),
    )


def _write_capture(
    runner: ServiceRunner,
    *,
    source_id: str,
    digest: str,
    length: int,
    media_type: str,
    capture: str = "local_file",
) -> SourceCaptureResult:
    assert runner.connection is not None
    assert runner.identity is not None
    assert runner.workspace_id is not None
    assert runner.generation is not None

    nonce = uuid.uuid4().hex
    staged_source_ref = f"stg-local-{nonce}"
    evidence_id = f"evd-local-{nonce}"
    provenance_event_id = f"prv-local-{nonce}"
    metadata = to_canonical_json({"capture": capture, "source_id": source_id})
    if len(metadata) > 8192:
        raise SourceCaptureRefused("source metadata exceeds the accepted bound")
    metadata_digest = f"sha256:{hashlib.sha256(metadata.encode()).hexdigest()}"
    now_us = time.time_ns() // 1000
    _ensure_blob(runner, digest, length, now_us)
    runner.connection.execute(
        "INSERT INTO omnivia_staged_sources "
        "(staged_source_ref, workspace_id, source_kind, declared_checksum, "
        "content_length_bytes, media_type, source_version, computed_checksum, "
        "original_metadata_json, original_metadata_digest, staging_outcome, "
        "blob_workspace_id, blob_content_digest, recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'verified', ?, ?, ?)",
        (
            staged_source_ref,
            runner.workspace_id,
            SOURCE_KIND,
            digest,
            length,
            media_type,
            digest,
            metadata,
            metadata_digest,
            runner.workspace_id,
            digest,
            now_us,
        ),
    )
    runner.connection.execute(
        "INSERT INTO omnivia_evidence_artifacts "
        "(evidence_id, workspace_id, source_kind, source_native_id, "
        "source_locator, source_retrieved_at_us, event_at_us, observed_at_us, "
        "ingested_at_us, recorded_at_us, content_checksum, blob_content_digest, "
        "media_type, original_metadata_json, original_metadata_digest, "
        "sensitivity, parser_status, ingestion_status, staged_source_ref, "
        "import_run_id) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, "
        "?, ?, ?, 'private', 'not_parsed', 'ingested', ?, NULL)",
        (
            evidence_id,
            runner.workspace_id,
            SOURCE_KIND,
            source_id,
            now_us,
            now_us,
            digest,
            digest,
            media_type,
            metadata,
            metadata_digest,
            staged_source_ref,
        ),
    )
    runner.connection.execute(
        "INSERT INTO omnivia_evidence_provenance_events "
        "(provenance_event_id, evidence_id, workspace_id, provenance_sequence, "
        "actor_id, actor_kind, action, occurred_at_us, reason_code, reason_comment, "
        "parser_status, ingestion_status, tombstoned_observation, source_kind, "
        "source_native_id, audit_ref) VALUES (?, ?, ?, 1, 'core-service', "
        "'service', 'captured', ?, NULL, NULL, 'not_parsed', 'ingested', 0, ?, ?, "
        "NULL)",
        (
            provenance_event_id,
            evidence_id,
            runner.workspace_id,
            now_us,
            SOURCE_KIND,
            source_id,
        ),
    )

    return SourceCaptureResult(
        status="captured",
        workspace_id=runner.workspace_id,
        source_id=source_id,
        evidence_id=evidence_id,
        content_digest=digest,
        content_length_bytes=length,
        media_type=media_type,
        reason="source captured as immutable workspace evidence",
    )


def _append_capture(
    runner: ServiceRunner,
    *,
    source_id: str,
    digest: str,
    length: int,
    media_type: str,
) -> SourceCaptureResult:
    assert runner.connection is not None
    assert runner.identity is not None
    assert runner.workspace_id is not None
    assert runner.generation is not None
    with fenced_transaction(
        runner.connection,
        runner.identity,
        workspace_id=runner.workspace_id,
        fencing_generation=runner.generation,
    ):
        return _write_capture(
            runner,
            source_id=source_id,
            digest=digest,
            length=length,
            media_type=media_type,
        )


def capture_local_source(
    *,
    workspace_root: Path,
    installation_root: Path,
    source_path: Path,
    source_id: str,
    media_type: str,
    core_version: str,
) -> SourceCaptureResult:
    """Capture one local source while holding the workspace's full write authority."""
    _validate_request(source_id, media_type)
    content = _read_source(source_path)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=workspace_root,
            installation_root=installation_root,
            core_version=core_version,
            endpoint=None,
        )
    )
    report = runner.start()
    try:
        if not report.ready:
            raise SourceCaptureRefused(
                report.reason or "workspace ownership was refused"
            )
        existing = _existing_capture(
            runner,
            source_id=source_id,
            digest=digest,
            length=len(content),
            media_type=media_type,
        )
        publish_blob(runner.layout.blobs_path, digest, content)
        if existing is not None:
            return existing
        return _append_capture(
            runner,
            source_id=source_id,
            digest=digest,
            length=len(content),
            media_type=media_type,
        )
    finally:
        runner.stop()


#: Whether a checkout can be walked by descriptor at all. Asked of the platform rather
#: than assumed from ``os.name``: without ``dir_fd``, ``O_NOFOLLOW`` and ``O_DIRECTORY``
#: the only walk left is by pathname, which is the race this read exists to refuse, so
#: such a host is refused rather than given a weaker read.
_NO_FOLLOW_WALK: Final = (
    {os.open, os.stat} <= os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
#: ``O_NONBLOCK`` so a FIFO raced into place cannot hold the open. It is refused by kind
#: once open, and changes nothing about reading a regular file.
_FILE_FLAGS: Final = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)


def _open_component(name: bytes, flags: int, directory: int | None) -> int | None:
    """Open one component relative to a held directory, or ``None``.

    Every call site passes one component or the fixed filesystem root anchor, so
    ancestor symlinks are refused. OS errors are dropped because they may quote a
    local path; the caller raises a fixed-text refusal afterwards.
    """
    try:
        return os.open(name, flags, dir_fd=directory)
    except (OSError, ValueError):
        return None


def _directory_chain_is_real_by_descriptor(path: Path) -> bool:
    """Whether every component of `path`, root to leaf, opens as a real, non-symlinked
    directory when each is opened relative to the descriptor of the one before it.

    Never by a composed path: a later component's open can never be fooled by a rename
    of an earlier one's name, and `O_NOFOLLOW` on each single-component open refuses a
    symlink at that exact hop, ancestor or leaf alike.
    """
    directory: int | None = None
    held: list[int] = []
    try:
        for part in path.parts:
            descriptor = _open_component(os.fsencode(part), _DIRECTORY_FLAGS, directory)
            if descriptor is None:
                return False
            held.append(descriptor)
            directory = descriptor
        return True
    finally:
        for descriptor in held:
            os.close(descriptor)


def _directory_chain_is_real_by_lstat(path: Path) -> bool:
    """The weaker fallback for a host that cannot open by descriptor (`_NO_FOLLOW_WALK`
    is false, e.g. Windows): each prefix from root to leaf is `lstat`ed in turn and must
    be a real directory, never a symlink. Racier than the descriptor walk -- a swap
    between two of these calls is not caught -- but still refuses every symlink,
    ancestor or leaf, that is in place at the time of this check.
    """
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            status = os.lstat(current)
        except OSError:
            return False
        if not stat.S_ISDIR(status.st_mode):
            return False
    return True


def is_trusted_local_checkout_root(value: str) -> bool:
    """Whether `value` is safe to register as an exact, installation-local checkout root.

    Pure and read-only: it reads nothing beneath the named path -- registration binds an
    operator's own claim about a directory, never anything the directory itself asserts,
    so no config file, remote or other hint inside it is ever consulted. `value` must be
    an absolute path in canonical form (no `.` or `..` segment) naming a real directory,
    and no component of it, from the filesystem root down to the leaf, may be a symlink:
    a relative path, a `..`-bearing one, a missing path, and one with a symlink anywhere
    in its ancestry (not only as the final component) are all refused.

    Checked by opening each component by descriptor without following it
    (`_directory_chain_is_real_by_descriptor`) on a host that supports it; a host that
    cannot establish that (no `dir_fd`, `O_NOFOLLOW` or `O_DIRECTORY`) falls back to a
    per-component `lstat` walk rather than trusting only the leaf.

    This is a point-in-time check, not a standing guarantee: it says nothing about a
    later swap of the same path, which is why every later read of a bound checkout
    reopens it by descriptor without following a link, rather than trusting this once.
    """
    if not value or "\x00" in value or len(value) > 512:
        return False
    path = Path(value)
    if (
        not path.is_absolute()
        or os.path.normpath(value) != value
        or any(part in (".", "..") for part in path.parts)
    ):
        return False
    if _NO_FOLLOW_WALK:
        return _directory_chain_is_real_by_descriptor(path)
    return _directory_chain_is_real_by_lstat(path)


@dataclass(frozen=True, slots=True)
class CheckoutFile:
    """The bytes of one trusted-checkout file and the digest they were verified against."""

    content: bytes
    digest: str
    executable: bool = False


class _SourceChanged(SourceCaptureRefused):
    """The file moved or changed while it was read (fixed text, like every refusal)."""


class _RootIdentityChanged(_SourceChanged):
    """The checkout root no longer matches the identity pinned for this capture.

    A subclass of `_SourceChanged`: from a capture attempt's point of view, the root
    itself is a source that changed underneath it, so it is caught wherever a per-file
    read already treats `_SourceChanged` as instability rather than a hard refusal.
    """


class _SourceOversized(SourceCaptureRefused):
    """The file is larger than `MAX_SOURCE_BYTES`."""
def _identity(name: bytes, directory: int | None) -> tuple[int, int] | None:
    """What `name` is right now, relative to a held directory, without following it."""
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return None
    return current.st_dev, current.st_ino


def _root_components(checkout_root: Path) -> tuple[bytes, ...]:
    """Every component from the filesystem root down to `checkout_root`.

    `Path` parsing already collapses a `.` segment, a repeated separator and a
    trailing separator; a `..` segment survives that parsing only if the caller put
    one in, and resolving it would mean following whatever an ancestor names right
    now, exactly the escape this walk exists to refuse. A root that is not
    absolute, or that names only the filesystem root itself, is refused the same
    way: neither is a checkout this walk can open.
    """
    if not checkout_root.is_absolute():
        raise SourceCaptureRefused("checkout root is not an accessible directory")
    parts = checkout_root.parts[1:]
    if not parts or any(part == ".." for part in parts):
        raise SourceCaptureRefused("checkout root is not an accessible directory")
    return tuple(part.encode() for part in parts)


def _open_checkout_root(
    components: tuple[bytes, ...], expected_identity: tuple[int, int] | None = None
) -> int:
    """Open `checkout_root`'s descriptor by a no-follow walk from the filesystem root.

    Every component, including every ancestor, is opened relative to the descriptor
    of the one before it and refused if it is a symlink; nothing is ever opened by a
    composed path, so an ancestor symlink cannot redirect where this lands. The
    caller owns the returned descriptor and must close it.

    When `expected_identity` is given, the freshly opened descriptor is refused
    unless it is exactly that `(st_dev, st_ino)`: one capture attempt pins the root's
    identity once and every later open in that attempt, enumeration or file read
    alike, must land on that same object, not merely on whatever the path names by
    the time this call runs.
    """
    directory = _open_component(b"/", _DIRECTORY_FLAGS, None)
    if directory is None:
        raise SourceCaptureRefused("checkout root is not an accessible directory")
    for name in components:
        next_directory = _open_component(name, _DIRECTORY_FLAGS, directory)
        os.close(directory)
        if next_directory is None:
            raise SourceCaptureRefused("checkout root is not an accessible directory")
        directory = next_directory
    if expected_identity is not None:
        opened = os.fstat(directory)
        if (opened.st_dev, opened.st_ino) != expected_identity:
            os.close(directory)
            raise _RootIdentityChanged("checkout root changed identity during capture")
    return directory


def _root_identity(components: tuple[bytes, ...]) -> tuple[int, int] | None:
    """What `checkout_root` is right now, by the same no-follow walk, or `None`."""
    try:
        directory = _open_checkout_root(components)
    except SourceCaptureRefused:
        return None
    try:
        status = os.fstat(directory)
    finally:
        os.close(directory)
    return status.st_dev, status.st_ino


def read_checkout_file(
    *, checkout_root: Path, relative_path: str, expected_digest: str
) -> CheckoutFile:
    """Read one file of a trusted checkout, refusing anything that is not that file.

    `checkout_root` is explicit and trusted, but must itself be a real directory: every
    one of its components, from the filesystem root down, is opened relative to the
    descriptor of the one before it and refused if it is a symlink, exactly like every
    name below it, so an ancestor symlink cannot redirect the open either.
    `relative_path` is not trusted. It must satisfy the Engineering Memory path rules
    (`engineering_source.valid_path`), which keep its exact Unicode and case and refuse
    an absolute path, a `..` segment and every other spelling that could leave the
    checkout; it is used exactly as given, never normalised or case-folded.

    The walk opens each component relative to the descriptor of the one before it, never
    by a composed path, and holds every descriptor until the read is over. A symlink,
    whether it is the file or any parent, is refused rather than followed, and no rename
    can redirect a walk that is already inside a directory. The file must be one regular
    file of at most `MAX_SOURCE_BYTES`, read within that bound. Afterwards every name is
    resolved again and must still be the object that was opened, and the file's size and
    modification time must not have moved; otherwise the source changed or moved while
    it was read and is refused. Last, the bytes must hash to `expected_digest`.

    Every refusal is a `SourceCaptureRefused` with fixed text. No path, local or
    relative, and no operating-system error is quoted or chained. Not every host can do
    this walk; one that cannot is refused, not given a weaker read. Nothing is persisted.
    """
    if not is_content_checksum(expected_digest):
        raise SourceCaptureRefused(
            "expected digest is outside the accepted checksum domain"
        )
    return _read_checkout(checkout_root, relative_path, expected_digest)


def _walk_read(
    root: int, components: tuple[bytes, ...], expected_digest: str | None
) -> CheckoutFile:
    """Read one file below an already-opened, no-follow-verified `root` descriptor.

    Every component of the relative path is opened relative to the descriptor of
    the one before it and held until the read is over; a symlink, whether it is the
    file or any parent, is refused rather than followed, and no rename can redirect
    a walk that is already inside a directory. Afterwards every held name is
    resolved again and must still be the object that was opened. `root` is owned by
    the caller and is never closed here.
    """
    *parents, leaf = components
    with ExitStack() as stack:
        # Each held name as (its directory, the name, the object opened): the second
        # look at the end compares against exactly this.
        held: list[tuple[int | None, bytes, os.stat_result]] = []
        directory: int | None = root
        for name in parents:
            descriptor = _open_component(name, _DIRECTORY_FLAGS, directory)
            if descriptor is None:
                raise SourceCaptureRefused(
                    "source cannot be opened as a file inside the checkout"
                )
            stack.callback(os.close, descriptor)
            held.append((directory, name, os.fstat(descriptor)))
            directory = descriptor
        descriptor = _open_component(leaf, _FILE_FLAGS, directory)
        if descriptor is None:
            raise SourceCaptureRefused(
                "source cannot be opened as a file inside the checkout"
            )
        stack.callback(os.close, descriptor)
        opened = os.fstat(descriptor)
        held.append((directory, leaf, opened))
        if not stat.S_ISREG(opened.st_mode):
            raise SourceCaptureRefused("source must be one regular file")
        if opened.st_size > MAX_SOURCE_BYTES:
            raise _SourceOversized("source exceeds the capture size limit")

        chunks: list[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)

        if len(content) > MAX_SOURCE_BYTES:
            raise _SourceOversized("source exceeds the capture size limit")
        if (
            (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns)
            or len(content) != after.st_size
            or any(
                _identity(name, directory) != (status.st_dev, status.st_ino)
                for directory, name, status in held
            )
        ):
            raise _SourceChanged("source changed while it was being read")

    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if expected_digest is not None and digest != expected_digest:
        raise SourceCaptureRefused("source content does not match the expected digest")
    return CheckoutFile(
        content=content,
        digest=digest,
        executable=bool(opened.st_mode & stat.S_IXUSR),
    )


def _read_checkout(
    checkout_root: Path,
    relative_path: str,
    expected_digest: str | None,
    expected_identity: tuple[int, int] | None = None,
) -> CheckoutFile:
    """The walked read; `expected_digest=None` returns the verified-stable bytes.

    `checkout_root` is opened by a no-follow walk of every one of its components,
    from the filesystem root down, not just its last: an ancestor symlink refuses
    the open exactly like a symlinked leaf does. That root descriptor is held for
    exactly this one read and closed afterwards. `expected_identity`, when given,
    pins this open to the root identity a capture attempt already established
    elsewhere: a root swapped out since then is refused here rather than walked
    afresh, even if nothing else about the swap is visible from this one read.
    """
    if not valid_path(relative_path):
        raise SourceCaptureRefused(
            "repository path is outside the accepted portable domain"
        )
    if not _NO_FOLLOW_WALK:
        raise SourceCaptureRefused(
            "this host cannot read a checkout without following links"
        )
    components = tuple(part.encode() for part in relative_path.split("/"))
    root = _open_checkout_root(_root_components(checkout_root), expected_identity)
    try:
        return _walk_read(root, components, expected_digest)
    finally:
        os.close(root)


WORKING_TREE_MANIFEST_FORMAT: Final = "omnivia.working-tree-manifest.v1"
MAX_CAPTURE_FILES: Final = 10_000
MAX_CAPTURE_TOTAL_BYTES: Final = 256 * 1024 * 1024
MAX_CAPTURE_ATTEMPTS: Final = 3
MAX_GIT_OUTPUT_BYTES: Final = 8 * 1024 * 1024
GIT_TIMEOUT_SECONDS: Final = 20.0
_GIT_ENV: Final = {
    "PATH": os.environ.get("PATH", os.defpath),
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}
#: Repository-provided commands and filters git could otherwise run for these read-only
#: plumbing calls; none of them is needed, so each is turned off explicitly.
_GIT_ARGS: Final = (
    "git",
    "--no-pager",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=" + os.devnull,
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.quotePath=false",
)


@dataclass(frozen=True, slots=True)
class ManifestFile:
    """One captured regular file: exact path spelling, mode, digest and frozen bytes."""

    path: str
    mode: str
    tracked: bool
    digest: str
    length: int
    content: bytes


@dataclass(frozen=True, slots=True)
class ManifestOmission:
    """A path (or an unrepresentable one, `path=None`) that is not captured, and why."""

    path: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class WorkingTreeManifest:
    """An immutable in-memory capture of a working tree over its base commit.

    `complete` is true only when every enumerated path was captured and the checkout
    was unchanged across the capture. It is never certified from Git metadata: the
    bytes were read through the descriptor-walked reader. Ignored files are outside the
    coverage by definition (`coverage`). Nothing here is persisted.
    """

    base_commit_id: str
    files: tuple[ManifestFile, ...]
    omissions: tuple[ManifestOmission, ...]
    complete: bool
    attempts: int

    def to_dict(self) -> dict[str, object]:
        """The bytes-free, deterministic description that `manifest_digest` covers."""
        return {
            "format": WORKING_TREE_MANIFEST_FORMAT,
            "snapshot_kind": "working_tree",
            "base": {"kind": "commit", "commit_id": self.base_commit_id},
            "complete": self.complete,
            "coverage": {
                "tracked": "included",
                "untracked": "included",
                "ignored": "excluded",
                "symlinks": "omitted",
                "submodules": "omitted",
            },
            "files": [
                {
                    "path": f.path,
                    "kind": "file",
                    "mode": f.mode,
                    "tracked": f.tracked,
                    "content_digest": f.digest,
                    "length_bytes": f.length,
                }
                for f in self.files
            ],
            "omissions": [{"path": o.path, "reason": o.reason} for o in self.omissions],
        }

    @property
    def manifest_digest(self) -> str:
        canonical = to_canonical_json(self.to_dict()).encode()
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


#: Fixed source for the isolated child that runs one git query. `pass_fds` keeps a
#: directory descriptor, opened by a no-follow walk, alive across the fork; this
#: child's only job is `os.fchdir` onto it and `os.execvp` into `git`, so git's
#: working directory is bound to that descriptor's identity, never to a path that a
#: rename or symlink swap could redirect after the descriptor was opened. `-I -S`
#: keep the child free of user site customisation and `PYTHON*` environment
#: overrides. No `preexec_fn` runs in this multithreaded service: this fixed,
#: reviewed source is the entire child, and nothing dynamic is interpolated into it,
#: only the descriptor number and the fixed git arguments are ever passed as argv.
_GIT_ROOT_EXEC_HELPER: Final = (
    "import os, sys\n"
    "os.fchdir(int(sys.argv[1]))\n"
    "os.execvp('git', ['git', *sys.argv[2:]])\n"
)


def _git(root: int, *args: str) -> bytes:
    """Run one bounded, read-only git query rooted at an open directory descriptor.

    `root` must already be opened by a no-follow walk (`_open_checkout_root`); a
    path is never accepted here. Fixed-text refusal on any failure.
    """
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    unavailable = False
    try:
        process = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-S",
                "-c",
                _GIT_ROOT_EXEC_HELPER,
                str(root),
                *_GIT_ARGS[1:],
                *args,
            ),
            env=_GIT_ENV,
            pass_fds=(root,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        unavailable = True
    if unavailable:
        raise SourceCaptureRefused("git is not available for capture")
    assert process.stdout is not None
    chunks: list[bytes] = []
    size = 0
    failure = ""
    try:
        while not failure:
            wait = deadline - time.monotonic()
            if wait <= 0:
                failure = "git query exceeded its time bound"
            elif select.select([process.stdout], [], [], wait)[0]:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                size += len(chunk)
                chunks.append(chunk)
                if size > MAX_GIT_OUTPUT_BYTES:
                    failure = "git query exceeded its output bound"
    finally:
        if failure or process.poll() is None:
            process.kill()
        process.stdout.close()
        process.wait()
    if failure:
        raise SourceCaptureRefused(failure)
    if process.returncode != 0:
        raise SourceCaptureRefused("git could not describe this checkout")
    return b"".join(chunks)


def _enumerate(
    root: Path, expected_identity: tuple[int, int] | None
) -> tuple[str, dict[bytes, tuple[str, bool]], tuple[int, int]]:
    """HEAD identity, `{path bytes: (git mode, tracked)}`, and the root's own identity.

    `root` is opened once by a no-follow walk of every component and that one
    descriptor is reused for every git query below, so a rename or symlink swap of
    `root` after the descriptor was opened cannot redirect what git reads. When
    `expected_identity` is given, the open itself is refused unless it lands on that
    same `(st_dev, st_ino)`: this closes the window where a root already swapped
    out during an earlier step of the same capture attempt would otherwise be
    walked afresh here without anyone noticing it is not the root that attempt
    pinned. Once enumeration is done, `root`'s identity is checked again by a fresh
    walk against the descriptor's own identity: a root moved or replaced during
    enumeration is refused here rather than silently enumerating whatever now sits
    at that path.
    """
    components = _root_components(root)
    root_fd = _open_checkout_root(components, expected_identity)
    try:
        opened = os.fstat(root_fd)
        opened_identity = (opened.st_dev, opened.st_ino)
        toplevel = _git(root_fd, "rev-parse", "--show-toplevel").rstrip(b"\n")
        if os.path.realpath(os.fsdecode(toplevel)) != os.path.realpath(root):
            raise SourceCaptureRefused("checkout root is not a repository top level")
        head = _git(
            root_fd, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"
        ).strip()
        algorithm = {40: "sha1", 64: "sha256"}.get(len(head))
        if algorithm is None or re.fullmatch(rb"[0-9a-f]+", head) is None:
            raise SourceCaptureRefused("checkout has no verifiable base commit")
        entries: dict[bytes, tuple[str, bool]] = {}
        for record in _git(root_fd, "ls-files", "-z", "--stage").split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, _oid, stage = meta.split(b" ")
            # An unmerged path (stage != 0) has no single content: recorded as such.
            entries[path] = (mode.decode() if stage == b"0" else "unmerged", True)
        others = _git(root_fd, "ls-files", "-z", "--others", "--exclude-standard")
        for path in others.split(b"\0"):
            if path:
                entries[path] = ("100644", False)
        if len(entries) > MAX_CAPTURE_FILES:
            raise SourceCaptureRefused("checkout exceeds the capture file limit")
        if _root_identity(components) != opened_identity:
            raise _RootIdentityChanged("checkout root changed identity during capture")
        return f"{algorithm}:{head.decode()}", entries, opened_identity
    finally:
        os.close(root_fd)


def _capture_pass(
    root: Path,
    entries: dict[bytes, tuple[str, bool]],
    expected_identity: tuple[int, int],
) -> tuple[list[ManifestFile], list[ManifestOmission], bool]:
    """Read every entry, pinned to the root identity this capture attempt started with.

    Every read below, both the first look and the final re-verification, opens the
    checkout root through `expected_identity`: a root swapped out partway through
    this pass is refused at the read that would otherwise have landed on it, not
    just at whatever enumeration runs before or after this function.
    """
    files: list[ManifestFile] = []
    omissions: list[ManifestOmission] = []
    stable = True
    total = 0
    for raw in sorted(entries):
        mode, tracked = entries[raw]
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            path = ""
        if not valid_path(path):
            omissions.append(ManifestOmission(None, "path_not_portable"))
            continue
        if mode in {"120000", "160000", "unmerged"}:
            reason = {"120000": "symlink", "160000": "submodule"}.get(mode, mode)
            omissions.append(ManifestOmission(path, reason))
            continue
        try:
            read = _read_checkout(root, path, None, expected_identity)
        except _SourceChanged:
            stable = False
            omissions.append(ManifestOmission(path, "changed_during_capture"))
            continue
        except _SourceOversized:
            omissions.append(ManifestOmission(path, "oversized"))
            continue
        except SourceCaptureRefused:
            # A label only: the reader already refused, this decides no capture.
            try:
                is_link = stat.S_ISLNK((root / path).lstat().st_mode)
            except OSError:
                is_link = False
            reason = "symlink" if is_link else "missing_or_unsupported"
            omissions.append(ManifestOmission(path, reason))
            continue
        if total + len(read.content) > MAX_CAPTURE_TOTAL_BYTES:
            omissions.append(ManifestOmission(path, "total_bytes_limit"))
            continue
        total += len(read.content)
        files.append(
            ManifestFile(
                path=path,
                mode="100755" if read.executable else "100644",
                tracked=tracked,
                digest=read.digest,
                length=len(read.content),
                content=read.content,
            )
        )
    # Second look: every captured file must still hold the bytes just frozen.
    kept: list[ManifestFile] = []
    for file in files:
        try:
            _read_checkout(root, file.path, file.digest, expected_identity)
        except SourceCaptureRefused:
            stable = False
            omissions.append(ManifestOmission(file.path, "changed_during_capture"))
        else:
            kept.append(file)
    return kept, omissions, stable


def capture_working_tree_manifest(*, checkout_root: Path) -> WorkingTreeManifest:
    """Capture an explicitly trusted checkout as a `working_tree` over its HEAD commit.

    Git is used only to name HEAD and to list tracked and untracked paths, by fixed
    read-only plumbing commands with a scrubbed environment, no hooks, no external
    diff, no filters and no network, each bounded in time and output. Every path then
    goes through the portable-path validator and the descriptor-walked reader, and the
    manifest keeps the frozen bytes for later publication. Symlinks, submodules,
    oversized, missing, unreadable and changing files are omitted and make the manifest
    `complete=False`; a checkout whose listing or HEAD moves across the capture is
    retried up to `MAX_CAPTURE_ATTEMPTS` times and then returned incomplete.

    Each attempt pins one root identity from its first enumeration and every later
    open in that attempt, the second enumeration and every file read alike, is
    refused unless it lands on that same object. A root swapped for a different
    checkout and swapped back before the attempt's final enumeration is therefore
    still caught: it is the reads in between, not the before-and-after comparison,
    that refuse it.

    Nothing is registered, persisted or published here; `capture_working_tree_snapshot`
    is the service-owned step that does that.
    """
    if not _NO_FOLLOW_WALK:
        raise SourceCaptureRefused(
            "this host cannot read a checkout without following links"
        )
    for attempt in range(1, MAX_CAPTURE_ATTEMPTS + 1):
        head, entries, root_identity = _enumerate(checkout_root, None)
        files, omissions, stable = _capture_pass(checkout_root, entries, root_identity)
        if stable:
            try:
                other_head, other_entries, _ = _enumerate(checkout_root, root_identity)
                stable = (other_head, other_entries) == (head, entries)
            except _SourceChanged:
                stable = False
        if stable or attempt == MAX_CAPTURE_ATTEMPTS:
            return WorkingTreeManifest(
                base_commit_id=head,
                files=tuple(files),
                omissions=tuple(
                    sorted(omissions, key=lambda o: (o.path or "", o.reason))
                    + ([] if stable else [ManifestOmission(None, "unstable_checkout")])
                ),
                complete=stable and not omissions,
                attempts=attempt,
            )
    raise AssertionError("unreachable")  # pragma: no cover


SNAPSHOT_RESULT_FORMAT: Final = "omnivia.working-tree-snapshot-result.v1"
MANIFEST_MEDIA_TYPE: Final = "application/json"
#: The manifest's evidence source id is ``working-tree-manifest.<snapshot id>`` and must
#: fit `_IDENTIFIER`'s 128 characters.
_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")


@dataclass(frozen=True, slots=True)
class WorkingTreeSnapshotResult:
    """The redacted result of one snapshot capture: identities and digests, no paths."""

    status: str
    workspace_id: str
    repository_id: str
    snapshot_id: str
    manifest_digest: str
    manifest_evidence_id: str
    capture_status: str
    file_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "format": SNAPSHOT_RESULT_FORMAT,
            "status": self.status,
            "workspace_id": self.workspace_id,
            "repository_id": self.repository_id,
            "snapshot": {"kind": "working_tree", "snapshot_id": self.snapshot_id},
            "manifest_digest": self.manifest_digest,
            "manifest_evidence_id": self.manifest_evidence_id,
            "capture_status": self.capture_status,
            "file_count": self.file_count,
        }


def _require_bound_checkout(
    runner: ServiceRunner, *, repository_id: str, checkout_root: Path
) -> None:
    """The repository is registered and this installation bound this exact root to it."""
    assert runner.connection is not None
    assert runner.identity is not None and runner.workspace_id is not None
    if (
        repository_identity.resolve_repository(
            runner.connection,
            workspace_id=runner.workspace_id,
            repository_id=repository_id,
        )
        is None
    ):
        raise SourceCaptureRefused("repository is not registered in this workspace")
    bound = runner.connection.execute(
        "SELECT 1 FROM omnivia_engineering_checkouts WHERE workspace_id = ? "
        "AND repository_id = ? AND installation_id = ? AND checkout_hint = ?",
        (
            runner.workspace_id,
            repository_id,
            runner.identity.installation_id,
            os.fspath(checkout_root),
        ),
    ).fetchone()
    if bound is None:
        raise SourceCaptureRefused(
            "checkout is not bound to this repository on this installation"
        )


def _existing_identity(
    runner: ServiceRunner, snapshot_id: str
) -> tuple[object, ...] | None:
    assert runner.connection is not None
    row = runner.connection.execute(
        "SELECT repository_id, snapshot_kind, manifest_digest, capture_status "
        "FROM omnivia_engineering_snapshots WHERE workspace_id = ? AND snapshot_id = ?",
        (runner.workspace_id, snapshot_id),
    ).fetchone()
    return None if row is None else tuple(row)


def capture_working_tree_snapshot(
    *,
    workspace_root: Path,
    installation_root: Path,
    repository_id: str,
    checkout_root: Path,
    snapshot_id: str,
    core_version: str,
) -> WorkingTreeSnapshotResult:
    """Persist a `capture_working_tree_manifest` result as an authoritative snapshot.

    A maintenance path, not an application operation: it runs only while a
    `ServiceRunner` owns the workspace. `repository_id` must already be registered and
    `checkout_root` must be the exact root this installation bound to it
    (`omnivia_engineering_checkouts`); nothing is inferred from Git remotes or names, and
    a mismatch is refused before the checkout is read.

    The manifest and every captured file are frozen and published as content-addressed
    blobs first; only then does one fenced transaction record the blobs, the manifest as
    immutable evidence (`source_native_id` ``working-tree-manifest.<snapshot_id>``) and
    the snapshot row, whose `manifest_digest` is exactly that evidence's blob digest. A
    failed publication raises before any row is written. The snapshot is kind
    `working_tree` (the schema allows a base commit only on `git_commit`); the base commit
    lives in the manifest. `capture_status` is `incomplete` whenever the manifest is.

    Retrying an identical snapshot returns `already_captured` with the same identity;
    the same `snapshot_id` with different content or repository is refused.
    """
    if _IDENTIFIER.fullmatch(repository_id) is None or (
        _SNAPSHOT_ID.fullmatch(snapshot_id) is None
    ):
        raise SourceCaptureRefused("identity is outside the accepted identifier domain")
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=workspace_root,
            installation_root=installation_root,
            core_version=core_version,
            endpoint=None,
        )
    )
    report = runner.start()
    try:
        if not report.ready:
            raise SourceCaptureRefused(
                report.reason or "workspace ownership was refused"
            )
        assert runner.connection is not None and runner.identity is not None
        assert runner.workspace_id is not None and runner.generation is not None
        _require_bound_checkout(
            runner, repository_id=repository_id, checkout_root=checkout_root
        )
        manifest = capture_working_tree_manifest(checkout_root=checkout_root)
        document = manifest.to_dict()
        manifest_bytes = canonical_document(document).encode()
        manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
        capture_status = "complete" if manifest.complete else "incomplete"
        expected = (repository_id, "working_tree", manifest_digest, capture_status)
        # Refuse a conflicting retry before publishing bytes nothing would reference;
        # the fenced transaction below re-checks against a race.
        prior = _existing_identity(runner, snapshot_id)
        if prior is not None and prior != expected:
            raise SourceCaptureRefused(
                "snapshot identity already names different content"
            )
        blobs = {f.digest: f.content for f in manifest.files}
        blobs[manifest_digest] = manifest_bytes
        for digest, content in blobs.items():
            publish_blob(runner.layout.blobs_path, digest, content)

        source_id = f"working-tree-manifest.{snapshot_id}"
        with fenced_transaction(
            runner.connection,
            runner.identity,
            workspace_id=runner.workspace_id,
            fencing_generation=runner.generation,
        ):
            existing = _existing_identity(runner, snapshot_id)
            status = "captured"
            if existing is not None:
                if existing != expected:
                    raise SourceCaptureRefused(
                        "snapshot identity already names different content"
                    )
                status = "already_captured"
                row = runner.connection.execute(
                    "SELECT evidence_id FROM omnivia_evidence_artifacts "
                    "WHERE workspace_id = ? AND source_kind = ? "
                    "AND source_native_id = ? AND blob_content_digest = ?",
                    (runner.workspace_id, SOURCE_KIND, source_id, manifest_digest),
                ).fetchone()
                if row is None:
                    raise SourceCaptureRefused("the snapshot has no manifest evidence")
                evidence_id = str(row[0])
            else:
                now_us = time.time_ns() // 1000
                audit_ref = f"aud-local-{uuid.uuid4().hex}"
                runner.connection.execute(
                    "INSERT INTO omnivia_application_audit_events "
                    "(audit_ref, workspace_id, principal_id, operation, purpose, "
                    "request_id, correlation_id, trace_id, granted_authority_json, "
                    "outcome_class, error_code, recorded_at_us) VALUES "
                    "(?, ?, 'core-service', 'engineering.snapshot.capture', "
                    "'engineering.snapshot', ?, ?, ?, '{}', 'succeeded', NULL, ?)",
                    (
                        audit_ref,
                        runner.workspace_id,
                        audit_ref,
                        audit_ref,
                        audit_ref,
                        now_us,
                    ),
                )
                for file in manifest.files:
                    _ensure_blob(runner, file.digest, file.length, now_us)
                evidence_id = (
                    _write_capture(
                        runner,
                        source_id=source_id,
                        digest=manifest_digest,
                        length=len(manifest_bytes),
                        media_type=MANIFEST_MEDIA_TYPE,
                        capture="working_tree_manifest",
                    ).evidence_id
                    or ""
                )
                recorded = repository_identity.record_snapshot(
                    runner.connection,
                    SimpleNamespace(audit_ref=audit_ref),
                    workspace_id=runner.workspace_id,
                    snapshot_id=snapshot_id,
                    repository_id=repository_id,
                    snapshot_kind="working_tree",
                    manifest=document,
                    base_commit=None,
                    capture_status=capture_status,
                    captured_at_us=now_us,
                )
                if (
                    recorded != manifest_digest
                ):  # pragma: no cover - same canonical form
                    raise SourceCaptureRefused("manifest digest is not reproducible")
        return WorkingTreeSnapshotResult(
            status=status,
            workspace_id=runner.workspace_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            manifest_digest=manifest_digest,
            manifest_evidence_id=evidence_id,
            capture_status=capture_status,
            file_count=len(manifest.files),
        )
    finally:
        runner.stop()


__all__ = [
    "MAX_CAPTURE_ATTEMPTS",
    "MAX_SOURCE_BYTES",
    "SOURCE_CAPTURE_FORMAT",
    "WORKING_TREE_MANIFEST_FORMAT",
    "BlobPublicationRefused",
    "CheckoutFile",
    "ManifestFile",
    "ManifestOmission",
    "SourceCaptureRefused",
    "SourceCaptureResult",
    "WorkingTreeManifest",
    "WorkingTreeSnapshotResult",
    "capture_local_source",
    "capture_working_tree_manifest",
    "capture_working_tree_snapshot",
    "is_trusted_local_checkout_root",
    "publish_blob",
    "read_checkout_file",
]
