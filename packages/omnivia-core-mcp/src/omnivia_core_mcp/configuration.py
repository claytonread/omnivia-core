"""Trusted, bounded configuration for the stdio MCP server.

The configuration file fixes the principal and the maximum authority an MCP
session may claim.  It is coordination data, not a credential store: remote
mode carries an opaque credential reference and never credential material.

Only explicit paths are accepted.  The reader opens a regular owner-private
file without following symlinks, verifies that the pathname and descriptor keep
the same identity throughout the bounded read, and then decodes one UTF-8 JSON
object.  All public failures are fixed, payload-free sentences.

"Owner-private" is proved from the open descriptor on both platform families:
the POSIX owner and mode bits, or on Windows a native owner and DACL proof that
the file's owner is this process's user and that no access-allowed ACE grants
anyone else.  Either proof fails closed.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import stat
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, NoReturn, Protocol

from omnivia_core_client import (
    ClientError,
    CredentialReference,
    parse_http_endpoint,
)

from omnivia_core.contracts.v1 import (
    IDENTIFIER_PATTERN,
    PURPOSE_PATTERN,
    WORKSPACE_ID_PATTERN,
)

__all__ = [
    "CONFIGURATION_FORMAT",
    "MAXIMUM_CONFIGURATION_BYTES",
    "McpConfiguration",
    "McpConfigurationError",
    "parse_configuration",
    "read_configuration",
]

CONFIGURATION_FORMAT: Final = "omnivia.mcp-config.v1"
MAXIMUM_CONFIGURATION_BYTES: Final = 65_536

_FIELDS: Final = frozenset(
    {
        "format",
        "principal_id",
        "allowed_workspace_ids",
        "default_workspace_id",
        "allowed_purposes",
        "mutation_enabled",
        "service_mode",
        "installation_state",
        "endpoint",
        "credential_reference",
    }
)
_IDENTIFIER_RE: Final = re.compile(IDENTIFIER_PATTERN)
_WORKSPACE_RE: Final = re.compile(WORKSPACE_ID_PATTERN)
_PURPOSE_RE: Final = re.compile(PURPOSE_PATTERN)
_IS_WINDOWS: Final = os.name == "nt"

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
_ALLOWED_ACE_SID_OFFSET: Final = 8
_MINIMUM_SID_BYTES: Final = 8


class McpConfigurationError(Exception):
    """A fixed-text refusal raised before MCP initialization."""


class _DuplicateMember(ValueError):
    pass


def _raise_file() -> NoReturn:
    raise McpConfigurationError(
        "the MCP configuration file is not a trusted owner-only file"
    )


def _raise_document() -> NoReturn:
    raise McpConfigurationError("the MCP configuration document is not valid")


def _raise_semantics() -> NoReturn:
    raise McpConfigurationError("the MCP configuration values are not admissible")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMember
        result[key] = value
    return result


def _invalid_constant(_value: str) -> NoReturn:
    raise ValueError


def _exact_text(value: object, pattern: re.Pattern[str], maximum: int) -> str | None:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        return None
    return value if pattern.fullmatch(value) is not None else None


def _unique_texts(
    value: object,
    *,
    maximum_items: int,
    maximum_length: int,
    pattern: re.Pattern[str],
) -> tuple[str, ...] | None:
    if type(value) is not list or not 1 <= len(value) <= maximum_items:
        return None
    accepted: list[str] = []
    for item in value:
        text = _exact_text(item, pattern, maximum_length)
        if text is None or text in accepted:
            return None
        accepted.append(text)
    return tuple(accepted)


def _validated_endpoint(value: object) -> str | None:
    if type(value) is not str:
        return None
    endpoint = None
    try:
        endpoint = parse_http_endpoint(value)
    except ClientError:
        pass
    return None if endpoint is None else endpoint.origin


def _validated_reference(value: object) -> CredentialReference | None:
    if type(value) is not str:
        return None
    reference = None
    try:
        reference = CredentialReference(value)
    except ClientError:
        pass
    return reference


@dataclass(frozen=True, slots=True, repr=False)
class McpConfiguration:
    """One validated, immutable MCP authority and service configuration."""

    format: str
    principal_id: str
    allowed_workspace_ids: tuple[str, ...]
    default_workspace_id: str | None
    allowed_purposes: tuple[str, ...]
    mutation_enabled: bool
    service_mode: Literal["managed_local", "service_client"]
    installation_state: Path | None
    endpoint: str | None
    credential_reference: CredentialReference | None

    def __post_init__(self) -> None:
        principal = _exact_text(self.principal_id, _IDENTIFIER_RE, 128)
        workspaces = _unique_texts(
            list(self.allowed_workspace_ids),
            maximum_items=128,
            maximum_length=128,
            pattern=_WORKSPACE_RE,
        )
        purposes = _unique_texts(
            list(self.allowed_purposes),
            maximum_items=32,
            maximum_length=128,
            pattern=_PURPOSE_RE,
        )
        invalid = (
            self.format != CONFIGURATION_FORMAT
            or principal is None
            or type(self.allowed_workspace_ids) is not tuple
            or workspaces is None
            or type(self.allowed_purposes) is not tuple
            or purposes is None
            or type(self.mutation_enabled) is not bool
            or self.service_mode not in ("managed_local", "service_client")
            or (
                self.default_workspace_id is not None
                and self.default_workspace_id not in (workspaces or ())
            )
        )
        if self.service_mode == "managed_local":
            invalid = invalid or (
                not isinstance(self.installation_state, Path)
                or not self.installation_state.is_absolute()
                or self.endpoint is not None
                or self.credential_reference is not None
            )
        elif self.service_mode == "service_client":
            invalid = invalid or (
                self.installation_state is not None
                or _validated_endpoint(self.endpoint) is None
                or not isinstance(self.credential_reference, CredentialReference)
            )
        if invalid:
            _raise_semantics()

    def __repr__(self) -> str:
        return "McpConfiguration(<redacted>)"

    @property
    def selected_workspace_id(self) -> str | None:
        """The unambiguous workspace for tools that carry no workspace selector."""
        if self.default_workspace_id is not None:
            return self.default_workspace_id
        if len(self.allowed_workspace_ids) == 1:
            return self.allowed_workspace_ids[0]
        return None


def parse_configuration(document: object) -> McpConfiguration:
    """Validate one already-decoded configuration object."""
    if type(document) is not dict:
        _raise_document()
    mapping = document
    if set(mapping) - _FIELDS:
        _raise_semantics()
    required = {
        "format",
        "principal_id",
        "allowed_workspace_ids",
        "allowed_purposes",
        "service_mode",
    }
    if not required <= set(mapping):
        _raise_semantics()

    principal = _exact_text(mapping.get("principal_id"), _IDENTIFIER_RE, 128)
    workspaces = _unique_texts(
        mapping.get("allowed_workspace_ids"),
        maximum_items=128,
        maximum_length=128,
        pattern=_WORKSPACE_RE,
    )
    purposes = _unique_texts(
        mapping.get("allowed_purposes"),
        maximum_items=32,
        maximum_length=128,
        pattern=_PURPOSE_RE,
    )
    default = mapping.get("default_workspace_id")
    if default is not None:
        default = _exact_text(default, _WORKSPACE_RE, 128)
    mutation = mapping.get("mutation_enabled", False)
    mode = mapping.get("service_mode")
    if (
        mapping.get("format") != CONFIGURATION_FORMAT
        or principal is None
        or workspaces is None
        or purposes is None
        or type(mutation) is not bool
        or mode not in ("managed_local", "service_client")
        or ("default_workspace_id" in mapping and default is None)
        or (default is not None and default not in workspaces)
    ):
        _raise_semantics()

    installation: Path | None = None
    endpoint: str | None = None
    reference: CredentialReference | None = None
    if mode == "managed_local":
        raw_installation = mapping.get("installation_state")
        if (
            type(raw_installation) is not str
            or not Path(raw_installation).is_absolute()
            or "endpoint" in mapping
            or "credential_reference" in mapping
        ):
            _raise_semantics()
        installation = Path(raw_installation)
    else:
        if "installation_state" in mapping:
            _raise_semantics()
        endpoint = _validated_endpoint(mapping.get("endpoint"))
        reference = _validated_reference(mapping.get("credential_reference"))
        if endpoint is None or reference is None:
            _raise_semantics()

    return McpConfiguration(
        format=CONFIGURATION_FORMAT,
        principal_id=principal,
        allowed_workspace_ids=workspaces,
        default_workspace_id=default,
        allowed_purposes=purposes,
        mutation_enabled=mutation,
        service_mode=mode,
        installation_state=installation,
        endpoint=endpoint,
        credential_reference=reference,
    )


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
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
) -> tuple[tuple[int, bool], ...] | None:
    """Describe each ACE as ``(type, grants the owner and nobody else)``.

    ``None`` reports an absent or NULL DACL, which grants everyone everything.
    """
    if not dacl:
        return None
    size = _AclSizeInformation()
    if not api.GetAclInformation(
        dacl, size, ctypes.sizeof(size), _ACL_SIZE_INFORMATION
    ):
        _raise_native()
    aces: list[tuple[int, bool]] = []
    for index in range(int(size.AceCount)):
        entry = ctypes.c_void_p()
        if not api.GetAce(dacl, index, entry) or not entry.value:
            _raise_native()
        header = _AceHeader.from_address(entry.value)
        kind = int(header.AceType)
        if kind != _ACCESS_ALLOWED_ACE_TYPE:
            aces.append((kind, False))
            continue
        if int(header.AceSize) < _ALLOWED_ACE_SID_OFFSET + _MINIMUM_SID_BYTES:
            _raise_native()
        sid = _sid_bytes(api, entry.value + _ALLOWED_ACE_SID_OFFSET)
        if int(header.AceSize) < _ALLOWED_ACE_SID_OFFSET + len(sid):
            _raise_native()
        aces.append((kind, sid == owner))
    return tuple(aces)


def _windows_acl_facts(
    descriptor: int,
) -> tuple[bool, tuple[tuple[int, bool], ...] | None]:
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
    try:
        if result:
            _raise_native()
        owner_sid = _sid_bytes(api, owner.value)
        user_sid = _token_user_sid(api)
        return owner_sid == user_sid, _dacl_aces(api, dacl.value, owner_sid)
    finally:
        if security.value:
            api.LocalFree(security)


def _owner_only_dacl(
    owner_matches: bool, aces: tuple[tuple[int, bool], ...] | None
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
        for kind, grants_owner in aces
    )


def _windows_owner_only(descriptor: int) -> bool:
    """Prove the open descriptor names a file only its owning user may reach."""
    return _owner_only_dacl(*_windows_acl_facts(descriptor))


def _lstat(path: Path) -> os.stat_result:
    failed = False
    value: os.stat_result | None = None
    try:
        value = path.lstat()
    except (OSError, ValueError):
        failed = True
    if failed or value is None:
        _raise_file()
    return value


def _open(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    failed = False
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        failed = True
    if failed or descriptor < 0:
        _raise_file()
    return descriptor


def _descriptor_stat(descriptor: int) -> os.stat_result:
    failed = False
    value: os.stat_result | None = None
    try:
        value = os.fstat(descriptor)
    except OSError:
        failed = True
    if failed or value is None:
        _raise_file()
    return value


def _private_file(value: os.stat_result, descriptor: int) -> bool:
    if not stat.S_ISREG(value.st_mode):
        return False
    if _IS_WINDOWS:
        verified = False
        try:
            verified = _windows_owner_only(descriptor)
        except Exception:  # noqa: BLE001 -- platform verifier must fail closed.
            verified = False
        return verified
    return value.st_uid == os.geteuid() and value.st_mode & 0o077 == 0


def _bounded_read(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = MAXIMUM_CONFIGURATION_BYTES + 1
    failed = False
    try:
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        failed = True
    if failed:
        _raise_file()
    return b"".join(chunks)


def _read_trusted_bytes(path: Path) -> bytes:
    before = _lstat(path)
    if not stat.S_ISREG(before.st_mode):
        _raise_file()
    descriptor = _open(path)
    try:
        opened = _descriptor_stat(descriptor)
        if not _same_file(before, opened) or not _private_file(opened, descriptor):
            _raise_file()
        content = _bounded_read(descriptor)
        after_read = _descriptor_stat(descriptor)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    after_path = _lstat(path)
    if not _same_file(opened, after_read) or not _same_file(opened, after_path):
        _raise_file()
    if len(content) > MAXIMUM_CONFIGURATION_BYTES:
        _raise_document()
    return content


def read_configuration(path: Path) -> McpConfiguration:
    """Read one explicit, trusted configuration file before MCP initialization."""
    if not isinstance(path, Path) or not path.is_absolute():
        _raise_file()
    content = _read_trusted_bytes(path)
    if content.startswith(b"\xef\xbb\xbf"):
        _raise_document()
    invalid = False
    document: object = None
    try:
        text = content.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_invalid_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        _DuplicateMember,
        TypeError,
        ValueError,
    ):
        invalid = True
    if invalid:
        _raise_document()
    return parse_configuration(document)
