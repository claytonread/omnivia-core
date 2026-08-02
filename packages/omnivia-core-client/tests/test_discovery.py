"""Installation-local endpoint discovery and live identity verification."""

from __future__ import annotations

import csv
import ctypes
import dataclasses
import json
import os
import stat
import subprocess
import traceback
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

import pytest
from omnivia_core_client import (
    CancellationToken,
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
    RequestEnvelope,
    ResponseEnvelope,
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProbeResult,
    ServiceProcessEvidence,
)

WORKSPACE_ID = "workspace-alpha"
SERVICE_INSTANCE_ID = "service-instance-01"


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
        "endpoint_uri": "unix:///var/run/omnivia/core.sock",
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
    return path


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
        result = advapi.SetNamedSecurityInfoW(
            str(path),
            1,
            0x00000004 | 0x80000000,
            None,
            None,
            dacl,
            None,
        )
        assert result == 0
    finally:
        kernel.LocalFree.argtypes = [wintypes.LPVOID]
        kernel.LocalFree.restype = wintypes.LPVOID
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
def test_posix_rejects_group_or_other_access_on_each_derived_parent(
    tmp_path: Path,
) -> None:
    path = publish(tmp_path)
    for parent in (tmp_path / "runtime", path.parent):
        parent.chmod(0o750)
        with pytest.raises(TransportError, match="provenance"):
            discover(tmp_path)
        parent.chmod(0o700)


@pytest.mark.skipif(os.name != "posix", reason="POSIX provenance")
def test_posix_owner_mismatch_is_rejected_through_the_public_discovery_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_descriptor_incompatibility_is_refused_before_connection(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport()
    for override in (
        {"descriptor_version": "2.0"},
        {"protocol_version": "2.0"},
        {"supported_api_versions": {"minimum": "9.0", "maximum": "9.9"}},
    ):
        publish(tmp_path, descriptor_wire(**override))
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
