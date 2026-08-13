"""The fixed-principal MCP configuration fails closed before initialization."""

from __future__ import annotations

import ctypes
import dataclasses
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_client import CredentialReference
from omnivia_core_mcp import configuration
from omnivia_core_mcp.configuration import (
    CONFIGURATION_FORMAT,
    MAXIMUM_CONFIGURATION_BYTES,
    McpConfiguration,
    McpConfigurationError,
    parse_configuration,
    read_configuration,
)

INSTALLATION_STATE = Path.cwd().resolve() / "installation-state"


def managed_document(**overrides: object) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": CONFIGURATION_FORMAT,
        "principal_id": "local-user",
        "allowed_workspace_ids": ["workspace-alpha"],
        "default_workspace_id": "workspace-alpha",
        "allowed_purposes": ["workspace_inspection", "knowledge_retrieval"],
        "mutation_enabled": False,
        "service_mode": "managed_local",
        "installation_state": str(INSTALLATION_STATE),
    }
    document.update(overrides)
    return document


def service_document(**overrides: object) -> dict[str, Any]:
    document = managed_document(
        service_mode="service_client",
        endpoint="https://Core.Example/",
        credential_reference="core.default",
    )
    del document["installation_state"]
    document.update(overrides)
    return document


def write_config(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    make_private(path)
    return path


def make_private(path: Path) -> None:
    """Apply the platform's owner-only test-file policy."""
    path.chmod(0o600)
    if os.name != "nt":
        return
    identity = subprocess.run(
        ["whoami"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{identity}:F",
            "/q",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def assert_payload_free(error: BaseException, *values: str) -> None:
    rendered = " ".join(
        (
            str(error),
            repr(error),
            repr(error.args),
            repr(error.__cause__),
            repr(error.__context__),
        )
    )
    for value in values:
        assert value not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_managed_configuration_is_immutable_and_selects_its_default() -> None:
    config = parse_configuration(managed_document())
    assert config.service_mode == "managed_local"
    assert config.installation_state == INSTALLATION_STATE
    assert config.selected_workspace_id == "workspace-alpha"
    assert config.mutation_enabled is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.principal_id = "other"  # type: ignore[misc]


def test_a_single_allowlisted_workspace_is_an_unambiguous_implicit_default() -> None:
    document = managed_document()
    del document["default_workspace_id"]
    assert parse_configuration(document).selected_workspace_id == "workspace-alpha"


def test_several_workspaces_without_a_default_select_nothing() -> None:
    document = managed_document(allowed_workspace_ids=["workspace-a", "workspace-b"])
    del document["default_workspace_id"]
    assert parse_configuration(document).selected_workspace_id is None


def test_service_configuration_uses_client_value_types_and_normalizes_origin() -> None:
    config = parse_configuration(service_document())
    assert config.endpoint == "https://core.example:443"
    assert config.credential_reference == CredentialReference("core.default")
    assert config.installation_state is None


@pytest.mark.parametrize(
    "change",
    [
        {"format": "omnivia.mcp-config.v2"},
        {"principal_id": ""},
        {"principal_id": "has space"},
        {"principal_id": "x" * 129},
        {"allowed_workspace_ids": []},
        {"allowed_workspace_ids": ["workspace-alpha", "workspace-alpha"]},
        {"allowed_workspace_ids": ["../escape"]},
        {"allowed_purposes": []},
        {"allowed_purposes": ["knowledge_retrieval", "knowledge_retrieval"]},
        {"allowed_purposes": ["has space"]},
        {"mutation_enabled": 1},
        {"service_mode": "ambient"},
        {"default_workspace_id": "workspace-other"},
        {"unknown": True},
    ],
)
def test_invalid_or_authority_widening_values_are_refused(
    change: dict[str, object],
) -> None:
    with pytest.raises(McpConfigurationError):
        parse_configuration(managed_document(**change))


@pytest.mark.parametrize(
    "missing",
    [
        "format",
        "principal_id",
        "allowed_workspace_ids",
        "allowed_purposes",
        "service_mode",
    ],
)
def test_required_fields_are_required(missing: str) -> None:
    document = managed_document()
    del document[missing]
    with pytest.raises(McpConfigurationError):
        parse_configuration(document)


def test_mode_fields_are_exact_and_mutually_exclusive() -> None:
    for document in (
        managed_document(
            endpoint="https://core.example", credential_reference="core.default"
        ),
        managed_document(installation_state="relative/path"),
        service_document(installation_state="/tmp/state"),
        service_document(endpoint="http://core.example:80"),
        service_document(endpoint="https://user:secret@core.example"),
        service_document(credential_reference="eyHeader.payload.signature"),
    ):
        with pytest.raises(McpConfigurationError):
            parse_configuration(document)


def test_direct_construction_and_dataclass_replacement_revalidate() -> None:
    config = parse_configuration(managed_document())
    with pytest.raises(McpConfigurationError):
        dataclasses.replace(config, allowed_workspace_ids=("../escape",))
    with pytest.raises(McpConfigurationError):
        dataclasses.replace(config, mutation_enabled=1)  # type: ignore[arg-type]


def test_configuration_repr_redacts_private_values() -> None:
    config = parse_configuration(service_document())
    rendered = repr(config)
    assert rendered == "McpConfiguration(<redacted>)"
    assert "core.example" not in rendered
    assert "core.default" not in rendered


def test_a_private_regular_file_is_read_by_explicit_absolute_path(
    tmp_path: Path,
) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    assert read_configuration(path).principal_id == "local-user"


def test_a_relative_path_is_not_a_configuration_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path / "mcp.json", managed_document())
    monkeypatch.chdir(tmp_path)
    with pytest.raises(McpConfigurationError):
        read_configuration(Path("mcp.json"))


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666])
@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not encode a DACL")
def test_posix_group_or_other_access_is_refused(tmp_path: Path, mode: int) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    path.chmod(mode)
    with pytest.raises(McpConfigurationError) as raised:
        read_configuration(path)
    assert_payload_free(raised.value, str(path), "local-user")


def test_a_non_regular_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(McpConfigurationError):
        read_configuration(tmp_path)


@pytest.mark.skipif(
    os.name == "nt", reason="creating a Windows symlink requires host privilege"
)
def test_a_symlink_is_refused(tmp_path: Path) -> None:
    target = write_config(tmp_path / "target.json", managed_document())
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(McpConfigurationError):
        read_configuration(link)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership uses the effective uid")
def test_an_owner_mismatch_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    real_effective_user = os.geteuid()
    monkeypatch.setattr(configuration.os, "geteuid", lambda: real_effective_user + 1)
    with pytest.raises(McpConfigurationError):
        read_configuration(path)


def test_a_replacement_during_the_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    original = configuration._same_file
    comparisons = 0

    def disagree_after_open(first: os.stat_result, second: os.stat_result) -> bool:
        nonlocal comparisons
        comparisons += 1
        return comparisons == 1 and original(first, second)

    monkeypatch.setattr(configuration, "_same_file", disagree_after_open)
    with pytest.raises(McpConfigurationError):
        read_configuration(path)


def test_windows_requires_an_owner_only_acl_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    monkeypatch.setattr(configuration, "_IS_WINDOWS", True)
    monkeypatch.setattr(configuration, "_windows_owner_only", lambda descriptor: False)
    with pytest.raises(McpConfigurationError):
        read_configuration(path)
    monkeypatch.setattr(configuration, "_windows_owner_only", lambda descriptor: True)
    assert read_configuration(path).service_mode == "managed_local"


def test_windows_acl_verifier_errors_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    monkeypatch.setattr(configuration, "_IS_WINDOWS", True)

    def broken(_descriptor: int) -> bool:
        raise RuntimeError("C:/private/credential-store")

    monkeypatch.setattr(configuration, "_windows_owner_only", broken)
    with pytest.raises(McpConfigurationError) as raised:
        read_configuration(path)
    assert_payload_free(raised.value, "C:/private/credential-store", str(path))


@pytest.mark.parametrize(
    ("owner_matches", "aces", "owner_only"),
    [
        # An owner-only file: every access-allowed ACE names the owner.
        (True, ((0, True),), True),
        (True, ((1, False), (0, True)), True),
        # A present but empty DACL denies everyone, which is narrower still.
        (True, (), True),
        # An absent or NULL DACL grants everyone everything.
        (True, None, False),
        # The file is owner-only for somebody who is not this process's user.
        (False, ((0, True),), False),
        (False, None, False),
        # An access-allowed ACE naming another principal widens the reach.
        (True, ((0, False),), False),
        (True, ((0, True), (0, False)), False),
        # Unrecognised access-allowed forms: object, callback, callback-object,
        # compound. Their grantee is not where this reader looks for it.
        (True, ((5, True),), False),
        (True, ((9, True),), False),
        (True, ((11, True),), False),
        (True, ((4, True),), False),
    ],
)
def test_the_windows_owner_only_dacl_policy(
    owner_matches: bool, aces: tuple[tuple[int, bool], ...] | None, owner_only: bool
) -> None:
    assert configuration._owner_only_dacl(owner_matches, aces) is owner_only


def windows_sid(*subauthorities: int) -> bytes:
    """One well-formed binary SID: revision, count, authority, subauthorities."""
    return bytes([1, len(subauthorities), 0, 0, 0, 0, 0, 5]) + b"".join(
        value.to_bytes(4, "little") for value in subauthorities
    )


def windows_ace(kind: int, sid: bytes, *, size: int | None = None) -> bytes:
    """One ACE: `ACE_HEADER`, `ACCESS_MASK`, then the trustee SID inline."""
    declared = len(sid) + 8 if size is None else size
    return (
        bytes([kind, 0])
        + declared.to_bytes(2, "little")
        + (0x1F01FF).to_bytes(4, "little")
        + sid
    )


OWNER_SID = windows_sid(21, 1, 2, 3, 1001)
OTHER_SID = windows_sid(21, 1, 2, 3, 1002)


class FakeSecurityApi:
    """A Win32 double that lays its ACL out in real memory.

    Only the API is faked. The decoder under test still walks raw addresses,
    reads `ACE_HEADER` fields at their true offsets, and copies each SID out of
    that memory -- the part that cannot otherwise run off Windows.
    """

    def __init__(
        self,
        aces: list[bytes],
        *,
        owner: bytes = OWNER_SID,
        user: bytes = OWNER_SID,
        dacl_present: bool = True,
        security_error: int = 0,
    ) -> None:
        self.acl = ctypes.create_string_buffer(b"".join(aces) or b"\0")
        self.addresses: list[int] = []
        offset = 0
        for encoded in aces:
            self.addresses.append(ctypes.addressof(self.acl) + offset)
            offset += len(encoded)
        self.owner = ctypes.create_string_buffer(owner)
        self.user = ctypes.create_string_buffer(user)
        self.token_user = configuration._TokenUser()
        self.token_user.User.Sid = ctypes.addressof(self.user)
        self.dacl_present = dacl_present
        self.security_error = security_error
        self.freed: list[object] = []
        self.closed: list[object] = []

    def get_osfhandle(self, descriptor: int) -> int:
        return 500 + descriptor

    def GetSecurityInfo(
        self,
        handle: int,
        kind: int,
        wanted: int,
        owner: Any,
        group: Any,
        dacl: Any,
        sacl: Any,
        security: Any,
    ) -> int:
        owner.value = ctypes.addressof(self.owner)
        dacl.value = ctypes.addressof(self.acl) if self.dacl_present else None
        security.value = 0xD0D0
        return self.security_error

    def GetCurrentProcess(self) -> int:
        return 7

    def OpenProcessToken(self, process: int, access: int, token: Any) -> int:
        token.value = 0x7070
        return 1

    def GetTokenInformation(
        self, token: Any, kind: int, buffer: Any, size: int, needed: Any
    ) -> int:
        needed.value = ctypes.sizeof(self.token_user)
        if buffer is None:
            return 0
        ctypes.memmove(buffer, ctypes.byref(self.token_user), needed.value)
        return 1

    def IsValidSid(self, sid: int) -> int:
        return 1

    def GetLengthSid(self, sid: int) -> int:
        return 8 + 4 * ctypes.string_at(sid + 1, 1)[0]

    def GetAclInformation(
        self, acl: int, information: Any, size: int, kind: int
    ) -> int:
        information.AceCount = len(self.addresses)
        return 1

    def GetAce(self, acl: int, index: int, ace: Any) -> int:
        ace.value = self.addresses[index]
        return 1

    def CloseHandle(self, handle: Any) -> int:
        self.closed.append(handle.value)
        return 1

    def LocalFree(self, memory: Any) -> int:
        self.freed.append(memory.value)
        return 0


def test_the_windows_ace_walk_reads_each_grantee_out_of_real_memory() -> None:
    api = FakeSecurityApi(
        [
            windows_ace(0, OWNER_SID),
            windows_ace(1, OTHER_SID),
            windows_ace(0, OTHER_SID),
            windows_ace(9, OWNER_SID),
        ]
    )
    assert configuration._dacl_aces(api, 1, OWNER_SID) == (
        (0, True),
        (1, False),
        (0, False),
        (9, False),
    )


def test_a_null_dacl_is_reported_as_no_dacl_rather_than_an_empty_one() -> None:
    assert configuration._dacl_aces(FakeSecurityApi([]), None, OWNER_SID) is None
    assert configuration._dacl_aces(FakeSecurityApi([]), 1, OWNER_SID) == ()


def test_an_ace_declaring_less_room_than_its_sid_needs_is_refused() -> None:
    api = FakeSecurityApi([windows_ace(0, OWNER_SID, size=8)])
    with pytest.raises(OSError):
        configuration._dacl_aces(api, 1, OWNER_SID)


@pytest.mark.parametrize(
    ("api", "owner_only"),
    [
        (FakeSecurityApi([windows_ace(0, OWNER_SID)]), True),
        (
            FakeSecurityApi([windows_ace(0, OWNER_SID), windows_ace(0, OTHER_SID)]),
            False,
        ),
        (FakeSecurityApi([windows_ace(0, OWNER_SID)], user=OTHER_SID), False),
        (FakeSecurityApi([], dacl_present=False), False),
    ],
)
def test_the_windows_verdict_from_end_to_end_native_facts(
    api: FakeSecurityApi, owner_only: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(configuration, "_SECURITY_API", api)
    assert configuration._windows_owner_only(3) is owner_only
    assert api.freed == [0xD0D0]
    assert api.closed == [0x7070]


def test_the_security_descriptor_and_token_are_released_when_the_walk_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeSecurityApi([windows_ace(0, OWNER_SID, size=8)])
    monkeypatch.setattr(configuration, "_SECURITY_API", api)
    with pytest.raises(OSError):
        configuration._windows_owner_only(3)
    assert api.freed == [0xD0D0]
    assert api.closed == [0x7070]


def test_a_security_info_error_still_releases_an_allocated_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeSecurityApi([], security_error=5)
    monkeypatch.setattr(configuration, "_SECURITY_API", api)
    with pytest.raises(OSError):
        configuration._windows_owner_only(3)
    assert api.freed == [0xD0D0]
    assert api.closed == []


def test_the_windows_verifier_decides_only_from_the_native_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for facts, expected in (
        ((True, ((0, True),)), True),
        ((True, ((0, False),)), False),
        ((True, None), False),
        ((False, ((0, True),)), False),
    ):
        monkeypatch.setattr(configuration, "_windows_acl_facts", lambda _d, f=facts: f)
        assert configuration._windows_owner_only(7) is expected


@pytest.mark.skipif(os.name != "nt", reason="the native proof is Windows-only")
def test_the_native_windows_proof_reads_a_real_descriptor(tmp_path: Path) -> None:
    path = write_config(tmp_path / "mcp.json", managed_document())
    descriptor = os.open(path, os.O_RDONLY)
    try:
        # The owner verdict is not asserted: an elevated process creates files
        # owned by Administrators, so it is host policy, not a property of this
        # code. What is asserted is that the whole native path completes, hands
        # back well-formed facts, and stays stable when repeated -- a leaked or
        # double-freed handle or descriptor would not survive the second call.
        owner_matches, aces = configuration._windows_acl_facts(descriptor)
        assert isinstance(owner_matches, bool)
        assert aces is not None and all(
            isinstance(kind, int) and isinstance(grants_owner, bool)
            for kind, grants_owner in aces
        )
        assert configuration._windows_acl_facts(descriptor) == (owner_matches, aces)
        assert configuration._windows_owner_only(descriptor) is (
            configuration._owner_only_dacl(owner_matches, aces)
        )
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name != "nt", reason="the native proof is Windows-only")
def test_the_native_windows_proof_fails_closed_on_an_unusable_descriptor() -> None:
    with pytest.raises(OSError):
        configuration._windows_owner_only(-1)


@pytest.mark.parametrize(
    "content",
    [
        b"\xef\xbb\xbf{}",
        b"\xff",
        b"{} trailing",
        b'{"format":"a","format":"b"}',
        b'{"outer":{"field":1,"field":2}}',
        b"NaN",
        b"[]",
    ],
)
def test_malformed_documents_are_refused_without_payload_leakage(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "mcp.json"
    path.write_bytes(content)
    make_private(path)
    with pytest.raises(McpConfigurationError) as raised:
        read_configuration(path)
    assert_payload_free(raised.value, str(path), "trailing", "outer")


def test_the_byte_limit_is_checked_with_a_bounded_read(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_bytes(b" " * (MAXIMUM_CONFIGURATION_BYTES + 1))
    make_private(path)
    with pytest.raises(McpConfigurationError):
        read_configuration(path)


def test_exactly_the_maximum_is_not_rejected_as_oversized(tmp_path: Path) -> None:
    encoded = json.dumps(managed_document()).encode("utf-8")
    path = tmp_path / "mcp.json"
    path.write_bytes(encoded + b" " * (MAXIMUM_CONFIGURATION_BYTES - len(encoded)))
    make_private(path)
    assert read_configuration(path).format == CONFIGURATION_FORMAT


def test_semantic_failures_drop_sensitive_values_and_parser_context() -> None:
    secret = "Bearer secret-that-must-not-render"
    with pytest.raises(McpConfigurationError) as raised:
        parse_configuration(service_document(credential_reference=secret))
    assert_payload_free(raised.value, secret, "core.example", "local-user")


def test_the_public_model_accepts_only_tuple_authority_sets() -> None:
    with pytest.raises(McpConfigurationError):
        McpConfiguration(
            format=CONFIGURATION_FORMAT,
            principal_id="local-user",
            allowed_workspace_ids=["workspace-alpha"],  # type: ignore[arg-type]
            default_workspace_id="workspace-alpha",
            allowed_purposes=("workspace_inspection",),
            mutation_enabled=False,
            service_mode="managed_local",
            installation_state=Path("/var/lib/omnivia/installation-state"),
            endpoint=None,
            credential_reference=None,
        )
