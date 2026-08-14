"""A first-party, read-only local filesystem `SourceConnectorSpi` (V06-8).

Scope, stated honestly rather than implied. This is a bounded local root
connector only: one process, no sandbox, no plugin loading, no credential
resolution, and no claim about a remote or network filesystem. It implements
exactly the four accepted operations (`describe`, `migrate_cursor`, `probe`,
`poll`) and nothing else. `IngestionCoordinator.synchronise_spi` is the
host-owned durable bridge; this adapter never receives storage or blob-store
handles and never performs authoritative persistence itself.

Identity is locator-derived, not source-native (`IdentityStability.
LOCATOR_DERIVED`): a file's identity is a deterministic digest of its
POSIX-relative path under the configured root. A rename is therefore observed
as a deletion of the old identity and the creation of a new one -- this
connector makes no rename-continuity claim.

Completeness across resumes and restarts comes entirely from the cursor: each
successor cursor's payload is a compact encoding of the full set of relative
paths present at that snapshot, so a later poll can diff the current tree
against it to emit explicit deletions with no reliance on process memory or
any state outside the cursor itself. That is also why the cursor is bounded:
a root whose complete path listing does not fit in the SPI's 4096-byte
opaque-payload ceiling is refused rather than silently truncated.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from omnivia_core.connector.host import canonical_cursor_digest
from omnivia_core.connector.models import MAX_LOCATOR_LENGTH, HealthState, SourceHealth
from omnivia_core.connector.spi import (
    ERROR_CONNECTOR_CURSOR_UNMIGRATABLE,
    ERROR_CONNECTOR_STATE_INVALID,
    MAX_CURSOR_PAYLOAD_BYTES,
    MAX_OBSERVATION_CONTENT_BYTES,
    Batch,
    ConnectorDescriptor,
    ConnectorRefused,
    CursorBinding,
    CursorState,
    DeletionSignal,
    HealthStatus,
    IdentityStability,
    ItemFailure,
    Observation,
    PollContext,
    PollLimits,
    SpiVersion,
)
from omnivia_core.contracts.v1.generated import (
    ERROR_CODE_CANCELLED,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    RETRY_CLASS_NON_RETRYABLE,
)

#: `extension -> media type`. Deliberately small: anything else is refused.
ACCEPTED_MEDIA_TYPES: Final[dict[str, str]] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
}

#: Declared once, describing an honest limitation rather than a promise this
#: connector cannot keep.
DECLARED_LIMITATIONS: Final[tuple[str, ...]] = (
    "bounded_local_root_only",
    "first_party_in_process_only",
    "full_rescan_each_poll",
    "no_credential_resolution",
    "no_rename_continuity",
)

_ITEM_FAILURE_ESCAPE: Final = "filesystem_item_escape"
_ITEM_FAILURE_CHANGED: Final = "filesystem_item_changed_while_reading"
_ITEM_FAILURE_INVALID_UTF8: Final = "filesystem_item_invalid_utf8"
_ITEM_FAILURE_NUL_BYTE: Final = "filesystem_item_nul_byte"
_ITEM_FAILURE_PATH_TOO_LONG: Final = "filesystem_item_path_too_long"
_ITEM_FAILURE_SPECIAL_FILE: Final = "filesystem_item_special_file"
_ITEM_FAILURE_SYMLINK: Final = "filesystem_item_symlink"
_ITEM_FAILURE_SYMLINK_DIR: Final = "filesystem_item_symlink_directory"
_ITEM_FAILURE_UNREADABLE: Final = "filesystem_item_unreadable"
_ITEM_FAILURE_UNSUPPORTED_TYPE: Final = "filesystem_item_unsupported_type"

#: Declared metadata size for every observation this connector emits: it never
#: attaches `metadata_json`, so the declared metadata size is always zero.
#: `metadata_bytes` names metadata size, not the size of the inline `content`
#: this connector also attaches -- conflating the two would make the host's
#: `max_item_metadata_bytes`/`max_run_bytes` accounting measure the wrong thing.
_METADATA_BYTES: Final = 0


def _relative_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _native_id(relative_path: str) -> str:
    """A deterministic identity derived from the locator, and nothing else."""
    return "fs-" + hashlib.sha256(relative_path.encode("utf-8")).hexdigest()


def _encode_snapshot(relative_paths: tuple[str, ...]) -> bytes:
    text = "\n".join(sorted(relative_paths))
    return base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=")


def _decode_snapshot(payload: bytes) -> frozenset[str]:
    padded = payload + b"=" * (-len(payload) % 4)
    try:
        text = base64.urlsafe_b64decode(padded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise ConnectorRefused(
            ERROR_CONNECTOR_STATE_INVALID, "cursor payload is not a filesystem snapshot"
        ) from error
    return frozenset(line for line in text.split("\n") if line)


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    relative_path: str
    absolute_path: Path
    size: int


def _fail(native_id: str, error: str, detail: str) -> ItemFailure:
    return ItemFailure(
        source_native_id=native_id,
        error=error,
        retry_class=RETRY_CLASS_NON_RETRYABLE,
        detail=detail,
    )


@dataclass(frozen=True, slots=True)
class FilesystemSourceConnector:
    """A deterministic `SourceConnectorSpi` over one bounded local directory.

    `root` is supplied explicitly at construction and lives only on this
    object -- never on `PollContext`, and never resolved from a credential.
    Every scan is read-only: nothing here opens a file for writing, creates a
    path or follows a symlink.
    """

    root: Path
    connector_id: str = "local.filesystem"
    connector_version: str = "0.1.0"
    spi_version: SpiVersion = field(default_factory=lambda: SpiVersion(major=1, minor=0))
    identity_stability: IdentityStability = IdentityStability.LOCATOR_DERIVED
    state_version: int = 1
    supported_state_versions: tuple[int, ...] = (1,)
    max_item_bytes: int = 1_048_576
    max_path_length: int = 512
    max_files: int = 4096
    max_tree_bytes: int = 67_108_864
    _root_is_symlink: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_item_bytes > MAX_OBSERVATION_CONTENT_BYTES:
            raise ValueError(
                f"max_item_bytes ({self.max_item_bytes}) exceeds the SPI inline-content "
                f"ceiling of {MAX_OBSERVATION_CONTENT_BYTES} bytes"
            )
        # Symlinks are resolved *after* the check above: `strict=False` lets a
        # not-yet-created root through, but a root that is itself a symlink is
        # never silently followed into its target -- `poll`/`probe` refuse it.
        configured = Path(self.root)
        object.__setattr__(self, "root", configured.resolve(strict=False))
        object.__setattr__(self, "_root_is_symlink", configured.is_symlink())

    # --- discovery -----------------------------------------------------

    def describe(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            connector_id=self.connector_id,
            connector_version=self.connector_version,
            spi_version=self.spi_version,
            identity_stability=self.identity_stability,
            declared_limits=PollLimits(
                max_batch_items=max(self.max_files, 1),
                max_item_metadata_bytes=max(_METADATA_BYTES, 1),
                max_run_bytes=max(self.max_tree_bytes, 1),
                poll_deadline_ms=30_000,
            ),
            supported_state_versions=self.supported_state_versions,
            declared_limitations=DECLARED_LIMITATIONS,
        )

    # --- migration -------------------------------------------------------

    def migrate_cursor(self, cursor: CursorState) -> CursorState:
        # Exactly one state version is supported: there is nowhere forward to
        # migrate to, so every request is an explicit resynchronization.
        raise ConnectorRefused(
            ERROR_CONNECTOR_CURSOR_UNMIGRATABLE,
            f"state version {cursor.state_version} has no forward target",
        )

    # --- health ------------------------------------------------------------

    def probe(self, ctx: PollContext) -> HealthStatus:
        del ctx  # No credential is resolved and no context member is read.
        if self._root_is_symlink or self.root.is_symlink():
            return SourceHealth(state=HealthState.UNAVAILABLE, detail="root is a symlink")
        if not self.root.exists():
            return SourceHealth(state=HealthState.UNAVAILABLE, detail="root does not exist")
        if not self.root.is_dir():
            return SourceHealth(state=HealthState.UNAVAILABLE, detail="root is not a directory")
        if not os.access(self.root, os.R_OK):
            return SourceHealth(state=HealthState.UNAVAILABLE, detail="root is not readable")
        return SourceHealth(state=HealthState.HEALTHY, detail="")

    # --- polling -----------------------------------------------------------

    def poll(self, ctx: PollContext, cursor: CursorState | None) -> Iterator[Batch]:
        # No credential is ever resolved: `ctx.resolve_credential` is never
        # called. `ctx.limits`/`ctx.deadline` are host-verified after the fact
        # (`CON-D11`, `CON-C036`); only `ctx.cancellation` -- owned by neither
        # side until observed -- is checked here, before the scan runs.
        if ctx.cancellation():
            raise ConnectorRefused(ERROR_CODE_CANCELLED, "poll was cancelled before it started")
        if self._root_is_symlink or self.root.is_symlink():
            raise ConnectorRefused(ERROR_CODE_INVALID_REQUEST, "root is a symlink")
        if cursor is not None and cursor.state_version not in self.supported_state_versions:
            raise ConnectorRefused(
                ERROR_CONNECTOR_STATE_INVALID,
                f"state version {cursor.state_version} is not supported; resync required",
            )
        previous = _decode_snapshot(cursor.payload) if cursor is not None else frozenset()
        witness_seq = 0 if cursor is None else cursor.witness_seq + 1

        files, failures, unresolved_paths = self._scan()
        if len(files) > self.max_files:
            raise ConnectorRefused(
                ERROR_CODE_SIZE_LIMIT_EXCEEDED,
                f"tree carries {len(files)} files over the {self.max_files} ceiling",
            )
        tree_bytes = sum(item.size for item in files)
        if tree_bytes > self.max_tree_bytes:
            raise ConnectorRefused(
                ERROR_CODE_SIZE_LIMIT_EXCEEDED,
                f"tree carries {tree_bytes} bytes over the {self.max_tree_bytes} ceiling",
            )

        if ctx.cancellation():
            raise ConnectorRefused(ERROR_CODE_CANCELLED, "poll was cancelled during the scan")

        observations: list[Observation] = []
        current_paths: set[str] = set()
        for scanned in files:
            current_paths.add(scanned.relative_path)
            observation, failure = self._read_item(scanned)
            if failure is not None:
                failures.append(failure)
                continue
            assert observation is not None
            observations.append(observation)

        # A path that failed structural validation this poll (symlink, escape,
        # special file, unreadable, too long) is present but unresolved -- not
        # confirmed missing. It is excluded from both the successor snapshot
        # and the deletion diff, so it neither reports as deleted now nor
        # silently reappears as a false "creation" once it resolves cleanly.
        known_paths = current_paths | unresolved_paths
        for missing_path in sorted(previous - known_paths):
            observations.append(
                Observation(
                    source_native_id=_native_id(missing_path),
                    source_locator=missing_path,
                    observed_at_us=0,
                    metadata_bytes=_METADATA_BYTES,
                    deletion_signal=DeletionSignal.EXPLICIT_DELETE,
                )
            )

        snapshot_payload = _encode_snapshot(tuple(current_paths))
        if len(snapshot_payload) > MAX_CURSOR_PAYLOAD_BYTES:
            raise ConnectorRefused(
                ERROR_CODE_SIZE_LIMIT_EXCEEDED,
                f"snapshot of {len(current_paths)} paths encodes to "
                f"{len(snapshot_payload)} bytes, over the {MAX_CURSOR_PAYLOAD_BYTES}-byte "
                "cursor ceiling",
            )
        successor = CursorState(
            state_version=self.state_version,
            payload=snapshot_payload,
            witness_seq=witness_seq,
            predecessor_digest=canonical_cursor_digest(
                CursorBinding(
                    workspace_id=ctx.workspace_id,
                    connector_id=self.connector_id,
                ),
                cursor
                or CursorState(
                    state_version=self.supported_state_versions[0],
                    payload=b"",
                    witness_seq=0,
                ),
            ),
        )
        yield Batch(
            observations=tuple(observations),
            successor_cursor=successor,
            item_failures=tuple(failures),
        )

    # --- internals -----------------------------------------------------------

    def _scan(self) -> tuple[list[_ScannedFile], list[ItemFailure], frozenset[str]]:
        """Recursively enumerate regular files in deterministic order.

        No symlink is ever followed, whether it names a directory or a file: a
        symlinked directory is reported as a failure and not descended into,
        and a symlinked file is reported as a per-item failure rather than
        read. Every relative path that fails a structural check here (rather
        than one entirely absent from the tree) is returned in the third
        element, so the caller can tell "confirmed missing" apart from
        "present but unresolved this poll".
        """
        files: list[_ScannedFile] = []
        failures: list[ItemFailure] = []
        unresolved: set[str] = set()
        entries_seen = 0

        def refuse_directory_error(error: OSError) -> None:
            del error
            raise ConnectorRefused(
                _ITEM_FAILURE_UNREADABLE,
                "a directory could not be enumerated completely",
            )

        for current_dir, dirnames, filenames in os.walk(
            self.root, followlinks=False, onerror=refuse_directory_error
        ):
            entries_seen += len(dirnames) + len(filenames)
            if entries_seen > self.max_files:
                raise ConnectorRefused(
                    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
                    f"tree carries more than {self.max_files} filesystem entries",
                )
            current = Path(current_dir)
            kept_dirnames: list[str] = []
            for name in sorted(dirnames):
                child = current / name
                if child.is_symlink():
                    relative = _relative_posix(self.root, child)
                    unresolved.add(relative)
                    failures.append(
                        _fail(
                            _native_id(relative),
                            _ITEM_FAILURE_SYMLINK_DIR,
                            "symlinked directories are not descended into",
                        )
                    )
                    continue
                kept_dirnames.append(name)
            dirnames[:] = kept_dirnames

            for name in sorted(filenames):
                absolute = current / name
                relative = _relative_posix(self.root, absolute)
                native_id = _native_id(relative)

                if len(relative) > self.max_path_length or len(relative) > MAX_LOCATOR_LENGTH:
                    unresolved.add(relative)
                    failures.append(
                        _fail(native_id, _ITEM_FAILURE_PATH_TOO_LONG, "path exceeds the ceiling")
                    )
                    continue
                if absolute.is_symlink():
                    unresolved.add(relative)
                    failures.append(
                        _fail(native_id, _ITEM_FAILURE_SYMLINK, "symlinks are not followed")
                    )
                    continue
                try:
                    resolved = absolute.resolve(strict=True)
                except OSError:
                    unresolved.add(relative)
                    failures.append(
                        _fail(native_id, _ITEM_FAILURE_UNREADABLE, "path could not be resolved")
                    )
                    continue
                if resolved != self.root / relative:
                    unresolved.add(relative)
                    failures.append(
                        _fail(native_id, _ITEM_FAILURE_ESCAPE, "path resolves outside the root")
                    )
                    continue
                try:
                    info = absolute.stat(follow_symlinks=False)
                except OSError:
                    unresolved.add(relative)
                    failures.append(
                        _fail(native_id, _ITEM_FAILURE_UNREADABLE, "path could not be stat'd")
                    )
                    continue
                if not stat.S_ISREG(info.st_mode):
                    unresolved.add(relative)
                    failures.append(
                        _fail(native_id, _ITEM_FAILURE_SPECIAL_FILE, "not a regular file")
                    )
                    continue
                files.append(
                    _ScannedFile(
                        relative_path=relative, absolute_path=absolute, size=info.st_size
                    )
                )
        files.sort(key=lambda item: item.relative_path)
        return files, failures, frozenset(unresolved)

    def _read_item(
        self, scanned: _ScannedFile
    ) -> tuple[Observation | None, ItemFailure | None]:
        native_id = _native_id(scanned.relative_path)
        suffix = Path(scanned.relative_path).suffix.lower()
        media_type = ACCEPTED_MEDIA_TYPES.get(suffix)
        if media_type is None:
            return None, _fail(
                native_id, _ITEM_FAILURE_UNSUPPORTED_TYPE, f"unsupported extension {suffix!r}"
            )
        if scanned.size > self.max_item_bytes:
            return None, _fail(
                native_id, ERROR_CODE_SIZE_LIMIT_EXCEEDED, "item exceeds the configured ceiling"
            )

        try:
            before = scanned.absolute_path.stat(follow_symlinks=False)
            raw = scanned.absolute_path.read_bytes()
            after = scanned.absolute_path.stat(follow_symlinks=False)
        except OSError:
            return None, _fail(native_id, _ITEM_FAILURE_UNREADABLE, "read failed")

        # Identity (device + inode), kind, mode and the two size/time facts
        # `read_bytes` bracketed: any of them moving means the bytes just read
        # cannot be trusted to be a single, consistent snapshot of the file.
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_mode != after.st_mode
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or before.st_size != after.st_size
            or not stat.S_ISREG(after.st_mode)
        ):
            return None, _fail(
                native_id, _ITEM_FAILURE_CHANGED, "file changed while it was being read"
            )
        if len(raw) != scanned.size:
            return None, _fail(
                native_id, _ITEM_FAILURE_CHANGED, "file changed while it was being read"
            )
        if b"\x00" in raw:
            return None, _fail(native_id, _ITEM_FAILURE_NUL_BYTE, "content carries a NUL byte")
        try:
            raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None, _fail(native_id, _ITEM_FAILURE_INVALID_UTF8, "content is not valid UTF-8")

        checksum = "sha256:" + hashlib.sha256(raw).hexdigest()
        observation = Observation(
            source_native_id=native_id,
            source_locator=scanned.relative_path,
            observed_at_us=int(after.st_mtime * 1_000_000),
            metadata_bytes=_METADATA_BYTES,
            content_checksum=checksum,
            media_type=media_type,
            deletion_signal=DeletionSignal.NONE,
            content=raw,
            source_event_at_us=int(after.st_mtime * 1_000_000),
        )
        return observation, None


__all__ = [
    "ACCEPTED_MEDIA_TYPES",
    "DECLARED_LIMITATIONS",
    "FilesystemSourceConnector",
]
