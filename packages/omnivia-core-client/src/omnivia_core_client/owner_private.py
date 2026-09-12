"""Proving that an open file is one only its owning user can reach.

Two callers need the same proof and there is one of it here: the MCP adapter's
trusted configuration reader, which must not act on a document anyone else could
have written, and :mod:`~omnivia_core_client.installed_credentials`, which must
not hand out a bearer that anyone else could have substituted. The proof used to
live in the adapter; it is here because a second copy of a security check is a
second copy to keep correct, and the two would have drifted the first time one
was fixed.

**Nothing here raises.** :func:`read_owner_private` answers ``None`` for every
refusal -- an absent file, a symlink, a directory, a device, a file this process
does not own, one the group or world can reach, one whose identity changed under
the read, or an I/O failure at any point. That is deliberate: each caller has its
own fixed, payload-free sentence for "this file is not trustworthy", and a shared
exception type would either carry this module's words into both or tempt a caller
into rendering the path that failed. ``None`` carries nothing at all, and it
keeps this module clear of ``scripts/check-raise-discipline.py``'s subject matter
rather than merely compliant with it.

**The identity is checked three times, and the third is the one that matters.**
``lstat`` before the open refuses a symlink outright; ``O_NOFOLLOW`` refuses one
the open would otherwise have followed; and ``fstat`` on the descriptor, compared
against both the pre-open ``lstat`` and a post-read ``lstat`` of the same
pathname, refuses the case ``O_NOFOLLOW`` cannot see -- a pathname replaced
between the check and the read, or a component of it swapped underneath. The
bytes returned came from one file, and that file is the one the path named
throughout.

**Or, for a caller that says so, the third check is skipped rather than failed
closed.** ``rotation_tolerant=True`` is for exactly one shape of caller: a leaf
its own writer replaces by rename, inside a directory that caller reproves
unchanged around the whole operation. Nothing but this process can then have
moved the name, so a name that no longer matches the descriptor says nothing
against bytes already read from a handle nothing but this process could have
substituted -- the open descriptor's own proof stands alone. Off, which is the
default and every other caller, the comparison above still runs in full.

**"Owner-private" is proved on both platform families.** On POSIX it is the
descriptor's own ``st_uid`` against this process's effective uid, plus mode bits
with nothing set for group or world. On Windows there are no mode bits to read,
so it is a native owner and DACL proof from the open handle: the file's owner is
this process's user, and no access-allowed ACE grants anyone else. Either proof
fails closed, including when the native call itself does not complete.

**And on Windows the name is proved to be the thing, not a pointer at it.**
There is no ``O_NOFOLLOW`` on that platform, so a symbolic link, a junction or a
mount point would otherwise be followed silently -- by the open, and by every
directory check above it, since a junction *is* a directory to ``stat``. So the
reparse-point attribute is refused explicitly, on the file and on the directory
holding it, from the only stat that reports it; and because a directory there has
no descriptor to pin, :func:`owner_private_directory` adds the owner-and-DACL
proof by name and brackets it with a before-and-after identity check of its own.

Standard library only, and no import of any sibling distribution.
"""

from __future__ import annotations

import ctypes
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Final, NoReturn, Protocol

__all__ = [
    "not_a_reparse_point",
    "owner_private_chain",
    "owner_private_directory",
    "owner_private_directory_metadata",
    "owner_private_file",
    "owner_writable_only",
    "read_owner_private",
    "restrict_to_owner",
    "same_file",
    "write_owner_private",
]

_IS_WINDOWS: Final = os.name == "nt"

#: How much is read at once. A chunk size, not a bound: the bound is the
#: caller's ``maximum_bytes``, which this never reads past.
_CHUNK_BYTES: Final = 8192

# Win32 constants for the owner-only proof.  `_ALLOWED_ACE_SID_OFFSET` is
# `offsetof(ACCESS_ALLOWED_ACE, SidStart)`: a four-byte `ACE_HEADER` followed by
# a four-byte `ACCESS_MASK`.
_SE_FILE_OBJECT: Final = 1
_OWNER_SECURITY_INFORMATION: Final = 0x00000001
_DACL_SECURITY_INFORMATION: Final = 0x00000004
_TOKEN_QUERY: Final = 0x0008
_TOKEN_USER: Final = 1
_ACL_SIZE_INFORMATION: Final = 2
_ACCESS_ALLOWED_ACE_TYPE: Final = 0
_ACCESS_DENIED_ACE_TYPE: Final = 1
_ALLOWED_ACE_MASK_OFFSET: Final = 4
_ALLOWED_ACE_SID_OFFSET: Final = 8
_MINIMUM_SID_BYTES: Final = 8

#: The `icacls` rights :func:`restrict_to_owner` grants the owner alone -- the
#: repository's already hosted mechanism (see the Core Runtime distribution's
#: `ownership/discovery.py`, function `_restrict_windows`, and
#: `scripts/run-standard-journey.py`'s `_windows_owner_only`), repeated here
#: rather than imported, since this package may depend on neither. A directory
#: carries `(OI)(CI)` so anything created below it inherits this one entry
#: instead of whatever the parent would otherwise contribute -- the closest
#: Windows has to a POSIX `0o700` that also binds new children.
_WINDOWS_DIRECTORY_RIGHTS: Final = "(OI)(CI)F"
_WINDOWS_FILE_RIGHTS: Final = "F"

#: `whoami /user` reports the SID in this form, mixed into a CSV row.
_SID_RE: Final = re.compile(r"S-1-[0-9-]+")

#: Every access right that lets a holder change what a directory contains, or
#: change who may.
#:
#: A shared parent -- an installation root, ``runtime/`` -- is legitimately
#: *readable* by others on Windows, where the inherited profile ACL routinely
#: names SYSTEM and the local administrators beside the user. Refusing those
#: outright is the right policy for a directory holding bearers and the wrong one
#: for the two above it: it would refuse every real installation. What actually
#: makes a component substitutable is somebody else being able to write the name,
#: so the parent policy reads the mask and refuses exactly that.
_WRITE_ACCESS: Final = (
    0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)

#: The one Windows file attribute that makes a name stand for something other
#: than what it appears to be: a symbolic link, a directory junction, a mount
#: point, or any other reparse tag a filter driver claims. There is no
#: ``O_NOFOLLOW`` on that platform, so this attribute -- read from a
#: ``follow_symlinks=False`` stat, which is the only call that reports it -- is
#: what stands in its place, and it is checked on the file *and* on the directory
#: above it. Named from :mod:`stat`, which publishes the constant on every
#: platform, so the test for it is one branch rather than two.
_REPARSE_POINT: Final = stat.FILE_ATTRIBUTE_REPARSE_POINT


def not_a_reparse_point(metadata: os.stat_result) -> bool:
    """Whether this name is itself, rather than a redirection to somewhere else.

    Always true off Windows, where ``st_file_attributes`` does not exist and a
    final symlink is already refused by ``lstat`` and ``O_NOFOLLOW``. On Windows
    it is the whole of that defence: a junction or a mount point standing where a
    trusted directory belongs is a directory to every other check here, and this
    is the one that sees it is not the directory that was meant.

    The metadata must come from a ``follow_symlinks=False`` stat. One taken
    through a follow reports the attributes of the *target*, which is exactly the
    object this is trying not to be told about.
    """
    return not getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT


def same_file(first: os.stat_result, second: os.stat_result) -> bool:
    """Whether two stat results describe one and the same file."""
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
    )


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_uint16),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


class _SecurityApi(Protocol):
    def get_osfhandle(self, descriptor: int) -> int: ...

    def GetSecurityInfo(
        self,
        handle: int,
        kind: int,
        wanted: int,
        owner: object,
        group: object,
        dacl: object,
        sacl: object,
        security: object,
    ) -> int: ...

    def GetNamedSecurityInfoW(
        self,
        name: str,
        kind: int,
        wanted: int,
        owner: object,
        group: object,
        dacl: object,
        sacl: object,
        security: object,
    ) -> int: ...

    def GetCurrentProcess(self) -> int: ...

    def OpenProcessToken(self, process: int, access: int, token: object) -> int: ...

    def GetTokenInformation(
        self, token: object, kind: int, buffer: object, size: int, needed: object
    ) -> int: ...

    def IsValidSid(self, sid: int) -> int: ...

    def GetLengthSid(self, sid: int) -> int: ...

    def GetAclInformation(
        self, acl: int, information: object, size: int, kind: int
    ) -> int: ...

    def GetAce(self, acl: int, index: int, ace: object) -> int: ...

    def CloseHandle(self, handle: object) -> int: ...

    def LocalFree(self, memory: object) -> int: ...


class _WinSecurityApi:
    """The bound Win32 entry points, loaded only on a real Windows host."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("the Windows owner-only check is unavailable here")
        import msvcrt

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        address = ctypes.POINTER(ctypes.c_void_p)
        self.get_osfhandle = msvcrt.get_osfhandle  # type: ignore[attr-defined]
        self.GetSecurityInfo = advapi32.GetSecurityInfo
        self.GetSecurityInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            address,
            address,
            address,
            address,
            address,
        ]
        self.GetSecurityInfo.restype = wintypes.DWORD
        self.GetNamedSecurityInfoW = advapi32.GetNamedSecurityInfoW
        self.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            address,
            address,
            address,
            address,
            address,
        ]
        self.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.GetCurrentProcess = kernel32.GetCurrentProcess
        self.GetCurrentProcess.argtypes = []
        self.GetCurrentProcess.restype = wintypes.HANDLE
        self.OpenProcessToken = advapi32.OpenProcessToken
        self.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.OpenProcessToken.restype = wintypes.BOOL
        self.GetTokenInformation = advapi32.GetTokenInformation
        self.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.GetTokenInformation.restype = wintypes.BOOL
        self.IsValidSid = advapi32.IsValidSid
        self.IsValidSid.argtypes = [ctypes.c_void_p]
        self.IsValidSid.restype = wintypes.BOOL
        self.GetLengthSid = advapi32.GetLengthSid
        self.GetLengthSid.argtypes = [ctypes.c_void_p]
        self.GetLengthSid.restype = wintypes.DWORD
        self.GetAclInformation = advapi32.GetAclInformation
        self.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_AclSizeInformation),
            wintypes.DWORD,
            ctypes.c_int,
        ]
        self.GetAclInformation.restype = wintypes.BOOL
        self.GetAce = advapi32.GetAce
        self.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, address]
        self.GetAce.restype = wintypes.BOOL
        self.CloseHandle = kernel32.CloseHandle
        self.CloseHandle.argtypes = [wintypes.HANDLE]
        self.CloseHandle.restype = wintypes.BOOL
        self.LocalFree = kernel32.LocalFree
        self.LocalFree.argtypes = [wintypes.HANDLE]
        self.LocalFree.restype = wintypes.HANDLE


# Out-parameters below are passed as the ctypes instance rather than through
# `ctypes.byref`: a `POINTER(t)` argtype converts an instance of `t` by
# reference anyway, and it leaves `_SecurityApi` a seam a plain Python double
# can stand in for, so the ACE decoding is exercised off Windows too.
_SECURITY_API: _SecurityApi | None = None


def _security_api() -> _SecurityApi:
    global _SECURITY_API
    if _SECURITY_API is None:
        _SECURITY_API = _WinSecurityApi()
    return _SECURITY_API


def _raise_native() -> NoReturn:
    """Refuse the native proof itself; the caller turns this into a file refusal."""
    raise OSError("the Windows owner-only check did not complete")


def _sid_bytes(api: _SecurityApi, address: int | None) -> bytes:
    """Copy one validated SID out of native memory before that memory is freed."""
    if not address or not api.IsValidSid(address):
        _raise_native()
    length = int(api.GetLengthSid(address))
    if length <= 0:
        _raise_native()
    return ctypes.string_at(address, length)


def _token_user_sid(api: _SecurityApi) -> bytes:
    """Return the SID of the user this process runs as."""
    token = wintypes.HANDLE()
    if not api.OpenProcessToken(api.GetCurrentProcess(), _TOKEN_QUERY, token):
        _raise_native()
    try:
        needed = wintypes.DWORD()
        api.GetTokenInformation(token, _TOKEN_USER, None, 0, needed)
        size = int(needed.value)
        if size < ctypes.sizeof(_TokenUser):
            _raise_native()
        buffer = ctypes.create_string_buffer(size)
        if not api.GetTokenInformation(token, _TOKEN_USER, buffer, size, needed):
            _raise_native()
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        return _sid_bytes(api, user.User.Sid)
    finally:
        api.CloseHandle(token)


def _dacl_aces(
    api: _SecurityApi, dacl: int | None, owner: bytes
) -> tuple[tuple[int, bool, int], ...] | None:
    """Describe each ACE as ``(type, grants the owner, the rights it grants)``.

    ``None`` reports an absent or NULL DACL, which grants everyone everything.

    The mask is carried beside the verdict rather than judged here, because the
    two policies below want different things from it: the owner-only proof does
    not care what an outsider's ACE grants, and the parent proof cares about
    nothing else.
    """
    if not dacl:
        return None
    size = _AclSizeInformation()
    if not api.GetAclInformation(
        dacl, size, ctypes.sizeof(size), _ACL_SIZE_INFORMATION
    ):
        _raise_native()
    aces: list[tuple[int, bool, int]] = []
    for index in range(int(size.AceCount)):
        entry = ctypes.c_void_p()
        if not api.GetAce(dacl, index, entry) or not entry.value:
            _raise_native()
        header = _AceHeader.from_address(entry.value)
        kind = int(header.AceType)
        if kind != _ACCESS_ALLOWED_ACE_TYPE:
            aces.append((kind, False, 0))
            continue
        if int(header.AceSize) < _ALLOWED_ACE_SID_OFFSET + _MINIMUM_SID_BYTES:
            _raise_native()
        sid = _sid_bytes(api, entry.value + _ALLOWED_ACE_SID_OFFSET)
        if int(header.AceSize) < _ALLOWED_ACE_SID_OFFSET + len(sid):
            _raise_native()
        mask = ctypes.c_uint32.from_address(
            entry.value + _ALLOWED_ACE_MASK_OFFSET
        ).value
        aces.append((kind, sid == owner, int(mask)))
    return tuple(aces)


def _facts(
    api: _SecurityApi,
    result: int,
    owner: ctypes.c_void_p,
    dacl: ctypes.c_void_p,
    security: ctypes.c_void_p,
) -> tuple[bool, tuple[tuple[int, bool, int], ...] | None]:
    """Decode one answered ``Get*SecurityInfo`` call, releasing what it allocated.

    Written once over the *out-parameters* rather than once per entry point,
    because the handle form and the name form differ only in which call fills
    them in: everything that decides the verdict -- the owner SID, this process's
    token user, the ACE walk and the ``LocalFree`` -- is identical, and a second
    copy would be a second place for the release to go missing.
    """
    try:
        if result:
            _raise_native()
        owner_sid = _sid_bytes(api, owner.value)
        user_sid = _token_user_sid(api)
        return owner_sid == user_sid, _dacl_aces(api, dacl.value, owner_sid)
    finally:
        if security.value:
            api.LocalFree(security)


def _windows_acl_facts(
    descriptor: int,
) -> tuple[bool, tuple[tuple[int, bool, int], ...] | None]:
    """Read the owner verdict and the DACL shape from the open descriptor."""
    api = _security_api()
    handle = int(api.get_osfhandle(descriptor))
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    security = ctypes.c_void_p()
    result = api.GetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        owner,
        None,
        dacl,
        None,
        security,
    )
    return _facts(api, int(result), owner, dacl, security)


def _windows_acl_facts_by_name(
    path: Path,
) -> tuple[bool, tuple[tuple[int, bool, int], ...] | None]:
    """The same verdict for a *directory*, which there is no descriptor for here.

    A directory cannot be opened with ``os.open`` on Windows, so the security
    descriptor is read by name. That is weaker than the handle form on its own --
    a name can be re-pointed between two calls -- so it is never used on its own:
    :func:`owner_private_directory` refuses a reparse point before this runs and
    proves the directory's identity unchanged after it, and the material inside
    still carries its own handle-based proof.
    """
    api = _security_api()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    security = ctypes.c_void_p()
    result = api.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        owner,
        None,
        dacl,
        None,
        security,
    )
    return _facts(api, int(result), owner, dacl, security)


def _owner_only_dacl(
    owner_matches: bool, aces: tuple[tuple[int, bool, int], ...] | None
) -> bool:
    """The whole owner-only policy, over facts alone.

    A present DACL whose every access-allowed ACE grants the owner -- who is
    this process's user -- is owner-only.  Access-denied ACEs grant nothing and
    only narrow that.  Every other ACE form is unrecognised here and refused.
    """
    if not owner_matches or aces is None:
        return False
    return all(
        grants_owner
        if kind == _ACCESS_ALLOWED_ACE_TYPE
        else kind == _ACCESS_DENIED_ACE_TYPE
        for kind, grants_owner, _ in aces
    )


def _owner_writable_dacl(
    owner_matches: bool, aces: tuple[tuple[int, bool, int], ...] | None
) -> bool:
    """The parent policy: this user owns it and nobody else may write it.

    Weaker than :func:`_owner_only_dacl` in exactly one way -- an access-allowed
    ACE naming somebody else is admitted when it grants no right that could
    change what the directory holds. That is what a shared installation
    directory looks like on Windows, and refusing it would refuse every real
    installation; a directory holding the material itself still gets the
    owner-only proof in full.
    """
    if not owner_matches or aces is None:
        return False
    return all(
        (grants_owner or not mask & _WRITE_ACCESS)
        if kind == _ACCESS_ALLOWED_ACE_TYPE
        else kind == _ACCESS_DENIED_ACE_TYPE
        for kind, grants_owner, mask in aces
    )


def _windows_owner_only(descriptor: int) -> bool:
    """Prove the open descriptor names a file only its owning user may reach."""
    return _owner_only_dacl(*_windows_acl_facts(descriptor))


def _windows_directory_verdict(path: Path, *, owner_only: bool) -> bool:
    """One named directory against one of the two policies, failing closed.

    Unlike :func:`_windows_owner_only` this swallows the native refusal rather
    than raising it: every caller is a predicate that answers ``False`` for every
    reason a directory is not trustworthy, and a directory whose security
    descriptor could not be read is one of those reasons.
    """
    verified = False
    policy = _owner_only_dacl if owner_only else _owner_writable_dacl
    try:
        verified = policy(*_windows_acl_facts_by_name(path))
    except Exception:  # noqa: BLE001 -- platform verifier must fail closed.
        verified = False
    return verified


def _windows_owner_only_directory(path: Path) -> bool:
    return _windows_directory_verdict(path, owner_only=True)


def _system32(program: str) -> str:
    """An absolute path to a Windows system tool.

    Resolving through `PATH` would let any directory earlier on it supply the
    program that sets an object's ACL, which is backwards for a restriction.
    `SystemRoot` is where these live and is not always `C:\\Windows`.
    """
    return str(Path(os.environ.get("SystemRoot", "C:\\Windows"), "System32", program))


def _windows_restrict_to_owner(path: Path, *, directory: bool) -> bool:
    """Set `path`'s owner and DACL to this process's user alone, or answer ``False``.

    `icacls`, not `ctypes`. A prior version of this function called
    `SetNamedSecurityInfoW` directly. It passed every test written for it and
    failed on the one host that matters, because no host in this repository can
    exercise Windows: a wrong `ctypes` security call either does nothing or
    writes a security descriptor nobody intended, and both failures are silent.
    A wrong `icacls` argument is a non-zero exit this function can see. The cost
    is a process launch per command rather than one native call, paid once per
    object created, never in a loop.

    The sequence -- `/setowner`, `/reset`, then `/inheritance:r` with
    `/grant:r` -- and the SID it names are this repository's already hosted
    mechanism, repeated here rather than imported: see the Core Runtime
    distribution's `ownership/discovery.py`, function `_restrict_windows`, and
    `scripts/run-standard-journey.py`'s `_windows_owner_only`. `/setowner`
    first, because ownership comes from the token, not the DACL, and an
    elevated administrator's token makes it `BUILTIN\\Administrators` rather
    than this user. `/reset` drops whatever explicit entries an installer or a
    prior default left, which `/inheritance:r` alone would not touch;
    `/inheritance:r` together with `/grant:r` drops the inherited entries too,
    marks the DACL protected so a permissive parent cannot re-supply them, and
    leaves one allow ACE naming this process's own SID -- read from `whoami
    /user`'s closed CSV grammar, the same one this repository's other Windows
    SID readers use, rather than from a `ctypes` token query.

    `directory` is why this takes it rather than asking the filesystem: the
    rights an owner-only object needs differ by kind -- `(OI)(CI)F` so a
    directory's children inherit this one entry, or plain `F` for a file, which
    has none -- and the caller that just created the object already knows which
    one it made.

    Every step runs in order and the first failure ends the sequence: a
    non-zero `icacls` exit, a SID this call could not read, or any exception at
    all, including one raised by `subprocess` or while decoding a tool's
    output. Nothing about a failure is kept -- not the path, the SID, the
    command, or what either tool wrote to its own streams. A caller that cannot
    restrict an object it just created has no safe way to use it, and no
    diagnostic here is worth the risk of one of those reaching a log or a
    traceback.
    """
    rights = _WINDOWS_DIRECTORY_RIGHTS if directory else _WINDOWS_FILE_RIGHTS
    try:
        identity = subprocess.run(
            [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        found = _SID_RE.search(identity.stdout) if identity.returncode == 0 else None
        if found is None:
            return False
        sid = found.group()
        for arguments in (
            ("/setowner", f"*{sid}"),
            ("/reset",),
            ("/inheritance:r", "/grant:r", f"*{sid}:{rights}"),
        ):
            completed = subprocess.run(
                [_system32("icacls.exe"), str(path), *arguments, "/q"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                return False
    except Exception:  # noqa: BLE001 -- platform writer must fail closed.
        return False
    return True


def restrict_to_owner(path: Path, *, directory: bool) -> bool:
    """Make sure nobody but this process's user can reach an object just created.

    A no-op success off Windows, where the mode already given to ``mkdir`` or
    ``mkstemp`` made that true at the instant of creation and there is nothing
    further to enforce. There are no mode bits on Windows -- creation there
    hands the object whatever DACL its parent's inheritance and the caller's
    token supply, which an installation root or ``runtime/`` may legitimately
    have widened for SYSTEM or the local administrators, and which an elevated
    token can make owned by ``BUILTIN\\Administrators`` rather than this
    process's own user. This is what stands in place of the mode argument there:
    called once, immediately after creation and before a byte is written into the
    object or a child is created below it, it sets the owner and the DACL
    explicitly rather than trusting either -- and it fails closed, so an object
    this could not restrict is never treated as restricted merely because a
    later read-only proof happened to find the host's inherited defaults narrow
    enough by chance.

    `directory` is required rather than inferred from `path`: every call site
    already knows what kind of object it just created, and asking the
    filesystem again would be asking a name a racing attacker can move between
    the two calls.
    """
    if not _IS_WINDOWS:
        return True
    return _windows_restrict_to_owner(path, directory=directory)


def owner_private_file(metadata: os.stat_result, descriptor: int) -> bool:
    """Whether this open descriptor names a regular file only its owner can reach.

    The descriptor rather than the pathname is what is judged, on both platform
    families, because a pathname can be re-pointed between a check and a use and
    an open descriptor cannot.
    """
    if not stat.S_ISREG(metadata.st_mode) or not not_a_reparse_point(metadata):
        return False
    if _IS_WINDOWS:
        verified = False
        try:
            verified = _windows_owner_only(descriptor)
        except Exception:  # noqa: BLE001 -- platform verifier must fail closed.
            verified = False
        return verified
    return metadata.st_uid == os.geteuid() and metadata.st_mode & 0o077 == 0


def _lstat(path: Path, dir_fd: int | None) -> os.stat_result | None:
    value: os.stat_result | None = None
    try:
        value = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except (OSError, ValueError):
        value = None
    return value


def _open(path: Path, dir_fd: int | None) -> int:
    """Open for reading without following a final symlink, or return ``-1``."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, dir_fd=dir_fd)
    except (OSError, ValueError):
        descriptor = -1
    return descriptor


def _fstat(descriptor: int) -> os.stat_result | None:
    value: os.stat_result | None = None
    try:
        value = os.fstat(descriptor)
    except OSError:
        value = None
    return value


def _bounded_read(descriptor: int, maximum_bytes: int) -> bytes | None:
    """At most ``maximum_bytes``, or ``None`` if the read itself failed.

    Reading past the bound is never attempted, so a caller that wants to tell a
    file *at* its bound from one *past* it asks for one byte more than it will
    accept and compares the length.
    """
    chunks: list[bytes] = []
    remaining = maximum_bytes
    failed = False
    try:
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, _CHUNK_BYTES))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        failed = True
    return None if failed else b"".join(chunks)


def read_owner_private(
    path: Path,
    *,
    maximum_bytes: int,
    dir_fd: int | None = None,
    rotation_tolerant: bool = False,
) -> bytes | None:
    """At most ``maximum_bytes`` from one owner-private regular file, or ``None``.

    ``None`` is every refusal there is -- see this module's docstring. The caller
    decides what to say about it.

    ``dir_fd`` is the same proof, anchored. With it, `path` is a single relative
    name the *kernel* resolves against that already-open directory, so no
    component above the file is resolved through a pathname this process composed
    and no parent can be re-pointed between the check and the open -- the caller
    holds the directory. Without it, `path` is an absolute pathname and the
    identity re-check below is the whole of the substitution defence. The two
    differ in what they can prevent, not in what they prove about the file.

    ``rotation_tolerant`` is for exactly one shape of caller:
    :mod:`~omnivia_core_client.installed_credentials`, whose leaves are replaced
    by their own writer's rename, inside a directory that caller reproves
    unchanged around the whole operation. Off, the default, a pathname that no
    longer identifies the descriptor opened -- before the open or after the
    read -- is refused exactly like a substitution, because without an
    independent proof of the directory the two cannot be told apart. On, that
    comparison is dropped and the open descriptor's own proof -- real,
    owner-private, not a reparse point, stable through the bounded read --
    stands alone: the caller's own directory-unchanged proof already establishes
    that nothing but this process could have moved the name, so a name that has
    moved on says nothing against bytes already read from a handle nothing but
    this process could have substituted. A generic caller with no such proof of
    its own must leave this off.
    """
    if dir_fd is None:
        if not isinstance(path, Path) or not path.is_absolute():
            return None
    elif not isinstance(path, Path) or len(path.parts) != 1 or not path.name:
        return None
    before = _lstat(path, dir_fd)
    if (
        before is None
        or not stat.S_ISREG(before.st_mode)
        or not not_a_reparse_point(before)
    ):
        return None
    descriptor = _open(path, dir_fd)
    if descriptor < 0:
        return None
    content: bytes | None = None
    opened: os.stat_result | None = None
    after_read: os.stat_result | None = None
    try:
        opened = _fstat(descriptor)
        if (
            opened is not None
            and (rotation_tolerant or same_file(before, opened))
            and owner_private_file(opened, descriptor)
        ):
            content = _bounded_read(descriptor, maximum_bytes)
            after_read = _fstat(descriptor)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if content is None or opened is None or after_read is None:
        return None
    if not same_file(opened, after_read):
        return None
    if rotation_tolerant:
        return content
    after_path = _lstat(path, dir_fd)
    if after_path is None or not not_a_reparse_point(after_path):
        return None
    if not same_file(opened, after_path):
        return None
    return content


#: What a directory this module has to bring into existence is created with, and
#: what it is created with at ``mkdir`` rather than by a later ``chmod`` -- there
#: is no instant at which it is wider than this.
_NEW_DIRECTORY_MODE: Final = 0o700

#: The suffix a half-written file carries while it is still half-written. It is
#: never the destination name, so a reader that sees the destination sees a whole
#: document or nothing.
_PARTIAL_SUFFIX: Final = ".partial"


def _write_all(descriptor: int, content: bytes) -> bool:
    """Put the whole of `content` on `descriptor`, or say it could not be put.

    ``os.write`` is the raw system call: it may accept less than it was handed
    and reports that only in its return value, which raises nothing. Writing once
    and moving on would rename a *truncated* document over the destination -- a
    file that is atomic, owner-private, fresh and wrong.
    """
    written = 0
    while written < len(content):
        progress = os.write(descriptor, content[written:])
        if progress <= 0:
            return False
        written += progress
    return True


def write_owner_private(path: Path, content: bytes) -> bool:
    """Replace `path` with `content`, atomically and owner-private from creation.

    ``False`` for every refusal, and nothing raised, for the reason the rest of
    this module answers ``None``: the caller has its own fixed, payload-free
    sentence for "this could not be written safely", and a path is exactly what
    such a sentence must not carry.

    Three properties, in the order they matter:

    * **The directory is proved before anything is created in it.**
      :func:`owner_private_directory` refuses a symlink, a junction, a
      non-directory and -- on POSIX -- anything group or world can reach. A
      missing directory is created at :data:`_NEW_DIRECTORY_MODE` and then proved
      like any other, so a directory this call made gets no more trust than one
      it found.
    * **The file is owner-private before a byte is written.** ``mkstemp``
      creates with ``O_EXCL`` and mode ``0o600`` in one call -- there is no
      instant at which the file exists and is readable, and no name an attacker
      could have pre-created as a symlink or a reparse point. On Windows that
      mode is not a promise the filesystem keeps, so :func:`restrict_to_owner`
      sets the owner and the DACL explicitly there, before a byte is written,
      rather than trusting what creation happened to inherit. Either way the
      proof is then taken from the open descriptor by :func:`owner_private_file`,
      which is the POSIX owner-and-mode check or the Windows owner-and-DACL one.
    * **Publication is one rename.** A concurrent reader sees the previous
      document or this one and never a partial one, and a failure at any point
      removes the temporary rather than leaving it behind.

    :mod:`~omnivia_core_client.installed_credentials` keeps its own
    descriptor-anchored form of this rather than calling it: the store holds
    bearers, walks down to its directory one component at a time and holds those
    descriptors open for the whole operation, which is a stronger property than a
    pathname can offer and not one this general writer provides.
    """
    if not isinstance(path, Path) or not path.is_absolute() or not path.name:
        return False
    directory = path.parent
    if not owner_private_directory(directory):
        try:
            directory.mkdir(parents=True, mode=_NEW_DIRECTORY_MODE)
        except OSError:
            return False
        if not restrict_to_owner(
            directory, directory=True
        ) or not owner_private_directory(directory):
            return False
    descriptor, temporary = -1, ""
    failed = False
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=str(directory), suffix=_PARTIAL_SUFFIX
        )
        failed = not restrict_to_owner(Path(temporary), directory=False)
        if not failed:
            metadata = os.fstat(descriptor)
            failed = not owner_private_file(metadata, descriptor) or not _write_all(
                descriptor, content
            )
        if not failed:
            os.fsync(descriptor)
    except (OSError, ValueError):
        failed = True
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not failed:
        try:
            os.replace(temporary, path)
        except OSError:
            failed = True
    if failed and temporary:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return not failed


def owner_private_directory(path: Path) -> bool:
    """Whether `path` is a real directory that only its owning user can reach.

    ``lstat`` rather than ``stat``, so a symlink standing where the directory
    should be is refused for being a symlink rather than followed and judged on
    whatever it points at. That is the whole of the path-substitution defence one
    level up from the file: :func:`read_owner_private` proves the *file* was not
    re-pointed, and this proves the directory it was found in was not either.

    On Windows there are no mode bits and no cheap handle to a directory here, so
    this asks only that a directory is what is there. That is not a weaker
    promise about the material: on that platform the material's own file carries
    a native owner-and-DACL proof, read from its open handle, which is the check
    that decides whether it is used.
    """
    before = _lstat(path, None)
    if before is None or not owner_private_directory_metadata(before):
        return False
    if not _IS_WINDOWS:
        return True
    if not _windows_owner_only_directory(path):
        return False
    # The name form has no descriptor to pin, so the identity is pinned around
    # it instead: the directory that answered the security call is the one this
    # name resolved to before it and still resolves to after it.
    after = _lstat(path, None)
    return (
        after is not None
        and owner_private_directory_metadata(after)
        and same_file(before, after)
    )


def owner_private_directory_metadata(metadata: os.stat_result) -> bool:
    """The policy :func:`owner_private_directory` applies, over metadata alone.

    Split out because the same verdict has to be reached about a directory held
    open -- ``fstat`` on a descriptor, where there is no pathname left to
    re-point -- and one policy read from two places is one policy to keep right.
    """
    if not stat.S_ISDIR(metadata.st_mode) or not not_a_reparse_point(metadata):
        return False
    if _IS_WINDOWS:
        # Windows has no mode bits and this is metadata alone, so what is left
        # here is the kind check and the reparse refusal above. The owner and
        # DACL half of the same verdict needs a security call, which
        # :func:`owner_private_directory` makes by name; a caller holding only
        # metadata -- an already-open directory descriptor's ``fstat``, which is
        # the anchored POSIX walk and does not arise on this platform -- gets the
        # part that can be answered from what it has.
        return True
    return metadata.st_uid == os.geteuid() and metadata.st_mode & 0o077 == 0


def owner_writable_only(metadata: os.stat_result) -> bool:
    """Whether a directory on the way down is one nobody else can rearrange.

    Not the owner-only proof: an installation root and its ``runtime/`` are
    shared, conventionally-moded installation directories, so who may *read*
    them changes nothing about the material two levels down. What does matter is
    who may write them, because that is the whole of a swap: a group- or
    world-writable parent is a directory in which anyone can replace ``runtime``
    with a symlink.

    On Windows the mode bits say nothing, so what is answerable from metadata
    alone is the kind and the reparse refusal; the owner-and-DACL half needs a
    security call, which :func:`owner_private_chain` makes by name.
    """
    if not stat.S_ISDIR(metadata.st_mode) or not not_a_reparse_point(metadata):
        return False
    if _IS_WINDOWS:
        return True
    return metadata.st_uid == os.geteuid() and metadata.st_mode & 0o022 == 0


def owner_private_chain(
    root: Path, names: Sequence[str], *, owner_private_leaf: bool = True
) -> tuple[os.stat_result, ...] | None:
    """Prove every component from `root` down to ``root/*names``, or ``None``.

    The pathname form of the descriptor-anchored walk, for the one platform that
    has no ``O_DIRECTORY``, no ``O_NOFOLLOW`` and no ``dir_fd``. Each component
    must be a real directory that is not a symlink or a reparse point; each
    parent must be owned by this process's user and writable by nobody else; the
    last one must be owner-private in full. On Windows each also carries the
    native owner-and-DACL proof, which is the only thing that can be read there.

    `owner_private_leaf` is ``False`` for a caller proving a chain it is about to
    create the *next* component below: the last component is then a parent like
    the ones above it and gets the parent policy, not the owner-only one. Without
    it a caller creating ``runtime/`` would have to prove the shared installation
    root owner-only, which no real installation is.

    It returns the ``lstat`` of each proved component rather than a bare verdict,
    because a pathname proof is worth nothing on its own: the caller operates and
    then asks again, and compares the two with :func:`same_file`. Without that
    second half, a parent swapped between the proof and the operation would have
    been proved and then not used.
    """
    if not isinstance(root, Path) or not root.is_absolute():
        return None
    proved: list[os.stat_result] = []
    current = root
    for index in range(len(names) + 1):
        if index:
            current = current / names[index - 1]
        metadata = _lstat(current, None)
        if metadata is None:
            return None
        leaf = owner_private_leaf and index == len(names)
        admitted = (
            owner_private_directory_metadata(metadata)
            if leaf
            else owner_writable_only(metadata)
        )
        if not admitted:
            return None
        if _IS_WINDOWS and not _windows_directory_verdict(current, owner_only=leaf):
            return None
        proved.append(metadata)
    return tuple(proved)
