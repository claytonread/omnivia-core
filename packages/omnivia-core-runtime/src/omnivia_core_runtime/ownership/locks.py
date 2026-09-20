"""One lock interface with POSIX and Windows implementations (T-0629D, ADR-037).

Three lock roles, deliberately distinct because they grant different things:

- **bootstrap mutex** — short-lived launcher coordination. Any client may hold it
  to prevent duplicate service startup. It grants no storage ownership and no
  write authority.
- **takeover coordination** — held only while evaluating and performing a takeover,
  so two would-be successors cannot both decide they have won.
- **lifetime storage lock** — held by the owning service for the entire writable
  ownership lifetime. This is the one that, with the sole exclusive SQLite
  connection, constitutes storage ownership.

Everything fails closed. A lock that cannot be proven held is treated as not held.
"""

from __future__ import annotations

import json
import os
import platform
import stat
import tempfile
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Protocol, Self

IS_WINDOWS = platform.system() == "Windows"


class LockRole(str, Enum):
    """What a lock grants. Named so a caller cannot accidentally treat the
    bootstrap mutex as ownership."""

    BOOTSTRAP_MUTEX = "bootstrap_mutex"
    TAKEOVER_COORDINATION = "takeover_coordination"
    LIFETIME_STORAGE = "lifetime_storage"

    @property
    def grants_write_authority(self) -> bool:
        return self is LockRole.LIFETIME_STORAGE


class LockError(Exception):
    """A lock could not be acquired or its semantics could not be trusted."""


class LockUnavailable(LockError):
    """Another holder has the lock."""


class FileLock(Protocol):
    """The single lock interface both platforms implement."""

    @property
    def path(self) -> Path: ...

    @property
    def role(self) -> LockRole: ...

    @property
    def held(self) -> bool: ...

    def acquire(self, *, blocking: bool = False, timeout: float = 0.0) -> bool: ...

    def release(self) -> None: ...


@dataclass
class _LockState:
    handle: IO[bytes] | None = None
    held: bool = False


class _BaseFileLock:
    """Shared behaviour: the lock file, its payload and the context protocol.

    The payload is advisory diagnostics only. Ownership is decided by the OS lock,
    never by reading this file — a readable payload proves nothing about whether the
    writer still holds anything.
    """

    def __init__(
        self,
        path: Path,
        role: LockRole,
        payload: dict[str, object] | None = None,
        *,
        opened_handle: IO[bytes] | None = None,
    ) -> None:
        self._path = path
        self._role = role
        self._payload = payload or {}
        self._state = _LockState()
        self._opened_handle = opened_handle
        self._windows_path_api: Any | None = None
        self._windows_path_pins: list[object] = []

    @property
    def path(self) -> Path:
        return self._path

    @property
    def role(self) -> LockRole:
        return self._role

    @property
    def held(self) -> bool:
        return self._state.held

    def _open(self) -> IO[bytes]:
        if self._opened_handle is not None:
            handle = self._opened_handle
            self._opened_handle = None
            return handle
        if IS_WINDOWS:
            handle, api, pins = _open_windows_lock_file(self._path)
            self._windows_path_api = api
            self._windows_path_pins = pins
            return handle
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return open(self._path, "a+b")

    def _release_windows_path_pins(self) -> None:
        api = self._windows_path_api
        if api is not None:
            for pin in reversed(self._windows_path_pins):
                api.CloseHandle(pin)
        self._windows_path_pins.clear()
        self._windows_path_api = None

    def _write_payload(self) -> None:
        handle = self._state.handle
        if handle is None:  # pragma: no cover - guarded by callers
            return
        record = {
            "role": self._role.value,
            "pid": os.getpid(),
            **self._payload,
        }
        try:
            handle.seek(0)
            handle.truncate()
            handle.write((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        except OSError:  # pragma: no cover - diagnostics only
            pass

    def read_payload(self) -> dict[str, object] | None:
        """Advisory payload of the current or previous holder, if readable."""
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not text:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def __enter__(self) -> Self:
        if not self.acquire():
            raise LockUnavailable(
                f"{self._role.value} is held by another process: {self._path}"
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def acquire(self, *, blocking: bool = False, timeout: float = 0.0) -> bool:
        raise NotImplementedError

    def release(self) -> None:
        raise NotImplementedError


class PosixFileLock(_BaseFileLock):
    """`fcntl.flock` exclusive lock.

    flock is released automatically when the file descriptor closes or the process
    dies, which is the property takeover depends on: a crashed owner cannot keep
    the workspace locked forever.
    """

    def acquire(self, *, blocking: bool = False, timeout: float = 0.0) -> bool:
        import fcntl

        if self._state.held:
            return True
        handle = self._open()
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        deadline = timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), flags)
                break
            except OSError:
                if blocking or deadline <= 0:
                    handle.close()
                    self._release_windows_path_pins()
                    return False
                import time as _time

                _time.sleep(0.01)
                deadline -= 0.01
        self._state.handle = handle
        self._state.held = True
        self._write_payload()
        return True

    def release(self) -> None:
        import fcntl

        handle = self._state.handle
        if handle is None:
            self._state.held = False
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - platform dependent
            pass
        finally:
            handle.close()
            self._release_windows_path_pins()
            self._state.handle = None
            self._state.held = False


class WindowsFileLock(_BaseFileLock):  # pragma: no cover - exercised on Windows CI
    # `msvcrt` exists only on Windows, so a POSIX type-check cannot resolve its
    # members. The ignores are scoped to the three call sites rather than the file.
    """`msvcrt.locking` exclusive byte-range lock.

    Windows has no flock. A one-byte mandatory range lock is the closest
    equivalent; it is also released on process death, which is what takeover needs.
    """

    _RANGE = 1

    def acquire(self, *, blocking: bool = False, timeout: float = 0.0) -> bool:
        import msvcrt

        if self._state.held:
            return True
        handle = self._open()
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK  # type: ignore[attr-defined]
        deadline = timeout
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), mode, self._RANGE)  # type: ignore[attr-defined]
                break
            except OSError:
                if blocking or deadline <= 0:
                    handle.close()
                    self._release_windows_path_pins()
                    return False
                import time as _time

                _time.sleep(0.01)
                deadline -= 0.01
        self._state.handle = handle
        self._state.held = True
        self._write_payload()
        return True

    def release(self) -> None:
        import msvcrt

        handle = self._state.handle
        if handle is None:
            self._state.held = False
            return
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, self._RANGE)  # type: ignore[attr-defined]
        except OSError:
            pass
        finally:
            handle.close()
            self._release_windows_path_pins()
            self._state.handle = None
            self._state.held = False


def create_lock(
    path: Path,
    role: LockRole,
    payload: dict[str, object] | None = None,
    *,
    opened_handle: IO[bytes] | None = None,
) -> _BaseFileLock:
    """Platform-appropriate lock behind the one interface."""
    if IS_WINDOWS:  # pragma: no cover - selected on Windows CI
        return WindowsFileLock(path, role, payload, opened_handle=opened_handle)
    return PosixFileLock(path, role, payload, opened_handle=opened_handle)


# --- Filesystem qualification ------------------------------------------------


class FilesystemVerdict(str, Enum):
    """Whether a filesystem may be trusted for direct writable operation."""

    QUALIFIED = "qualified"
    REFUSED_REMOTE = "refused_remote"
    REFUSED_UNKNOWN = "refused_unknown"
    REFUSED_NO_LOCKING = "refused_no_locking"


#: Filesystems ADR-037 refuses for direct writable operation. Remote filesystems
#: without reliable cross-host locking must go through one networked Core Service.
REFUSED_FILESYSTEMS = frozenset(
    {
        "nfs",
        "nfs4",
        "smbfs",
        "cifs",
        "smb",
        "sshfs",
        "fuse.sshfs",
        "afpfs",
        "webdav",
        "ftp",
    }
)

#: Local filesystems with lock semantics this project has qualified.
#:
#: `ext2/ext3` is one label, not a pair of names: GNU coreutils prints that exact
#: string for statfs magic 0xEF53, which covers ext2, ext3 *and* ext4. So every
#: stock Linux box -- including a hosted Ubuntu runner, whose workspace is ext4 --
#: reports itself as `ext2/ext3` and never as `ext4`. Listing only the individual
#: names meant `stat -f -c %T` returned a value none of them matched and the
#: service refused a standard local filesystem.
#:
#: Membership is an exact match, deliberately. Accepting anything that merely
#: contains or starts with a qualified name would admit `ext2/ext3/ext4-fuse` and
#: every other filesystem whose name happens to embed one of these.
QUALIFIED_FILESYSTEMS = frozenset(
    {
        "apfs",
        "btrfs",
        "ext2",
        "ext2/ext3",
        "ext3",
        "ext4",
        "hfs",
        "ntfs",
        "overlay",
        "tmpfs",
        "xfs",
        "zfs",
    }
)


@dataclass(frozen=True)
class FilesystemQualification:
    """Result of qualifying a path for direct writable operation."""

    verdict: FilesystemVerdict
    filesystem: str
    reason: str

    @property
    def writable(self) -> bool:
        return self.verdict is FilesystemVerdict.QUALIFIED


def nearest_existing(path: Path) -> Path:
    """`path`, or the closest ancestor of it that exists.

    `path.parent` alone is not enough. A workspace root is created with
    `parents=True`, so a caller qualifying one *before* creating it can name several
    levels that are not there yet -- and probing a path that does not exist reports
    `"unknown"`, which default-deny then refuses. That would make the gate refuse
    every fresh workspace rather than every unqualified filesystem.
    """
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


def detect_filesystem(path: Path) -> str:
    """Best-effort filesystem type name for `path`, lowercased.

    Returns `"unknown"` rather than guessing. An unknown filesystem is refused for
    writable use, so a wrong guess here would be the difference between failing
    closed and corrupting a workspace.
    """
    system = platform.system()
    target = nearest_existing(path)
    try:
        if system == "Darwin":
            # `stat -f %T` on BSD reports the *file type* suffix (`/`, `@`), not the
            # filesystem type — the first implementation used it and every path came
            # back as "unknown", which the default-deny rule then refused. The
            # filesystem type comes from resolving the mount point and reading its
            # type out of `mount`.
            return _darwin_filesystem(target)
        if system == "Windows":
            # Windows has no `stat -f`, and until now this fell through to the
            # `"unknown"` below, so a Windows host could never qualify at all.
            return _windows_filesystem(target)
        if system == "Linux":
            import subprocess

            result = subprocess.run(
                ["stat", "-f", "-c", "%T", str(target)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            value = result.stdout.strip().lower()
            return value or "unknown"
    except Exception:  # noqa: BLE001  # pragma: no cover - any probe failure means unknown
        return "unknown"
    return "unknown"


def _darwin_filesystem(target: Path) -> str:
    """macOS filesystem type, via the mount point rather than `stat -f %T`.

    `df -P` gives the mount point; `mount` reports its type as the first field in
    the parenthesised list. Returns "unknown" on any doubt, which the default-deny
    rule then refuses.
    """
    import subprocess

    try:
        df = subprocess.run(
            ["df", "-P", str(target)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        lines = [line for line in df.stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            return "unknown"
        mount_point = lines[-1].split()[-1]

        mounts = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=5, check=False
        )
    except Exception:  # noqa: BLE001  # pragma: no cover - any failure means unknown
        return "unknown"

    for line in mounts.stdout.splitlines():
        # "/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, ...)"
        if f" on {mount_point} (" not in line:
            continue
        _, _, remainder = line.partition("(")
        first = remainder.split(",")[0].strip().rstrip(")").lower()
        return first or "unknown"
    return "unknown"


#: `GetDriveTypeW`'s DRIVE_REMOTE. A network volume reports the filesystem on the
#: *server*, which is no evidence at all about cross-host lock semantics, so its
#: name is never reported as if it were local.
_DRIVE_REMOTE = 4

#: MAX_PATH + 1. Both volume APIs document this as a sufficient output buffer.
_WINDOWS_BUFFER = 261


def _windows_filesystem(target: Path) -> str:
    """Windows filesystem type, read off the volume with `GetVolumeInformationW`.

    The name comes from the volume itself -- `ntfs`, `refs`, `exfat`, `fat32` --
    never from the fact that the host is Windows. Answering "ntfs" because
    `platform.system()` said Windows would make qualification a tautology: it would
    report success on a ReFS or exFAT workspace whose lock semantics this project
    has never qualified, which is the one thing the gate exists to prevent.

    `ctypes` is the standard library's route to the Win32 API, so this needs no
    dependency. Anything unexpected -- no kernel32, a failed call, a network
    volume, an empty name -- returns "unknown", which default-deny then refuses.
    """
    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        root = ctypes.create_unicode_buffer(_WINDOWS_BUFFER)
        # The volume mount point, which is what both following calls take. It is
        # not always `X:\`: a volume can be mounted on a directory.
        if not kernel32.GetVolumePathNameW(str(target), root, _WINDOWS_BUFFER):
            return "unknown"
        if kernel32.GetDriveTypeW(root.value) == _DRIVE_REMOTE:
            return "unknown"
        name = ctypes.create_unicode_buffer(_WINDOWS_BUFFER)
        # Volume label, serial, component length and flags are all passed NULL:
        # the filesystem name is the only output this needs.
        if not kernel32.GetVolumeInformationW(
            root.value, None, 0, None, None, None, name, _WINDOWS_BUFFER
        ):
            return "unknown"
    except Exception:  # noqa: BLE001 - any probe failure means unknown
        return "unknown"
    return str(name.value).strip().lower() or "unknown"


def qualify_filesystem(
    path: Path, *, filesystem: str | None = None, probe_locking: bool = True
) -> FilesystemQualification:
    """Decide whether `path` may host a directly writable workspace.

    Default-deny: anything not positively recognised as a qualified local
    filesystem is refused. ADR-037 requires direct writable operation to be refused
    when reliable locking cannot be provided, and "we did not recognise it" is not
    evidence that locking works.
    """
    # `is None` rather than truthiness: an explicitly supplied empty string means
    # "the filesystem could not be identified", which must be refused, not silently
    # replaced by auto-detection.
    name = (detect_filesystem(path) if filesystem is None else filesystem).lower()

    if any(
        name.startswith(refused) or refused in name for refused in REFUSED_FILESYSTEMS
    ):
        return FilesystemQualification(
            verdict=FilesystemVerdict.REFUSED_REMOTE,
            filesystem=name,
            reason=(
                f"{name} has no reliable cross-host lock semantics; use one "
                "networked Core Service instead of opening it directly"
            ),
        )

    if name not in QUALIFIED_FILESYSTEMS:
        return FilesystemQualification(
            verdict=FilesystemVerdict.REFUSED_UNKNOWN,
            filesystem=name,
            reason=(
                f"{name} is not a qualified filesystem; refusing direct writable "
                "operation rather than assuming its lock semantics"
            ),
        )

    if probe_locking and not _locking_works(path):
        return FilesystemQualification(
            verdict=FilesystemVerdict.REFUSED_NO_LOCKING,
            filesystem=name,
            reason=f"{name} did not honour an exclusive lock probe",
        )

    return FilesystemQualification(
        verdict=FilesystemVerdict.QUALIFIED,
        filesystem=name,
        reason=f"{name} is a qualified local filesystem and honoured a lock probe",
    )


def _locking_works(path: Path) -> bool:
    """Probe that an exclusive lock can be taken and released here.

    The probe is taken in the nearest existing directory rather than in `path`, so
    qualifying a workspace root that has not been created yet does not create it --
    and does not leave a probe file inside a tree the caller may still refuse. Its
    name is randomly generated and its descriptor is created exclusively, then
    handed directly to the lock implementation: no predictable pre-existing name
    can be opened, truncated, or unlinked through a symbolic link, and concurrent
    qualifications never contend for one shared probe.
    """
    existing = nearest_existing(path)
    directory = existing if existing.is_dir() else existing.parent
    handle: IO[bytes] | None = None
    try:
        probe, handle = _exclusive_lock_probe(directory)
        lock = create_lock(
            probe,
            LockRole.BOOTSTRAP_MUTEX,
            opened_handle=handle,
        )
        handle = None  # ownership transferred to the lock
        if not lock.acquire():
            return False
        lock.release()
        return True
    except (OSError, LockError):
        return False
    finally:
        if handle is not None:
            handle.close()


def _exclusive_lock_probe(directory: Path) -> tuple[Path, IO[bytes]]:
    """Create a probe whose still-open descriptor owns exact-object cleanup.

    POSIX unlinks the random name immediately while retaining the descriptor.
    Windows uses ``FILE_FLAG_DELETE_ON_CLOSE`` so the kernel deletes that exact
    file object when the lock closes it. Cleanup never resolves a pathname after
    releasing the descriptor.
    """
    if not IS_WINDOWS:
        descriptor, name = tempfile.mkstemp(
            prefix=".omnivia-lock-probe-", dir=directory
        )
        probe = Path(name)
        try:
            probe.unlink()
            return probe, os.fdopen(descriptor, "r+b", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise

    import ctypes  # pragma: no cover - exercised on the hosted Windows row
    import msvcrt  # pragma: no cover

    # FILE_FLAG_DELETE_ON_CLOSE requires DELETE access, and every later opener
    # must share deletion while this handle owns the exact-object cleanup.
    generic_read_write_delete = 0x80000000 | 0x40000000 | 0x00010000
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    create_new = 1
    temporary_delete_on_close = 0x00000100 | 0x04000000
    invalid = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
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
    for _attempt in range(16):
        probe = directory / f".omnivia-lock-probe-{uuid.uuid4().hex}"
        native = kernel32.CreateFileW(
            str(probe),
            generic_read_write_delete,
            share_read_write_delete,
            None,
            create_new,
            temporary_delete_on_close,
            None,
        )
        value = getattr(native, "value", native)
        if isinstance(value, int) and value not in (0, invalid):
            try:
                descriptor = msvcrt.open_osfhandle(  # type: ignore[attr-defined]
                    value, os.O_RDWR | getattr(os, "O_BINARY", 0)
                )
            except BaseException:
                kernel32.CloseHandle(native)
                raise
            try:
                return probe, os.fdopen(descriptor, "r+b", buffering=0)
            except BaseException:
                os.close(descriptor)
                raise
    raise OSError("could not create an exclusive filesystem lock probe")


def _open_windows_lock_file(path: Path) -> tuple[IO[bytes], Any, list[object]]:
    """Open/create one Windows lock and pin its whole namespace until release.

    Every parent is created or opened one component at a time without following a
    reparse point, then held without ``FILE_SHARE_DELETE``. The file is opened the
    same way and its descriptor identity is compared with the still-stable pathname
    before any payload is written. Later contenders therefore resolve the same
    namespace and exact file for the full lock lifetime.
    """
    import ctypes  # pragma: no cover - exercised on the hosted Windows row
    import msvcrt  # pragma: no cover

    generic_read_write = 0x80000000 | 0x40000000
    share_read_write = 0x00000001 | 0x00000002
    create_new = 1
    open_existing = 3
    open_reparse_point = 0x00200000
    backup_semantics = 0x02000000
    invalid = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
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
    pins: list[object] = []
    native: object | None = None
    descriptor = -1
    created_parent = False
    try:
        absolute_path = Path(os.path.abspath(os.fspath(path)))
        absolute_parent = absolute_path.parent
        for component in reversed((absolute_parent, *absolute_parent.parents)):
            try:
                before = os.lstat(component)
                component_created = False
            except FileNotFoundError:
                try:
                    component.mkdir(mode=0o700)
                except FileExistsError as failure:
                    # The name was absent when this call decided to create it. A
                    # concurrent winner is not silently adopted as our namespace.
                    raise OSError(
                        "Windows lock parent appeared while creating"
                    ) from failure
                before = os.lstat(component)
                component_created = True
                created_parent = True
            if not stat.S_ISDIR(before.st_mode) or (
                getattr(before, "st_file_attributes", 0) & 0x00000400
            ):
                raise OSError("Windows lock parent is not a real directory")
            parent_pin = kernel32.CreateFileW(
                str(component),
                0,
                0 if component_created else share_read_write,
                None,
                open_existing,
                open_reparse_point | backup_semantics,
                None,
            )
            parent_value = getattr(parent_pin, "value", parent_pin)
            if not isinstance(parent_value, int) or parent_value in (0, invalid):
                raise OSError("Windows lock parent could not be pinned")
            after = os.lstat(component)
            if (
                before.st_dev,
                before.st_ino,
                stat.S_IFMT(before.st_mode),
            ) != (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)) or (
                getattr(after, "st_file_attributes", 0) & 0x00000400
            ):
                kernel32.CloseHandle(parent_pin)
                raise OSError("Windows lock parent changed while opening")
            if component_created:
                # Creation modes do not establish a DACL on Windows. The
                # zero-share handle excludes a racing access-capable opener while
                # the exact directory is made owner-only; it remains the pin after
                # the repair, so there is no reopen window.
                from omnivia_core_runtime.ownership.discovery import (
                    restrict_to_owner,
                )

                try:
                    restrict_to_owner(component, directory=True)
                    secured = os.lstat(component)
                    if (
                        before.st_dev,
                        before.st_ino,
                        stat.S_IFMT(before.st_mode),
                    ) != (
                        secured.st_dev,
                        secured.st_ino,
                        stat.S_IFMT(secured.st_mode),
                    ):
                        raise OSError("Windows lock parent changed while securing")
                except BaseException:
                    kernel32.CloseHandle(parent_pin)
                    raise
            pins.append(parent_pin)

        try:
            before_file = os.lstat(absolute_path)
            if created_parent:
                raise OSError(
                    "Windows lock file appeared inside a newly created namespace"
                )
            created_file = False
        except FileNotFoundError:
            before_file = None
            created_file = True
        native = kernel32.CreateFileW(
            str(absolute_path),
            generic_read_write,
            0 if created_file else share_read_write,
            None,
            create_new if created_file else open_existing,
            open_reparse_point,
            None,
        )
        value = getattr(native, "value", native)
        if not isinstance(value, int) or value in (0, invalid):
            raise OSError("Windows lock file could not be opened safely")
        descriptor = msvcrt.open_osfhandle(  # type: ignore[attr-defined]
            value, os.O_RDWR | getattr(os, "O_BINARY", 0)
        )
        opened = os.fstat(descriptor)
        named = os.lstat(absolute_path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or (getattr(named, "st_file_attributes", 0) & 0x00000400)
            or (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
            != (named.st_dev, named.st_ino, stat.S_IFMT(named.st_mode))
            or (
                before_file is not None
                and (before_file.st_dev, before_file.st_ino)
                != (opened.st_dev, opened.st_ino)
            )
        ):
            raise OSError("Windows lock path is not the opened regular file")
        if created_file:
            from omnivia_core_runtime.ownership.discovery import restrict_to_owner

            restrict_to_owner(absolute_path, directory=False)
            secured = os.lstat(absolute_path)
            if (
                secured.st_dev,
                secured.st_ino,
                stat.S_IFMT(secured.st_mode),
                secured.st_nlink,
            ) != (
                opened.st_dev,
                opened.st_ino,
                stat.S_IFMT(opened.st_mode),
                1,
            ):
                raise OSError("Windows lock file changed while securing")
            os.close(descriptor)
            descriptor = -1
            native = None
            # The owner-only DACL now excludes an untrusted reopen. Reopen with
            # read/write sharing so a legitimate contender can reach the same
            # kernel byte-range lock and receive the normal busy result.
            before_file = secured
            native = kernel32.CreateFileW(
                str(absolute_path),
                generic_read_write,
                share_read_write,
                None,
                open_existing,
                open_reparse_point,
                None,
            )
            value = getattr(native, "value", native)
            if not isinstance(value, int) or value in (0, invalid):
                raise OSError("secured Windows lock file could not be reopened")
            descriptor = msvcrt.open_osfhandle(  # type: ignore[attr-defined]
                value, os.O_RDWR | getattr(os, "O_BINARY", 0)
            )
            reopened = os.fstat(descriptor)
            renamed = os.lstat(absolute_path)
            if (
                reopened.st_dev,
                reopened.st_ino,
                stat.S_IFMT(reopened.st_mode),
                reopened.st_nlink,
            ) != (
                before_file.st_dev,
                before_file.st_ino,
                stat.S_IFMT(before_file.st_mode),
                1,
            ) or (
                renamed.st_dev,
                renamed.st_ino,
                stat.S_IFMT(renamed.st_mode),
                renamed.st_nlink,
            ) != (
                before_file.st_dev,
                before_file.st_ino,
                stat.S_IFMT(before_file.st_mode),
                1,
            ):
                raise OSError("secured Windows lock path changed while reopening")
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        descriptor = -1
        return handle, kernel32, pins
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        elif native is not None:
            value = getattr(native, "value", native)
            if isinstance(value, int) and value not in (0, invalid):
                kernel32.CloseHandle(native)
        for pin in reversed(pins):
            kernel32.CloseHandle(pin)
        raise


__all__ = [
    "IS_WINDOWS",
    "QUALIFIED_FILESYSTEMS",
    "REFUSED_FILESYSTEMS",
    "FileLock",
    "FilesystemQualification",
    "FilesystemVerdict",
    "LockError",
    "LockRole",
    "LockUnavailable",
    "PosixFileLock",
    "WindowsFileLock",
    "create_lock",
    "detect_filesystem",
    "nearest_existing",
    "qualify_filesystem",
]
