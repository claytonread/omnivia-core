"""Safe installation-local discovery of a live Core service endpoint.

A descriptor is coordination data, never authority.  This module derives its one
accepted path from a trusted installation-state root and a validated workspace
identifier, checks native file provenance before decoding a bounded document,
refuses any endpoint that is not this platform's local IPC endpoint, composes
the accepted compatibility negotiation, and then asks the injected transport for
``service.discover``.  Only exact workspace and service-instance identity
agreement turns the coordination hint into a discovered endpoint.

There is deliberately no concrete transport, path registry, home-directory
fallback, retry, process management, workspace-storage access, or authority
claim here.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import stat
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn, cast

from omnivia_core.contracts.v1 import (
    WORKSPACE_ID_PATTERN,
    ContractDecodeError,
    ContractSemanticError,
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProbeResult,
    ServiceProcessEvidence,
    decode_service_endpoint_descriptor,
    decode_service_probe_result,
)
from omnivia_core_client.compatibility import NegotiatedEndpoint, negotiate_endpoint
from omnivia_core_client.deadline import (
    MAXIMUM_DURATION_MS,
    CancellationToken,
    Deadline,
)
from omnivia_core_client.errors import (
    CompatibilityError,
    DeadlineExceededError,
    OperationCancelledError,
    ProtocolError,
    TransportError,
)
from omnivia_core_client.transport import ClientTransport, enforce_send_preconditions

__all__ = [
    "MAXIMUM_DESCRIPTOR_BYTES",
    "DiscoveredEndpoint",
    "descriptor_path",
    "discover_endpoint",
]

MAXIMUM_DESCRIPTOR_BYTES: Final = 64 * 1024
"""Largest accepted UTF-8 descriptor document, in bytes.

The file size is checked before the first content read and the read itself stops
at one byte beyond this value, so a concurrent grow cannot turn the check into an
unbounded allocation.
"""

_DESCRIPTOR_NAME: Final = "service.json"
_RUNTIME_DIRECTORY: Final = "runtime"
_DISCOVER_PROBE: Final = "service.discover"
_WORKSPACE_ID_MAX: Final = 128
_WORKSPACE_ID_RE: Final = re.compile(WORKSPACE_ID_PATTERN)

_LOCAL_IPC_SCHEMES: Final = {"posix": "unix://", "nt": "pipe://"}
"""The one installation-local IPC scheme this client dials, per platform.

Spelled exactly as the public ``ServiceEndpointUri`` pattern spells them, so this
narrows that policy rather than restating it: no scheme is admitted here that the
public descriptor decoder did not already admit.
"""


@dataclass(frozen=True, slots=True)
class DiscoveredEndpoint:
    """One provenance-checked descriptor and its accepted version agreement."""

    descriptor: ServiceEndpointDescriptor
    negotiated: NegotiatedEndpoint


def _require_workspace_id(workspace_id: object) -> str:
    if (
        not isinstance(workspace_id, str)
        or not 1 <= len(workspace_id) <= _WORKSPACE_ID_MAX
        or _WORKSPACE_ID_RE.fullmatch(workspace_id) is None
    ):
        raise ValueError("workspace_id must be a valid public WorkspaceId")
    return workspace_id


def descriptor_path(installation_state: Path, workspace_id: str) -> Path:
    """Return the only descriptor path P1b accepts.

    ``installation_state`` is supplied explicitly and treated as the trusted
    installation root.  No caller-selected descriptor path, glob, environment
    lookup, or home fallback exists.
    """
    identifier = _require_workspace_id(workspace_id)
    return Path(installation_state) / _RUNTIME_DIRECTORY / identifier / _DESCRIPTOR_NAME


def _raise_provenance() -> NoReturn:
    raise TransportError("descriptor provenance check failed")


def _raise_access() -> NoReturn:
    raise TransportError("descriptor could not be read safely")


def _raise_size() -> NoReturn:
    raise ProtocolError("descriptor document exceeds the size bound")


def _raise_document() -> NoReturn:
    raise ProtocolError("descriptor document is not an accepted public descriptor")


def _raise_endpoint_locality() -> NoReturn:
    raise TransportError(
        "descriptor endpoint is not this platform's local IPC endpoint"
    )


def _raise_live_transport() -> NoReturn:
    raise TransportError("live discovery call did not complete")


def _raise_live_deadline() -> NoReturn:
    raise DeadlineExceededError(
        f"{_DISCOVER_PROBE}: the deadline for this call has passed"
    )


def _raise_live_cancelled() -> NoReturn:
    raise OperationCancelledError(f"{_DISCOVER_PROBE}: cancelled before it completed")


def _raise_live_discovery() -> NoReturn:
    raise TransportError("live discovery did not return an endpoint descriptor")


def _raise_live_identity() -> NoReturn:
    raise TransportError("descriptor and live identity do not agree")


def _require_local_endpoint(descriptor: ServiceEndpointDescriptor) -> None:
    """Refuse an endpoint this installation-local client has no business dialing.

    ``decode_service_endpoint_descriptor`` has already run, and it answers a
    different question: whether a URI is safe for a service to *publish* before
    anyone has authenticated. That policy is shared with every publisher and
    reader of the descriptor, and it admits remote HTTP endpoints and both
    platforms' local IPC schemes by design.

    Discovery here is installation-local by definition -- one trusted state root,
    one workspace directory, one file -- so the only endpoint it may connect to is
    this platform's local IPC endpoint. A remote URI reached through a local
    coordination file is the descriptor being used as authority, which it is not.
    """
    scheme = _LOCAL_IPC_SCHEMES.get(os.name)
    endpoint_uri = descriptor.endpoint_uri
    if (
        scheme is None
        or type(endpoint_uri) is not str
        or not endpoint_uri.startswith(scheme)
    ):
        _raise_endpoint_locality()


def _negotiate_descriptor(
    descriptor: ServiceEndpointDescriptor,
) -> NegotiatedEndpoint:
    negotiated: NegotiatedEndpoint | None = None
    try:
        negotiated = negotiate_endpoint(descriptor)
    except CompatibilityError:
        incompatible = True
    else:
        incompatible = False
    if incompatible or negotiated is None:
        raise CompatibilityError("descriptor compatibility check failed")
    return negotiated


def _live_descriptor_wire(descriptor: ServiceEndpointDescriptor) -> dict[str, Any]:
    if type(descriptor) is not ServiceEndpointDescriptor:
        raise TypeError
    process = descriptor.process
    if process is not None and type(process) is not ServiceProcessEvidence:
        raise TypeError

    wire: dict[str, Any] = {
        "descriptor_version": descriptor.descriptor_version,
        "workspace_id": descriptor.workspace_id,
        "service_instance_id": descriptor.service_instance_id,
        "installation_id": descriptor.installation_id,
        "endpoint_uri": descriptor.endpoint_uri,
        "protocol_version": descriptor.protocol_version,
        "server_version": descriptor.server_version,
        "supported_api_versions": {
            "minimum": descriptor.supported_api_versions.minimum,
            "maximum": descriptor.supported_api_versions.maximum,
        },
        "supported_workspace_versions": {
            "minimum": descriptor.supported_workspace_versions.minimum,
            "maximum": descriptor.supported_workspace_versions.maximum,
        },
        "workspace_format_version": descriptor.workspace_format_version,
        "ready": descriptor.ready,
        "lifecycle_state": descriptor.lifecycle_state,
        "fencing_generation": descriptor.fencing_generation,
        "published_at": descriptor.published_at,
    }
    if process is not None:
        wire["process"] = {
            "pid": process.pid,
            "start_time": process.start_time,
            "boot_id": process.boot_id,
        }
    return wire


def _live_result_wire(result: ServiceProbeResult) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "probe": result.probe,
        "status": result.status,
        "server_version": result.server_version,
        "api_version": result.api_version,
        "observed_at": result.observed_at,
    }
    if result.components is not None:
        wire["components"] = [
            {
                "id": component.id,
                "status": component.status,
                "observed_at": component.observed_at,
                **(
                    {"message": component.message}
                    if component.message is not None
                    else {}
                ),
                **(
                    {"details": component.details}
                    if component.details is not None
                    else {}
                ),
            }
            for component in result.components
        ]
    if result.supported_capabilities is not None:
        wire["supported_capabilities"] = [
            {"id": capability.id, "version": capability.version}
            for capability in result.supported_capabilities
        ]
    if result.descriptor is not None:
        wire["descriptor"] = _live_descriptor_wire(result.descriptor)
    if result.details is not None:
        wire["details"] = result.details
    return wire


def _validate_live_result(result: object) -> ServiceProbeResult:
    if type(result) is not ServiceProbeResult:
        _raise_live_discovery()
    assert type(result) is ServiceProbeResult
    validated: ServiceProbeResult | None = None
    try:
        validated = decode_service_probe_result(_live_result_wire(result))
    except Exception:  # noqa: BLE001 -- all ordinary normalization failures fail closed.
        invalid = True
    else:
        invalid = False
    if invalid or validated is None:
        _raise_live_discovery()
    return validated


def _is_secure_posix(metadata: os.stat_result, *, directory: bool) -> bool:
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        expected_kind(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_posix(
    name: str | Path,
    *,
    directory: bool,
    dir_fd: int | None = None,
    missing_is_absent: bool = False,
    trusted_root: bool = False,
) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    else:
        # The kind check below runs on an *open* descriptor, so the open itself
        # must not be able to wait: a FIFO published at the descriptor path would
        # otherwise block this call until a writer appeared, and nothing on that
        # path can be interrupted -- the descriptor is read before the transport's
        # deadline and cancellation are consulted at all. ``O_NONBLOCK`` has no
        # effect on a read of a regular file, which is the only kind that survives
        # ``_is_secure_posix``, so it is left set rather than cleared afterwards.
        flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        if missing_is_absent:
            return None
        failure = "access"
    except OSError:
        failure = "provenance"
    else:
        metadata = os.fstat(descriptor)
        safe = (
            stat.S_ISDIR(metadata.st_mode)
            if trusted_root
            else _is_secure_posix(metadata, directory=directory)
        )
        if safe:
            return descriptor
        os.close(descriptor)
        failure = "provenance"
    if failure == "access":
        _raise_access()
    _raise_provenance()


def _read_bounded(descriptor: int, size: int) -> bytes:
    if size > MAXIMUM_DESCRIPTOR_BYTES:
        _raise_size()
    chunks: list[bytes] = []
    remaining = MAXIMUM_DESCRIPTOR_BYTES + 1
    failed = False
    while remaining:
        try:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
        except OSError:
            failed = True
            break
        if not chunk:
            failed = False
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if failed:
        _raise_access()
    content = b"".join(chunks)
    if len(content) > MAXIMUM_DESCRIPTOR_BYTES:
        _raise_size()
    return content


def _read_posix(root: Path, workspace_id: str) -> bytes | None:
    root_fd: int | None = None
    runtime_fd: int | None = None
    workspace_fd: int | None = None
    file_fd: int | None = None
    try:
        root_fd = _open_posix(root, directory=True, trusted_root=True)
        assert root_fd is not None
        runtime_fd = _open_posix(
            _RUNTIME_DIRECTORY,
            directory=True,
            dir_fd=root_fd,
            missing_is_absent=True,
        )
        if runtime_fd is None:
            return None
        runtime_metadata = os.fstat(runtime_fd)

        workspace_fd = _open_posix(
            workspace_id,
            directory=True,
            dir_fd=runtime_fd,
            missing_is_absent=True,
        )
        if workspace_fd is None:
            return None
        workspace_metadata = os.fstat(workspace_fd)

        file_fd = _open_posix(
            _DESCRIPTOR_NAME,
            directory=False,
            dir_fd=workspace_fd,
            missing_is_absent=True,
        )
        if file_fd is None:
            return None
        file_metadata = os.fstat(file_fd)
        content = _read_bounded(file_fd, file_metadata.st_size)

        # Atomic publication may replace the descriptor while it is being read.
        # Compare every opened object with the name that is canonical *now*; a
        # replacement or parent swap refuses this attempt rather than accepting a
        # document that is no longer the installation's advertised one.
        try:
            current_runtime = os.stat(
                _RUNTIME_DIRECTORY, dir_fd=root_fd, follow_symlinks=False
            )
            current_workspace = os.stat(
                workspace_id, dir_fd=runtime_fd, follow_symlinks=False
            )
            current_file = os.stat(
                _DESCRIPTOR_NAME, dir_fd=workspace_fd, follow_symlinks=False
            )
        except OSError:
            raced = True
        else:
            raced = not (
                _same_file(runtime_metadata, current_runtime)
                and _same_file(workspace_metadata, current_workspace)
                and _same_file(file_metadata, current_file)
                and _is_secure_posix(current_runtime, directory=True)
                and _is_secure_posix(current_workspace, directory=True)
                and _is_secure_posix(current_file, directory=False)
            )
        if raced:
            _raise_provenance()
        return content
    finally:
        for descriptor in (file_fd, workspace_fd, runtime_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


# Windows provenance ---------------------------------------------------------
#
# Python exposes reparse-point metadata but not owner/DACL inspection.  The
# standard-library ``ctypes`` calls below ask the native security APIs directly.
# An accepted object is owned by the current user and grants access only to that
# owner, LocalSystem, or the built-in Administrators group.  A null DACL or any
# allow ACE for another trustee is refused.

_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400
_OWNER_SECURITY_INFORMATION: Final = 0x00000001
_DACL_SECURITY_INFORMATION: Final = 0x00000004
_SE_FILE_OBJECT: Final = 1
_TOKEN_QUERY: Final = 0x0008
_TOKEN_USER: Final = 1
_ACL_SIZE_INFORMATION_CLASS: Final = 2
_ACCESS_ALLOWED_ACE_TYPE: Final = 0
_OTHER_ACCESS_ALLOWED_ACE_TYPES: Final = frozenset({4, 5, 9, 11})
_ERROR_INSUFFICIENT_BUFFER: Final = 122


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _TOKEN_USER_VALUE(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


def _configure_windows_api_signatures(advapi: Any, kernel: Any) -> None:
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetLengthSid.argtypes = [wintypes.LPVOID]
    advapi.GetLengthSid.restype = wintypes.DWORD
    advapi.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetAclInformation.argtypes = [
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi.GetAce.restype = wintypes.BOOL

    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetLastError.argtypes = []
    kernel.GetLastError.restype = wintypes.DWORD


def _windows_apis() -> tuple[Any, Any]:
    libraries = cast(Any, ctypes).windll
    advapi = libraries.advapi32
    kernel = libraries.kernel32
    _configure_windows_api_signatures(advapi, kernel)
    return advapi, kernel


def _windows_current_user_sid() -> bytes:
    advapi, kernel = _windows_apis()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(
        kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        _raise_provenance()
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        if kernel.GetLastError() != _ERROR_INSUFFICIENT_BUFFER:
            _raise_provenance()
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(
            token, _TOKEN_USER, buffer, needed, ctypes.byref(needed)
        ):
            _raise_provenance()
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER_VALUE)).contents
        length = advapi.GetLengthSid(token_user.Sid)
        return ctypes.string_at(token_user.Sid, length)
    finally:
        kernel.CloseHandle(token)


def _windows_well_known_sid(text: str) -> bytes:
    advapi, kernel = _windows_apis()
    pointer = wintypes.LPVOID()
    if not advapi.ConvertStringSidToSidW(text, ctypes.byref(pointer)):
        _raise_provenance()
    try:
        return ctypes.string_at(pointer, advapi.GetLengthSid(pointer))
    finally:
        kernel.LocalFree(pointer)


def _windows_acl_is_owner_equivalent(path: Path) -> bool:
    advapi, kernel = _windows_apis()
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    security = wintypes.LPVOID()
    result = advapi.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(security),
    )
    if result != 0 or not owner or not dacl:
        if security:
            kernel.LocalFree(security)
        return False
    try:
        owner_bytes = ctypes.string_at(owner, advapi.GetLengthSid(owner))
        permitted = {
            _windows_current_user_sid(),
            _windows_well_known_sid("S-1-5-18"),
            _windows_well_known_sid("S-1-5-32-544"),
        }
        if owner_bytes != _windows_current_user_sid():
            return False
        information = _ACL_SIZE_INFORMATION()
        if not advapi.GetAclInformation(
            dacl,
            ctypes.byref(information),
            ctypes.sizeof(information),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            return False
        for index in range(information.AceCount):
            ace = wintypes.LPVOID()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                return False
            header = ctypes.string_at(ace, 4)
            if header[0] in _OTHER_ACCESS_ALLOWED_ACE_TYPES:
                # Object/callback/compound allow ACEs place their SID at a
                # different offset. They are unnecessary for an owner-equivalent
                # descriptor ACL, so refusing them is safer than parsing a wider
                # ACL language this package does not need.
                return False
            if header[0] != _ACCESS_ALLOWED_ACE_TYPE:
                continue
            assert ace.value is not None
            sid_address = int(ace.value) + 8
            sid_pointer = ctypes.c_void_p(sid_address)
            sid = ctypes.string_at(sid_pointer, advapi.GetLengthSid(sid_pointer))
            if sid not in permitted:
                return False
        return True
    finally:
        kernel.LocalFree(security)


def _windows_metadata_is_safe(path: Path, *, directory: bool) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT == 0
        and expected_kind(metadata.st_mode)
        and _windows_acl_is_owner_equivalent(path)
    )


def _read_windows(root: Path, workspace_id: str) -> bytes | None:
    runtime = root / _RUNTIME_DIRECTORY
    workspace = runtime / workspace_id
    path = workspace / _DESCRIPTOR_NAME
    before: tuple[os.stat_result, os.stat_result, os.stat_result] | None = None
    try:
        before = (runtime.lstat(), workspace.lstat(), path.lstat())
    except FileNotFoundError:
        return None
    except OSError:
        failed = True
    else:
        failed = False
    if failed:
        _raise_access()
    assert before is not None
    if not (
        _windows_metadata_is_safe(runtime, directory=True)
        and _windows_metadata_is_safe(workspace, directory=True)
        and _windows_metadata_is_safe(path, directory=False)
    ):
        _raise_provenance()
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOINHERIT", 0))
    except OSError:
        failed = True
    else:
        failed = False
    if failed:
        _raise_access()
    try:
        opened = os.fstat(descriptor)
        if not _same_file(before[2], opened):
            _raise_provenance()
        content = _read_bounded(descriptor, opened.st_size)
    finally:
        os.close(descriptor)
    after: tuple[os.stat_result, os.stat_result, os.stat_result] | None = None
    try:
        after = (runtime.lstat(), workspace.lstat(), path.lstat())
    except OSError:
        failed = True
    else:
        failed = False
    if failed:
        _raise_provenance()
    assert after is not None
    if any(
        not _same_file(left, right) for left, right in zip(before, after, strict=True)
    ):
        _raise_provenance()
    if not (
        _windows_metadata_is_safe(runtime, directory=True)
        and _windows_metadata_is_safe(workspace, directory=True)
        and _windows_metadata_is_safe(path, directory=False)
    ):
        _raise_provenance()
    return content


def _read_descriptor(root: Path, workspace_id: str) -> bytes | None:
    if os.name == "posix":
        return _read_posix(root, workspace_id)
    if os.name == "nt":
        return _read_windows(root, workspace_id)
    _raise_provenance()


class _DuplicateMember(ValueError):
    pass


def _mapping_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise _DuplicateMember
        mapping[key] = value
    return mapping


def _invalid_constant(_value: str) -> None:
    raise ValueError


def _decode_descriptor(content: bytes) -> ServiceEndpointDescriptor:
    try:
        text = content.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_mapping_without_duplicates,
            parse_constant=_invalid_constant,
        )
        value = decode_service_endpoint_descriptor(document)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        _DuplicateMember,
        ContractDecodeError,
        ContractSemanticError,
        TypeError,
        ValueError,
    ):
        invalid = True
    else:
        invalid = False
    if invalid:
        _raise_document()
    return value


def discover_endpoint(
    installation_state: Path,
    workspace_id: str,
    *,
    transport: ClientTransport,
    deadline: Deadline,
    cancellation: CancellationToken | None = None,
) -> DiscoveredEndpoint | None:
    """Discover, negotiate, and live-verify one installation-local endpoint.

    An absent descriptor is transient and returns ``None``; every present but
    untrusted, malformed, non-local, incompatible, replaced, or live-mismatched
    descriptor fails closed.  The same whole-call deadline and cancellation token
    are passed unchanged to the injected transport.  A failure raised by that
    transport is translated rather than propagated -- its diagnostic is outside
    this package's payload-free rule -- but the translation preserves the
    declared type, so an expired deadline and a cancellation stay distinguishable
    from a transport that could not carry the call.
    """
    identifier = _require_workspace_id(workspace_id)
    content = _read_descriptor(Path(installation_state), identifier)
    if content is None:
        return None
    descriptor = _decode_descriptor(content)
    if descriptor.workspace_id != identifier:
        _raise_live_identity()
    _require_local_endpoint(descriptor)
    negotiated = _negotiate_descriptor(descriptor)

    remaining = enforce_send_preconditions(
        deadline=deadline,
        cancellation=cancellation,
        operation=_DISCOVER_PROBE,
    )
    deadline_ms = min(MAXIMUM_DURATION_MS, math.floor(remaining * 1000))
    # A transport's failure keeps its *kind* and loses its *words*. Which of the
    # three ways a call can fail happened is what a caller acts on -- a cancelled
    # call was abandoned by the caller, an expired one by the clock, and reporting
    # either as "the transport could not carry this" misattributes the cause. The
    # message is another matter: an injected transport is third-party code, its
    # diagnostic is outside this package's payload-free rule, and it may name a
    # credential or a local path. So a fresh, fixed-text instance of the same
    # declared type is raised, never the transport's own object -- and, like every
    # refusal here, raised outside the handler so no chain survives to render it.
    answer: ServiceProbeResult | None = None
    outcome = "answered"
    try:
        answer = transport.probe(
            ServiceProbeRequest(probe=_DISCOVER_PROBE, deadline_ms=deadline_ms),
            deadline=deadline,
            cancellation=cancellation,
        )
    except DeadlineExceededError:
        outcome = "expired"
    except OperationCancelledError:
        outcome = "cancelled"
    except Exception:  # noqa: BLE001 -- a transport diagnostic is not payload-free.
        outcome = "unreachable"
    if outcome == "expired":
        _raise_live_deadline()
    if outcome == "cancelled":
        _raise_live_cancelled()
    if outcome != "answered" or answer is None:
        _raise_live_transport()
    result = _validate_live_result(answer)
    if (
        type(result.probe) is not str
        or result.probe != _DISCOVER_PROBE
        or type(result.status) is not str
        or result.status != "pass"
    ):
        _raise_live_discovery()
    live = result.descriptor
    if live is None:
        _raise_live_discovery()
    if (
        type(live.workspace_id) is not str
        or type(live.service_instance_id) is not str
        or live.workspace_id != descriptor.workspace_id
        or live.service_instance_id != descriptor.service_instance_id
    ):
        _raise_live_identity()
    return DiscoveredEndpoint(descriptor=descriptor, negotiated=negotiated)
