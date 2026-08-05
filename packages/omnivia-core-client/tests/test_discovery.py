"""Installation-local endpoint discovery and live identity verification."""

from __future__ import annotations

import csv
import ctypes
import dataclasses
import json
import os
import socket
import stat
import subprocess
import threading
import time
import traceback
from collections.abc import Iterator, Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

import omnivia_core_client.discovery as discovery_module
import pytest
from omnivia_core_client import (
    CancellationToken,
    ClientError,
    CompatibilityError,
    Deadline,
    DeadlineExceededError,
    DiscoveredEndpoint,
    OperationCancelledError,
    ProtocolError,
    TransportError,
    descriptor_path,
    discover_endpoint,
)
from omnivia_core_client.discovery import MAXIMUM_DESCRIPTOR_BYTES, _is_secure_posix

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ContractSemanticError,
    RequestEnvelope,
    ResponseEnvelope,
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProbeResult,
    ServiceProcessEvidence,
    decode_service_endpoint_descriptor,
)

WORKSPACE_ID = "workspace-alpha"
SERVICE_INSTANCE_ID = "service-instance-01"

# The local IPC endpoint this platform can dial, and the one it cannot. Both are
# accepted by the shared publication policy, which is the point: only the
# client's locality rule tells them apart.
LOCAL_IPC_URI = (
    "unix:///var/run/omnivia/core.sock"
    if os.name == "posix"
    else "pipe://omnivia-core-abc"
)
FOREIGN_LOCAL_IPC_URI = (
    "pipe://omnivia-core-abc"
    if os.name == "posix"
    else "unix:///var/run/omnivia/core.sock"
)

# A descriptor read must finish inside its own call deadline; the wall-clock
# bound is longer only so a blocked read is reported as a failure rather than
# hanging the suite.
DESCRIPTOR_DEADLINE_SECONDS = 1.0
HANG_DETECTION_SECONDS = 15.0


class EqualitySpoof(str):
    def __eq__(self, _other: object) -> bool:
        return True

    def __ne__(self, _other: object) -> bool:
        return False

    __hash__ = str.__hash__


def descriptor_wire(**overrides: object) -> dict[str, Any]:
    document: dict[str, Any] = {
        "descriptor_version": CONTRACT_VERSION,
        "workspace_id": WORKSPACE_ID,
        "service_instance_id": SERVICE_INSTANCE_ID,
        "installation_id": "installation-alpha",
        "endpoint_uri": LOCAL_IPC_URI,
        "protocol_version": "1.0",
        "server_version": "1.2.5",
        "supported_api_versions": {
            "minimum": f"{CONTRACT_VERSION.split('.')[0]}.0",
            "maximum": CONTRACT_VERSION,
        },
        "supported_workspace_versions": {"minimum": "1.0", "maximum": "1.0"},
        "workspace_format_version": "1.0",
        "ready": True,
        "lifecycle_state": "serving",
        "fencing_generation": 7,
        "published_at": "2026-07-30T11:59:58Z",
        "process": {
            "pid": 4821,
            "start_time": "1785412798.42",
            "boot_id": "boot-7f3c",
        },
    }
    document.update(overrides)
    return document


def descriptor(**overrides: object) -> ServiceEndpointDescriptor:
    return ServiceEndpointDescriptor.from_wire(descriptor_wire(**overrides))


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class RecordingTransport:
    def __init__(
        self, live_descriptor: ServiceEndpointDescriptor | None = None
    ) -> None:
        self.live_descriptor = (
            descriptor() if live_descriptor is None else live_descriptor
        )
        self.probes: list[
            tuple[ServiceProbeRequest, Deadline, CancellationToken | None]
        ] = []

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        raise AssertionError("discovery must not use the application call path")

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        self.probes.append((request, deadline, cancellation))
        return ServiceProbeResult(
            probe="service.discover",
            status="pass",
            server_version="1.2.5",
            api_version=CONTRACT_VERSION,
            observed_at="2026-07-30T12:00:00Z",
            descriptor=self.live_descriptor,
        )


class FixedResultTransport(RecordingTransport):
    def __init__(self, result: ServiceProbeResult) -> None:
        super().__init__()
        self.result = result

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        self.probes.append((request, deadline, cancellation))
        return self.result


def publish(root: Path, document: object | None = None) -> Path:
    path = descriptor_path(root, WORKSPACE_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        (root / "runtime").chmod(0o700)
        path.parent.chmod(0o700)
    path.write_text(
        json.dumps(descriptor_wire() if document is None else document),
        encoding="utf-8",
    )
    if os.name == "posix":
        path.chmod(0o600)
    elif os.name == "nt":
        # Windows test fixtures must model the producer's owner-equivalent
        # publication contract rather than inherit the hosted runner's broad temp
        # directory ACL and elevated-token default owner.
        _secure_windows_descriptor_tree(root, path)
    return path


def plant(path: Path, document: object | None = None) -> Path:
    """Write a well-formed descriptor at an arbitrary path.

    ``publish`` writes the *canonical* descriptor for a root. This writes the
    same document somewhere discovery must never look, which is the only way to
    tell "not found" apart from "found nothing because the fixture was empty".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(descriptor_wire() if document is None else document),
        encoding="utf-8",
    )
    if os.name == "posix":
        path.chmod(0o600)
    return path


NOBODY_UID = 65534


def foreign_owned_directory() -> Path | None:
    """A real directory on this host owned by another uid, already tightly moded.

    Ownership has to be the *only* rule such a directory breaks. A world-readable
    one -- ``/usr`` at ``0o755`` -- fails the mode rule as well and would prove
    nothing about the owner rule, so only ``0o700``-class directories qualify.
    Returns ``None`` where the host has none.
    """
    mine = os.geteuid()
    for parent in ("/var", "/etc", "/usr", "/opt", "/var/lib", "/var/db"):
        try:
            with os.scandir(parent) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if (
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid != mine
                and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
            ):
                return Path(entry.path)
    return None


def foreign_uid() -> int | None:
    """A uid that exists on this host and is not this process's own."""
    mine = os.geteuid()
    for candidate in ("/", "/usr", "/etc", "/var"):
        try:
            uid = os.stat(candidate).st_uid
        except OSError:
            continue
        if uid != mine:
            return uid
    return NOBODY_UID if mine != NOBODY_UID else None


def discover(
    root: Path, transport: RecordingTransport | None = None
) -> DiscoveredEndpoint | None:
    return discover_endpoint(
        root,
        WORKSPACE_ID,
        transport=RecordingTransport() if transport is None else transport,
        deadline=Deadline.after(30.0, clock=FakeClock()),
    )


def assert_sanitized(error: BaseException, planted: str) -> None:
    rendered = "".join(traceback.TracebackException.from_exception(error).format())
    for exposed in (str(error), repr(error.args), rendered):
        assert planted not in exposed
    assert error.__cause__ is None
    assert error.__context__ is None
    assert all(
        planted not in str(frame) for frame in traceback.extract_tb(error.__traceback__)
    )


def _windows_current_user_sid_text() -> str:
    completed = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
    )
    row = next(csv.reader([completed.stdout.strip()]))
    assert len(row) == 2
    sid = row[1]
    assert sid.startswith("S-1-")
    return sid


def _windows_set_dacl(path: Path, sddl: str) -> None:
    libraries = cast(Any, ctypes).windll
    advapi = libraries.advapi32
    kernel = libraries.kernel32
    security_descriptor = wintypes.LPVOID()
    owner = wintypes.LPVOID()
    size = wintypes.DWORD()

    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(security_descriptor), ctypes.byref(size)
    )
    advapi.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    converted_owner = advapi.ConvertStringSidToSidW(
        _windows_current_user_sid_text(), ctypes.byref(owner)
    )
    if not converted_owner:
        kernel.LocalFree.argtypes = [wintypes.LPVOID]
        kernel.LocalFree.restype = wintypes.LPVOID
        if owner:
            kernel.LocalFree(owner)
        kernel.LocalFree(security_descriptor)
    assert converted_owner
    try:
        present = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        defaulted = wintypes.BOOL()
        advapi.GetSecurityDescriptorDacl.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        ]
        advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        assert advapi.GetSecurityDescriptorDacl(
            security_descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        )
        assert present.value

        advapi.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
        dacl_result = advapi.SetNamedSecurityInfoW(
            str(path),
            1,
            0x00000004 | 0x80000000,
            None,
            None,
            dacl,
            None,
        )
        assert dacl_result == 0
        # Apply the protected owner-equivalent DACL first. Hosted Windows may create
        # elevated-token files owned by Administrators; granting the current SID
        # WRITE_OWNER before changing ownership avoids ERROR_ACCESS_DENIED (5).
        owner_result = advapi.SetNamedSecurityInfoW(
            str(path),
            1,
            0x00000001,
            owner,
            None,
            None,
            None,
        )
        assert owner_result == 0
    finally:
        kernel.LocalFree.argtypes = [wintypes.LPVOID]
        kernel.LocalFree.restype = wintypes.LPVOID
        kernel.LocalFree(owner)
        kernel.LocalFree(security_descriptor)


def _windows_set_null_dacl(path: Path) -> None:
    advapi = cast(Any, ctypes).windll.advapi32
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    result = advapi.SetNamedSecurityInfoW(
        str(path),
        1,
        0x00000004 | 0x80000000,
        None,
        None,
        None,
        None,
    )
    assert result == 0


def _windows_owner_equivalent_sddl() -> str:
    owner = _windows_current_user_sid_text()
    return f"D:P(A;;FA;;;{owner})(A;;FA;;;SY)(A;;FA;;;BA)"


def _secure_windows_descriptor_tree(root: Path, path: Path) -> None:
    sddl = _windows_owner_equivalent_sddl()
    for candidate in (root / "runtime", path.parent, path):
        _windows_set_dacl(candidate, sddl)


def test_windows_native_api_signatures_are_explicit_and_pointer_width_safe() -> None:
    class NativeFunction:
        argtypes: list[object] | None = None
        restype: object | None = None

    class NativeLibrary:
        pass

    advapi: Any = NativeLibrary()
    kernel: Any = NativeLibrary()
    for library, names in (
        (
            advapi,
            (
                "OpenProcessToken",
                "GetTokenInformation",
                "GetLengthSid",
                "ConvertStringSidToSidW",
                "GetNamedSecurityInfoW",
                "GetAclInformation",
                "GetAce",
            ),
        ),
        (
            kernel,
            ("LocalFree", "CloseHandle", "GetCurrentProcess", "GetLastError"),
        ),
    ):
        for name in names:
            setattr(library, name, NativeFunction())

    discovery_module._configure_windows_api_signatures(advapi, kernel)

    signatures: tuple[tuple[Any, list[object], object], ...] = (
        (
            advapi.OpenProcessToken,
            [
                wintypes.HANDLE,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.HANDLE),
            ],
            wintypes.BOOL,
        ),
        (
            advapi.GetTokenInformation,
            [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ],
            wintypes.BOOL,
        ),
        (advapi.GetLengthSid, [wintypes.LPVOID], wintypes.DWORD),
        (
            advapi.ConvertStringSidToSidW,
            [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)],
            wintypes.BOOL,
        ),
        (
            advapi.GetNamedSecurityInfoW,
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.LPVOID),
            ],
            wintypes.DWORD,
        ),
        (
            advapi.GetAclInformation,
            [wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD],
            wintypes.BOOL,
        ),
        (
            advapi.GetAce,
            [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)],
            wintypes.BOOL,
        ),
        (kernel.LocalFree, [wintypes.HLOCAL], wintypes.HLOCAL),
        (kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
        (kernel.GetCurrentProcess, [], wintypes.HANDLE),
        (kernel.GetLastError, [], wintypes.DWORD),
    )
    for function, argtypes, restype in signatures:
        assert function.argtypes == argtypes
        assert function.restype is restype


def test_descriptor_path_is_canonical_and_workspace_ids_cannot_select_paths(
    tmp_path: Path,
) -> None:
    assert descriptor_path(tmp_path, WORKSPACE_ID) == (
        tmp_path / "runtime" / WORKSPACE_ID / "service.json"
    )
    for invalid in (
        "",
        ".",
        "..",
        "../escape",
        "nested/workspace",
        "/absolute",
        "bad id",
    ):
        with pytest.raises(ValueError, match="workspace_id"):
            descriptor_path(tmp_path, invalid)


def test_discover_endpoint_is_exported_from_the_public_barrel() -> None:
    import omnivia_core_client

    assert "discover_endpoint" in omnivia_core_client.__all__
    assert omnivia_core_client.discover_endpoint is discover_endpoint


def test_canonical_descriptor_negotiates_and_live_identity_is_verified(
    tmp_path: Path,
) -> None:
    publish(tmp_path)
    transport = RecordingTransport()

    found = discover(tmp_path, transport)

    assert found is not None
    assert isinstance(found, DiscoveredEndpoint)
    assert found.descriptor == descriptor()
    assert found.negotiated.api_version == CONTRACT_VERSION
    assert found.negotiated.protocol_version == "1.0"
    assert transport.probes[0][0].probe == "service.discover"
    assert transport.probes[0][0].deadline_ms == 30_000
    assert transport.probes[0][1].end == 30.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        found.descriptor = descriptor()  # type: ignore[misc]


def test_missing_descriptor_is_a_transient_absence_and_does_not_connect(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport()
    assert discover(tmp_path, transport) is None
    assert transport.probes == []


def test_descriptor_read_is_bounded_before_content_is_decoded(tmp_path: Path) -> None:
    path = publish(tmp_path)
    with path.open("wb") as stream:
        stream.truncate(MAXIMUM_DESCRIPTOR_BYTES + 1)
    if os.name == "posix":
        path.chmod(0o600)

    with pytest.raises(ProtocolError, match="size bound") as caught:
        discover(tmp_path)
    assert_sanitized(caught.value, str(path))


@pytest.mark.parametrize(
    "document",
    [
        b"\xff\xfe",
        b'{"workspace_id":',
        b"[]",
        json.dumps({"workspace_id": "only-one-field"}).encode(),
        b'{"workspace_id":"one","workspace_id":"two"}',
        b'{"unknown":NaN}',
    ],
    ids=(
        "invalid-utf8",
        "invalid-json",
        "non-object",
        "invalid-dto",
        "duplicate-member",
        "non-finite-number",
    ),
)
def test_malformed_descriptors_fail_with_sanitized_errors(
    tmp_path: Path, document: bytes
) -> None:
    path = publish(tmp_path)
    path.write_bytes(document)
    if os.name == "posix":
        path.chmod(0o600)

    with pytest.raises(ProtocolError, match="descriptor document") as caught:
        discover(tmp_path)
    assert_sanitized(caught.value, str(path))


def test_unknown_optional_fields_are_tolerated_and_never_carried_forward(
    tmp_path: Path,
) -> None:
    """A newer peer's additive minor still decodes here, and adds nothing.

    Within a major, a minor release only adds optional fields, so refusing a
    document for carrying one this build has never heard of would make every
    additive release a breaking one. The other half matters just as much: the
    unknown members are dropped, not smuggled into the value handed back, so
    nothing downstream can start depending on a field this build cannot validate.
    """
    publish(
        tmp_path,
        descriptor_wire(
            unknown_future_field="additive-minor-value",
            process={
                "pid": 4821,
                "start_time": "1785412798.42",
                "boot_id": "boot-7f3c",
                "unknown_evidence": ["anything"],
            },
            supported_api_versions={
                "minimum": f"{CONTRACT_VERSION.split('.')[0]}.0",
                "maximum": CONTRACT_VERSION,
                "unknown_bound": "1.9",
            },
        ),
    )

    found = discover(tmp_path)

    assert found is not None
    assert found.descriptor == descriptor()


@pytest.mark.parametrize(
    "decoy",
    [
        "implicit-home",
        "dot-directory-under-home",
        "workspace-storage",
        "installation-root",
        "runtime-root",
        "current-directory",
    ],
)
def test_a_hostile_root_cannot_redirect_discovery_into_workspace_or_home_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decoy: str
) -> None:
    """One derived path, and no second place to look when it comes up empty.

    Each decoy here is a perfectly valid descriptor sitting where some other
    local-state convention would put it: an implicit home, a dotted directory
    under it, this installation's workspace storage, the installation root
    itself, the runtime directory without a workspace below it, and the process's
    current directory. A fallback to any of them would let whoever controls that
    location choose the endpoint this client dials, which is the descriptor being
    used as authority rather than as coordination.

    The positive control at the end is what makes the absence meaningful: the
    same fixture finds the canonical descriptor, so "not found" is a statement
    about where discovery looked and not about a fixture that never worked.
    """
    installation = tmp_path / "installation"
    (installation / "runtime").mkdir(parents=True, mode=0o700)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    for variable in (
        "HOME",
        "USERPROFILE",
        "XDG_STATE_HOME",
        "APPDATA",
        "LOCALAPPDATA",
    ):
        monkeypatch.setenv(variable, str(home))

    if decoy == "implicit-home":
        publish(home)
    elif decoy == "dot-directory-under-home":
        (home / ".omnivia").mkdir(mode=0o700)
        publish(home / ".omnivia")
    elif decoy == "workspace-storage":
        plant(installation / "workspaces" / WORKSPACE_ID / "service.json")
    elif decoy == "installation-root":
        plant(installation / "service.json")
    elif decoy == "runtime-root":
        plant(installation / "runtime" / "service.json")
    else:
        working = tmp_path / "working"
        working.mkdir(mode=0o700)
        publish(working)
        monkeypatch.chdir(working)

    transport = RecordingTransport()
    assert discover(installation, transport) is None
    assert transport.probes == []

    publish(installation)
    assert discover(installation) is not None


def test_credential_bearing_and_direct_storage_endpoints_are_refused_without_leakage(
    tmp_path: Path,
) -> None:
    for planted in (
        "https://user:secret@example.test/core",
        "file:///private/workspace.sqlite3",
    ):
        publish(tmp_path, descriptor_wire(endpoint_uri=planted))
        with pytest.raises(ProtocolError, match="descriptor document") as caught:
            discover(tmp_path)
        assert_sanitized(caught.value, planted)


@pytest.mark.parametrize(
    "planted",
    [
        "https://example.test/core",
        "http://127.0.0.1:8080/",
        FOREIGN_LOCAL_IPC_URI,
        f"https://example.test/{LOCAL_IPC_URI}",
    ],
    ids=(
        "credential-free-remote",
        "loopback-remote",
        "other-platform-local-scheme",
        "scheme-mismatch",
    ),
)
def test_endpoints_outside_this_platform_local_ipc_are_refused_before_connection(
    tmp_path: Path, planted: str
) -> None:
    """Every one of these is a descriptor the shared publication policy admits.

    That policy answers "is this safe to publish before authentication", which is
    a different question from "may this client dial it". A credential-free remote
    URI, a loopback HTTP endpoint, the other platform's local IPC scheme, and a
    remote URI whose *path* spells this platform's local scheme are all well
    formed and all refused here, before the transport is touched.
    """
    publish(tmp_path, descriptor_wire(endpoint_uri=planted))
    transport = RecordingTransport()

    with pytest.raises(TransportError, match="local IPC") as caught:
        discover(tmp_path, transport)

    assert_sanitized(caught.value, planted)
    assert transport.probes == []


def test_traversal_like_pipe_names_never_reach_the_transport(tmp_path: Path) -> None:
    planted_uris = ["pipe://../../etc/passwd", "pipe://..%2F..%2Fomnivia"]
    if os.name != "nt":
        # A separator-free name is a legal Windows pipe name with nothing to
        # traverse into, so the shared pattern admits it and only the POSIX
        # locality rule refuses it.
        planted_uris.append("pipe://omnivia..-..-core")
    transport = RecordingTransport()

    for planted in planted_uris:
        publish(tmp_path, descriptor_wire(endpoint_uri=planted))
        with pytest.raises(ClientError) as caught:
            discover(tmp_path, transport)
        assert_sanitized(caught.value, planted)
        assert transport.probes == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor file kinds")
@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_posix_refuses_a_non_regular_descriptor_without_waiting_for_a_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A FIFO or socket published at the descriptor path is refused promptly.

    Opening a FIFO for reading blocks until a writer appears, and that wait is
    reachable by no deadline in this package: the descriptor is read before the
    transport's send preconditions are ever consulted. So the bound asserted here
    is wall-clock, and the read runs in a worker thread so that a regression is a
    failure rather than a hung suite.
    """
    path = publish(tmp_path)
    path.unlink()
    listener: socket.socket | None = None
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        # An AF_UNIX path is bounded near 104 bytes, well under a temporary
        # descriptor path, so bind relative to the workspace directory instead.
        monkeypatch.chdir(path.parent)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path.name)
    transport = RecordingTransport()
    outcome: list[object] = []

    def attempt() -> None:
        try:
            outcome.append(
                discover_endpoint(
                    tmp_path,
                    WORKSPACE_ID,
                    transport=transport,
                    deadline=Deadline.after(DESCRIPTOR_DEADLINE_SECONDS),
                )
            )
        except Exception as error:  # noqa: BLE001 -- the worker reports any outcome.
            outcome.append(error)

    try:
        os.chmod(path, 0o600)
        worker = threading.Thread(target=attempt, daemon=True)
        started = time.monotonic()
        worker.start()
        worker.join(HANG_DETECTION_SECONDS)
        elapsed = time.monotonic() - started
    finally:
        if listener is not None:
            listener.close()

    assert not worker.is_alive(), f"a {kind} descriptor blocked discovery"
    assert elapsed < DESCRIPTOR_DEADLINE_SECONDS
    assert transport.probes == []
    (refusal,) = outcome
    assert isinstance(refusal, TransportError)
    assert "provenance" in str(refusal)
    assert_sanitized(refusal, str(path))


@pytest.mark.skipif(os.name != "posix", reason="POSIX provenance")
def test_posix_rejects_symlinks_wrong_file_kinds_and_accessible_or_foreign_files(
    tmp_path: Path,
) -> None:
    path = publish(tmp_path)
    target = tmp_path / "target.json"
    target.write_text(json.dumps(descriptor_wire()), encoding="utf-8")
    target.chmod(0o600)

    path.unlink()
    path.symlink_to(target)
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)

    path.unlink()
    path.mkdir()
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)

    path.rmdir()
    path.write_text(json.dumps(descriptor_wire()), encoding="utf-8")
    path.chmod(0o640)
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)

    path.chmod(0o600)
    original = path.stat()
    # SYNTHESIZED owner, not a real one: this host cannot chown a file to another
    # uid without privilege. Real foreign-owner evidence lives in
    # `test_posix_refuses_a_real_foreign_owned_directory_and_accepts_an_owned_one`
    # and in the chown-gated public-path test beside it.
    foreign = os.stat_result(
        (
            original.st_mode,
            original.st_ino,
            original.st_dev,
            original.st_nlink,
            original.st_uid + 1,
            original.st_gid,
            original.st_size,
            original.st_atime,
            original.st_mtime,
            original.st_ctime,
        )
    )
    assert not _is_secure_posix(foreign, directory=False)


@pytest.mark.skipif(os.name != "posix", reason="POSIX provenance")
@pytest.mark.parametrize("mode", [0o750, 0o701, 0o711], ids=("0o750", "0o701", "0o711"))
def test_posix_rejects_group_or_other_access_on_each_derived_parent(
    tmp_path: Path, mode: int
) -> None:
    """Any group or other bit on a traversed parent refuses.

    ``0o701`` and ``0o711`` are the world-*searchable* cases, and they are the
    interesting ones: neither grants a read of the directory, only the right to
    traverse into it and name what is inside. That is enough for another local
    account to reach the descriptor, so a directory that grants it is not a
    directory this client will read a coordination file out of.
    """
    path = publish(tmp_path)
    for parent in (tmp_path / "runtime", path.parent):
        parent.chmod(mode)
        with pytest.raises(TransportError, match="provenance"):
            discover(tmp_path)
        parent.chmod(0o700)


@pytest.mark.skipif(os.name != "posix", reason="POSIX provenance")
@pytest.mark.parametrize("mode", [0o640, 0o644, 0o666], ids=("0o640", "0o644", "0o666"))
def test_posix_rejects_any_group_or_other_access_on_the_descriptor_file(
    tmp_path: Path, mode: int
) -> None:
    """The file half of the same rule: group read, other read and world write.

    ``0o644`` and ``0o666`` are what an ordinary ``open()`` produces under a
    default and a cleared umask, so they are the two modes a real publisher is
    most likely to write by accident rather than by attack -- which is precisely
    why the refusal cannot be reserved for the deliberate-looking ones.
    """
    path = publish(tmp_path)
    path.chmod(mode)
    transport = RecordingTransport()

    with pytest.raises(TransportError, match="provenance") as caught:
        discover(tmp_path, transport)

    assert_sanitized(caught.value, str(path))
    assert transport.probes == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX provenance")
def test_posix_refuses_a_real_foreign_owned_directory_and_accepts_an_owned_one(
    tmp_path: Path,
) -> None:
    """Real foreign ownership, read off a real inode, with nothing mocked.

    An unprivileged process cannot manufacture a foreign-owned file: ``chown`` to
    another uid needs root. What every ordinary host does have is directories
    another uid already owns, and the owner rule can be judged against one of
    those directly. The pair of assertions is the evidence: two directories with
    the same owner-only mode and the same kind, differing in nothing but uid, and
    only one of them survives. So it is ownership that decided it, not the mode.
    """
    foreign = foreign_owned_directory()
    if foreign is None:
        pytest.skip("this host has no foreign-owned directory with an owner-only mode")
    ours = tmp_path / "ours"
    ours.mkdir(mode=0o700)
    foreign_metadata = os.stat(foreign)

    assert foreign_metadata.st_uid != os.geteuid()
    assert stat.S_IMODE(foreign_metadata.st_mode) & 0o077 == 0
    assert _is_secure_posix(os.stat(ours), directory=True)
    assert not _is_secure_posix(foreign_metadata, directory=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX provenance")
def test_posix_real_foreign_owner_is_refused_through_the_public_discovery_path(
    tmp_path: Path,
) -> None:
    """The same rule end to end, against provenance the kernel actually recorded.

    Only a privileged process can hand a file to another uid, so this runs where
    the suite has that privilege and skips where it does not, saying which. The
    skip is not a gap being waved past: the sibling above proves the owner rule
    against a real foreign inode without privilege, and the monkeypatched test
    below is labelled as the stand-in for exactly this path.
    """
    path = publish(tmp_path)
    uid = foreign_uid()
    if uid is None:
        pytest.skip("no uid other than this process's own is available to chown to")
    try:
        os.chown(path, uid, -1)
    except OSError:
        pytest.skip("this host does not permit chown to a foreign uid")
    assert path.stat().st_uid == uid

    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX provenance")
def test_posix_mocked_owner_mismatch_is_rejected_through_the_public_discovery_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MOCK -- ``os.fstat`` is monkeypatched to report a uid the file does not have.

    Labelled as a mock on purpose. The cross-platform expectations forbid mocking
    for final acceptance, and this is not offered as acceptance evidence: it is
    the only way to reach the *public* path's owner branch on a developer machine
    that cannot ``chown``. The two tests above carry the real evidence -- a real
    foreign-owned inode judged by the real rule, and this same public path where
    the host permits a genuine ``chown``. Read this one as coverage of the
    branch's wiring, not of its behaviour against real provenance.
    """
    publish(tmp_path)
    real_fstat = os.fstat

    def foreign_file_owner(descriptor_fd: int) -> os.stat_result:
        metadata = real_fstat(descriptor_fd)
        if not stat.S_ISREG(metadata.st_mode):
            return metadata
        return os.stat_result(
            (
                metadata.st_mode,
                metadata.st_ino,
                metadata.st_dev,
                metadata.st_nlink,
                metadata.st_uid + 1,
                metadata.st_gid,
                metadata.st_size,
                metadata.st_atime,
                metadata.st_mtime,
                metadata.st_ctime,
            )
        )

    monkeypatch.setattr(os, "fstat", foreign_file_owner)
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX provenance")
def test_posix_rejects_symlinked_runtime_and_workspace_parents(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (tmp_path / "runtime").symlink_to(outside, target_is_directory=True)
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)

    (tmp_path / "runtime").unlink()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    external_workspace = tmp_path / "external-workspace"
    external_workspace.mkdir(mode=0o700)
    (runtime / WORKSPACE_ID).symlink_to(external_workspace, target_is_directory=True)
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX provenance")
def test_real_posix_descriptor_provenance_uses_native_owner_mode_and_file_kind(
    tmp_path: Path,
) -> None:
    path = publish(tmp_path)
    metadata = path.stat()
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_uid == os.geteuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert discover(tmp_path) is not None


@pytest.mark.skipif(os.name != "nt", reason="native Windows provenance")
def test_windows_accepts_owner_equivalent_dacl_and_refuses_foreign_or_null_dacl(
    tmp_path: Path,
) -> None:
    path = publish(tmp_path)
    _secure_windows_descriptor_tree(tmp_path, path)

    assert discover(tmp_path) is not None

    owner = _windows_current_user_sid_text()
    _windows_set_dacl(path, f"D:P(A;;FA;;;{owner})(A;;FA;;;WD)")
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)

    _windows_set_dacl(path, _windows_owner_equivalent_sddl())
    assert discover(tmp_path) is not None
    _windows_set_null_dacl(path)
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="native Windows provenance")
def test_windows_refuses_wrong_file_kind_and_reparse_point_via_public_discovery(
    tmp_path: Path,
) -> None:
    path = publish(tmp_path)
    _secure_windows_descriptor_tree(tmp_path, path)

    path.unlink()
    path.mkdir()
    _windows_set_dacl(path, _windows_owner_equivalent_sddl())
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)

    path.rmdir()
    path.parent.rmdir()
    target = tmp_path / "external-workspace"
    target.mkdir()
    (target / "service.json").write_text(
        json.dumps(descriptor_wire()), encoding="utf-8"
    )
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(path.parent), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    try:
        attributes = getattr(path.parent.lstat(), "st_file_attributes", 0)
        assert attributes & 0x400
        with pytest.raises(TransportError, match="provenance"):
            discover(tmp_path)
    finally:
        path.parent.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="native Windows provenance")
def test_windows_refuses_descriptor_replacement_race_via_public_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = publish(tmp_path)
    _secure_windows_descriptor_tree(tmp_path, path)
    replacement = path.with_name("replacement.json")
    replacement.write_text(json.dumps(descriptor_wire()), encoding="utf-8")
    _windows_set_dacl(replacement, _windows_owner_equivalent_sddl())
    real_open = os.open
    replaced = False

    def replacing_open(name: str | os.PathLike[str], flags: int) -> int:
        nonlocal replaced
        if not replaced and Path(name) == path:
            replacement.replace(path)
            replaced = True
        return real_open(name, flags)

    monkeypatch.setattr(os, "open", replacing_open)
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)
    assert replaced


@pytest.mark.skipif(os.name != "posix", reason="POSIX replacement race")
def test_descriptor_replacement_during_the_bounded_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = publish(tmp_path)
    replacement = path.with_name("replacement.json")
    replacement.write_text(json.dumps(descriptor_wire()), encoding="utf-8")
    replacement.chmod(0o600)
    real_read = os.read
    replaced = False

    def replacing_read(descriptor_fd: int, length: int) -> bytes:
        nonlocal replaced
        content = real_read(descriptor_fd, length)
        if not replaced:
            replacement.replace(path)
            replaced = True
        return content

    monkeypatch.setattr(os, "read", replacing_read)
    with pytest.raises(TransportError, match="provenance"):
        discover(tmp_path)
    assert replaced


@pytest.mark.parametrize(
    "override",
    [
        {"descriptor_version": "2.0"},
        {"protocol_version": "2.0"},
        {"protocol_version": "1.1"},
        {"supported_api_versions": {"minimum": "9.0", "maximum": "9.9"}},
        {"supported_api_versions": {"minimum": "1.1", "maximum": "1.0"}},
    ],
    ids=(
        "descriptor-major",
        "protocol-major",
        "protocol-minor",
        "non-overlapping-api-window",
        "malformed-api-window",
    ),
)
def test_descriptor_incompatibility_is_refused_before_connection(
    tmp_path: Path, override: dict[str, object]
) -> None:
    """Every version disagreement is settled from the file, before any dial.

    A protocol *minor* is refused for a different reason than a major, and both
    reasons are real: a major is a different frame format, while a minor this
    build does not implement is a claim about frames it has never seen. Neither
    is negotiable -- OVC1 is this package's own frozen format, not a window.

    The malformed window's bounds are individually well formed and both sit
    inside the client's major; the only fault is that they are reversed. That is
    bad input rather than a window matching nothing, and it is refused as such
    rather than being passed through to read as "no overlap" later.
    """
    publish(tmp_path, descriptor_wire(**override))
    transport = RecordingTransport()

    with pytest.raises(CompatibilityError):
        discover(tmp_path, transport)

    assert transport.probes == []


@pytest.mark.parametrize(
    ("override", "planted"),
    [
        ({"descriptor_version": "descriptor-secret"}, "descriptor-secret"),
        ({"protocol_version": "protocol-secret"}, "protocol-secret"),
        (
            {
                "supported_api_versions": {
                    "minimum": "api-secret",
                    "maximum": CONTRACT_VERSION,
                }
            },
            "api-secret",
        ),
    ],
)
def test_descriptor_compatibility_failures_are_fixed_payload_free_and_unchained(
    tmp_path: Path, override: dict[str, object], planted: str
) -> None:
    publish(tmp_path, descriptor_wire(**override))
    transport = RecordingTransport()

    with pytest.raises(
        CompatibilityError, match="descriptor compatibility check failed"
    ) as caught:
        discover(tmp_path, transport)

    assert_sanitized(caught.value, planted)
    assert transport.probes == []


def test_descriptor_workspace_must_match_the_requested_workspace_before_connection(
    tmp_path: Path,
) -> None:
    publish(tmp_path, descriptor_wire(workspace_id="workspace-other"))
    transport = RecordingTransport()
    with pytest.raises(TransportError, match="live identity"):
        discover(tmp_path, transport)
    assert transport.probes == []


def test_live_workspace_mismatch_is_refused(tmp_path: Path) -> None:
    publish(tmp_path)
    transport = RecordingTransport(descriptor(workspace_id="workspace-other"))
    with pytest.raises(TransportError, match="live identity"):
        discover(tmp_path, transport)


def test_live_service_instance_mismatch_refuses_even_when_pid_evidence_matches(
    tmp_path: Path,
) -> None:
    publish(tmp_path)
    planted = "service-instance-secret-token"
    transport = RecordingTransport(descriptor(service_instance_id=planted))
    with pytest.raises(TransportError, match="live identity") as caught:
        discover(tmp_path, transport)
    assert_sanitized(caught.value, planted)


def test_stale_pid_is_only_corroboration_when_live_identity_matches(
    tmp_path: Path,
) -> None:
    publish(
        tmp_path,
        descriptor_wire(
            process={"pid": 999_999, "start_time": "old", "boot_id": "old"}
        ),
    )
    found = discover(tmp_path)
    assert found is not None
    assert found.descriptor.service_instance_id == SERVICE_INSTANCE_ID


@pytest.mark.parametrize(
    "published_at",
    ["2001-01-01T00:00:00Z", "2099-12-31T23:59:59Z"],
    ids=("long-stale", "far-future"),
)
def test_publication_time_never_decides_discovery_in_either_direction(
    tmp_path: Path, published_at: str
) -> None:
    """A publication timestamp is not evidence, however it reads.

    Years stale or dated in the future: neither is a reason to refuse an endpoint
    whose live identity agrees, and neither is a reason to accept one whose live
    identity does not. A clock is the one input an attacker and a badly configured
    host produce identically, so nothing here may turn on it.

    A third case used to ride along here -- an offset spelling, `+05:30` -- on the
    reading that an installation's timezone is as irrelevant as its clock. It is
    not the same thing. `Timestamp` is declared UTC with a literal `Z`, so that
    value is not a differently-written instant but a malformed field, and it now
    belongs to the test below. What this one asserts is unchanged: both values here
    conform, and discovery still turns on live identity alone.
    """
    publish(tmp_path, descriptor_wire(published_at=published_at))

    found = discover(
        tmp_path, RecordingTransport(descriptor(published_at=published_at))
    )
    assert found is not None
    assert found.descriptor.published_at == published_at

    planted = "service-instance-secret-token"
    live = descriptor(service_instance_id=planted, published_at=published_at)
    with pytest.raises(TransportError, match="live identity") as caught:
        discover(tmp_path, RecordingTransport(live))
    assert_sanitized(caught.value, planted)


def test_a_published_at_outside_the_declared_timestamp_is_refused(
    tmp_path: Path,
) -> None:
    """Refused for being malformed, not for what the clock reads.

    Both halves are asserted, because the second alone would pass for the wrong
    reason: the client's document refusal is the same fixed string a truncated
    file or a duplicated member produces, so it cannot on its own show that the
    timestamp is why. The contract's refusal names the field, and that is the
    reason this document is rejected.
    """
    offset = "2026-07-30T11:59:58+05:30"

    with pytest.raises(ContractSemanticError) as refusal:
        decode_service_endpoint_descriptor(descriptor_wire(published_at=offset))
    assert str(refusal.value) == "published_at is not a canonical RFC 3339 UTC Timestamp"

    publish(tmp_path, descriptor_wire(published_at=offset))
    with pytest.raises(ProtocolError) as caught:
        discover(tmp_path, RecordingTransport(descriptor()))
    assert str(caught.value) == "descriptor document is not an accepted public descriptor"


@pytest.mark.parametrize(
    ("status", "live_overrides", "live_process", "expected"),
    [
        ("fail", {}, "same", "live discovery"),
        ("pass", {"service_instance_id": "service-instance-other"}, "same", "identity"),
        ("pass", {"workspace_id": "workspace-other"}, "rebooted", "identity"),
    ],
    ids=("failed-probe", "instance-mismatch", "workspace-mismatch-after-reboot"),
)
def test_process_evidence_is_corroboration_and_never_rescues_a_live_failure(
    tmp_path: Path,
    status: str,
    live_overrides: dict[str, object],
    live_process: str,
    expected: str,
) -> None:
    """Process evidence is all-or-none corroboration, never sufficient on its own.

    The corroboration offered here is as strong as it gets: a PID that is
    genuinely running -- this interpreter's own, so it cannot be dismissed as a
    stale or reused number -- with a start time and boot identifier the file and
    the live answer agree on exactly. A failed probe stays failed and a mismatched
    identity stays mismatched anyway, because the live identity probe is the thing
    being corroborated and there is nothing to corroborate without it.
    """
    evidence = {
        "pid": os.getpid(),
        "start_time": "1785412798.42",
        "boot_id": "boot-7f3c",
    }
    publish(tmp_path, descriptor_wire(process=evidence))
    live = descriptor(
        process=(
            evidence if live_process == "same" else {**evidence, "boot_id": "boot-9a11"}
        ),
        **live_overrides,
    )
    transport = FixedResultTransport(
        ServiceProbeResult(
            probe="service.discover",
            status=status,
            server_version="1.2.5",
            api_version=CONTRACT_VERSION,
            observed_at="2026-07-30T12:00:00Z",
            descriptor=live,
        )
    )

    with pytest.raises(TransportError, match=expected):
        discover(tmp_path, transport)

    assert len(transport.probes) == 1


def test_failed_discovery_status_is_refused_even_with_a_descriptor(
    tmp_path: Path,
) -> None:
    publish(tmp_path)
    result = ServiceProbeResult(
        probe="service.discover",
        status="fail",
        server_version="1.2.5",
        api_version=CONTRACT_VERSION,
        observed_at="2026-07-30T12:00:00Z",
        descriptor=descriptor(),
    )

    with pytest.raises(TransportError, match="live discovery"):
        discover(tmp_path, FixedResultTransport(result))


def test_live_result_subclass_cannot_lie_about_invalid_fields(tmp_path: Path) -> None:
    publish(tmp_path)
    accepted_wire = ServiceProbeResult(
        probe="service.discover",
        status="pass",
        server_version="1.2.5",
        api_version=CONTRACT_VERSION,
        observed_at="2026-07-30T12:00:00Z",
        descriptor=descriptor(),
    ).to_wire()

    class LyingProbeResult(ServiceProbeResult):
        def to_wire(self) -> dict[str, Any]:
            return accepted_wire

    result = LyingProbeResult(
        probe="service.health",
        status="fail",
        server_version="1.2.5",
        api_version=CONTRACT_VERSION,
        observed_at="2026-07-30T12:00:00Z",
        descriptor=None,
    )

    with pytest.raises(TransportError, match="live discovery"):
        discover(tmp_path, FixedResultTransport(result))


def test_overloaded_string_equality_cannot_spoof_probe_status_or_live_identity(
    tmp_path: Path,
) -> None:
    publish(tmp_path)
    spoof = EqualitySpoof("foreign-service-secret")
    live = dataclasses.replace(
        descriptor(), workspace_id=spoof, service_instance_id=spoof
    )
    result = ServiceProbeResult(
        probe=EqualitySpoof("service.health"),
        status=EqualitySpoof("fail"),
        server_version="1.2.5",
        api_version=CONTRACT_VERSION,
        observed_at="2026-07-30T12:00:00Z",
        descriptor=live,
    )

    with pytest.raises(TransportError, match="live discovery") as caught:
        discover(tmp_path, FixedResultTransport(result))
    assert_sanitized(caught.value, spoof)


def test_live_identity_fields_must_be_exact_builtin_strings(tmp_path: Path) -> None:
    publish(tmp_path)
    spoof = EqualitySpoof("foreign-service-secret")
    live = dataclasses.replace(
        descriptor(), workspace_id=spoof, service_instance_id=spoof
    )
    result = ServiceProbeResult(
        probe="service.discover",
        status="pass",
        server_version="1.2.5",
        api_version=CONTRACT_VERSION,
        observed_at="2026-07-30T12:00:00Z",
        descriptor=live,
    )

    with pytest.raises(TransportError, match="live identity") as caught:
        discover(tmp_path, FixedResultTransport(result))
    assert_sanitized(caught.value, spoof)


def test_live_result_descriptor_is_semantically_validated_without_leakage(
    tmp_path: Path,
) -> None:
    publish(tmp_path)
    planted = "https://user:live-secret@example.test/core"
    live = dataclasses.replace(descriptor(), endpoint_uri=planted)
    result = ServiceProbeResult(
        probe="service.discover",
        status="pass",
        server_version="1.2.5",
        api_version=CONTRACT_VERSION,
        observed_at="2026-07-30T12:00:00Z",
        descriptor=live,
    )

    with pytest.raises(TransportError, match="live discovery") as caught:
        discover(tmp_path, FixedResultTransport(result))
    assert_sanitized(caught.value, planted)


def test_live_result_is_structurally_validated(tmp_path: Path) -> None:
    publish(tmp_path)
    result = ServiceProbeResult(
        probe="service.discover",
        status="pass",
        server_version=object(),  # type: ignore[arg-type]
        api_version=CONTRACT_VERSION,
        observed_at="2026-07-30T12:00:00Z",
        descriptor=descriptor(),
    )

    with pytest.raises(TransportError, match="live discovery"):
        discover(tmp_path, FixedResultTransport(result))


@pytest.mark.parametrize("nested_kind", ["descriptor", "process"])
def test_malformed_nested_dto_renderer_is_never_called_or_leaked(
    tmp_path: Path, nested_kind: str
) -> None:
    publish(tmp_path)
    planted = f"planted-{nested_kind}-renderer-secret"

    class ExplodingDescriptor(ServiceEndpointDescriptor):
        def to_wire(self) -> dict[str, Any]:
            raise RuntimeError(planted)

    class ExplodingProcess(ServiceProcessEvidence):
        def to_wire(self) -> dict[str, Any]:
            raise RuntimeError(planted)

    live: ServiceEndpointDescriptor
    if nested_kind == "descriptor":
        live = ExplodingDescriptor.from_wire(descriptor_wire())
    else:
        live = dataclasses.replace(
            descriptor(),
            process=ExplodingProcess(
                pid=4821,
                start_time="1785412798.42",
                boot_id="boot-7f3c",
            ),
        )
    result = ServiceProbeResult(
        probe="service.discover",
        status="pass",
        server_version="1.2.5",
        api_version=CONTRACT_VERSION,
        observed_at="2026-07-30T12:00:00Z",
        descriptor=live,
    )

    with pytest.raises(TransportError, match="live discovery") as caught:
        discover(tmp_path, FixedResultTransport(result))

    assert_sanitized(caught.value, planted)


def test_ordinary_normalization_exception_is_fixed_payload_free_and_unchained(
    tmp_path: Path,
) -> None:
    publish(tmp_path)
    planted = "planted-normalization-runtime-secret"

    class ExplodingDetails(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            raise RuntimeError(planted)

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError(planted)

        def __len__(self) -> int:
            return 1

    result = ServiceProbeResult(
        probe="service.discover",
        status="pass",
        server_version="1.2.5",
        api_version=CONTRACT_VERSION,
        observed_at="2026-07-30T12:00:00Z",
        descriptor=descriptor(),
        details=ExplodingDetails(),
    )

    with pytest.raises(TransportError, match="live discovery") as caught:
        discover(tmp_path, FixedResultTransport(result))

    assert_sanitized(caught.value, planted)


def test_transport_probe_failure_is_fixed_payload_free_and_unchained(
    tmp_path: Path,
) -> None:
    """An injected transport's own exception is translated, never re-raised.

    A transport is third-party code to this package: its diagnostics are outside
    the payload-free rule and may name a credential or a local path. So the probe
    call is a boundary like every decode here, and what crosses it is a fixed
    sentence with no chain.
    """
    publish(tmp_path)
    planted = "Bearer sk-live-DEADBEEF /Users/victim/.omnivia/state"

    class ExplodingTransport(RecordingTransport):
        def probe(
            self,
            request: ServiceProbeRequest,
            *,
            deadline: Deadline,
            cancellation: CancellationToken | None = None,
        ) -> ServiceProbeResult:
            self.probes.append((request, deadline, cancellation))
            raise RuntimeError(f"upstream dial failed: {planted}")

    with pytest.raises(TransportError, match="live discovery") as caught:
        discover(tmp_path, ExplodingTransport())

    assert_sanitized(caught.value, planted)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (DeadlineExceededError, DeadlineExceededError),
        (OperationCancelledError, OperationCancelledError),
        (RuntimeError, TransportError),
    ],
    ids=("expired", "cancelled", "unreachable"),
)
def test_transport_probe_failure_keeps_its_kind_and_loses_its_words(
    tmp_path: Path,
    raised: type[BaseException],
    expected: type[ClientError],
) -> None:
    """Translation is not flattening: the declared type survives, the message does not.

    A cancelled call was abandoned by the caller and an expired one by the clock,
    and neither is "the transport could not carry this" -- so all three stay
    distinguishable. What none of them keeps is the transport's own sentence,
    which is third-party text: the type is re-raised as a fresh instance rather
    than the object the transport built, so a secret cannot ride the taxonomy out.
    """
    publish(tmp_path)
    planted = "Bearer sk-live-DEADBEEF /Users/victim/.omnivia/state"

    class ExplodingTransport(RecordingTransport):
        def probe(
            self,
            request: ServiceProbeRequest,
            *,
            deadline: Deadline,
            cancellation: CancellationToken | None = None,
        ) -> ServiceProbeResult:
            self.probes.append((request, deadline, cancellation))
            raise raised(planted)

    with pytest.raises(expected) as caught:
        discover(tmp_path, ExplodingTransport())

    assert type(caught.value) is expected
    assert_sanitized(caught.value, planted)


@pytest.mark.parametrize(
    "result",
    [
        ServiceProbeResult(
            probe="service.health",
            status="pass",
            server_version="1.2.5",
            api_version=CONTRACT_VERSION,
            observed_at="2026-07-30T12:00:00Z",
            descriptor=descriptor(),
        ),
        ServiceProbeResult(
            probe="service.discover",
            status="fail",
            server_version="1.2.5",
            api_version=CONTRACT_VERSION,
            observed_at="2026-07-30T12:00:00Z",
        ),
    ],
)
def test_non_discovery_or_descriptorless_live_answers_are_refused(
    tmp_path: Path, result: ServiceProbeResult
) -> None:
    publish(tmp_path)

    class ResultTransport(RecordingTransport):
        def probe(
            self,
            request: ServiceProbeRequest,
            *,
            deadline: Deadline,
            cancellation: CancellationToken | None = None,
        ) -> ServiceProbeResult:
            self.probes.append((request, deadline, cancellation))
            return result

    with pytest.raises(TransportError, match="live discovery"):
        discover(tmp_path, ResultTransport())


def test_pre_cancelled_discovery_does_not_reach_transport(tmp_path: Path) -> None:
    publish(tmp_path)
    token = CancellationToken()
    token.cancel()
    transport = RecordingTransport()

    with pytest.raises(OperationCancelledError):
        discover_endpoint(
            tmp_path,
            WORKSPACE_ID,
            transport=transport,
            deadline=Deadline.after(30.0, clock=FakeClock()),
            cancellation=token,
        )
    assert transport.probes == []


def test_expired_discovery_does_not_reach_transport(tmp_path: Path) -> None:
    publish(tmp_path)
    transport = RecordingTransport()
    with pytest.raises(DeadlineExceededError):
        discover_endpoint(
            tmp_path,
            WORKSPACE_ID,
            transport=transport,
            deadline=Deadline.after(0.0, clock=FakeClock()),
        )
    assert transport.probes == []


def test_discovery_passes_the_same_shrinking_deadline_and_token_to_the_probe(
    tmp_path: Path,
) -> None:
    publish(tmp_path)
    clock = FakeClock()
    deadline = Deadline.after(30.0, clock=clock)
    clock.now = 11.25
    token = CancellationToken()
    transport = RecordingTransport()

    found = discover_endpoint(
        tmp_path,
        WORKSPACE_ID,
        transport=transport,
        deadline=deadline,
        cancellation=token,
    )

    assert found is not None
    request, received_deadline, received_token = transport.probes[0]
    assert request.deadline_ms == 18_750
    assert received_deadline is deadline
    assert received_token is token
